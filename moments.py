# -*- coding: utf-8 -*-
"""看朋友圈：读动态、判断感不感兴趣、点赞 / 评论。

数据来源（2026-10-18 实测）：
  · ``MomentDB.get_moments()`` 读本地 ``sns.db`` 的 ``SnsTimeLine`` 表
  · 每条是一个 dict：
        {"id": "15029789017517724167",   <- 真正的动态 id（数字字符串）
         "tid": "wxid_xxx",              <- **字段名有坑，实际是 username**
         "username": "wxid_xxx",
         "nickname": "💤",
         "text": "去剪头了",
         "images": [], "videos": [],
         "likes": [], "comments": [],
         "create_time": 1791690470}
  · **本地缓存只有十几条，而且图片基本是空的**（实测 15 条全 images=[]）。
    所以"根据图片内容回复"只能：先 download_moment_media 拉下来，
    拉不到就只按文字判断 —— 不能假装看到了图。

交互（点赞/评论）走 ``Moment`` 这个 UI 类，不是数据库：
  ``LikeMoment(publisher=...)`` / ``CommentMoment(publisher=...)``
  这两个会**打开朋友圈窗口、滚动定位、点浮层** —— 属于会真的动用户界面的操作。
"""
from __future__ import annotations

import time as _import_time
import json
import re
import time

# 水位：记住"处理到哪一条了"。用动态的 id（数字字符串）比 create_time 可靠，
# 因为同一个人连发两条时 create_time 可能相同。
STATE_KEY = "moments_seen"


def _post_id(post: dict) -> str:
    for k in ("id", "tid", "local_id"):
        v = post.get(k)
        if v is not None and str(v).strip():
            s = str(v).strip()
            # tid 那个字段实际是 wxid，不能当 id 用
            if not s.startswith("wxid_") and not s.startswith("v3_"):
                return s
    return ""


# 「让她去看朋友圈」的触发方式：**只看关键词组合，不要求连续出现**。
#
# 踩过的坑（2026-10-19）：原来写的是连续词表（"看朋友圈" 等），
# 结果用户说「你去看我发的朋友圈」——"看"和"朋友圈"之间夹了"我发的"，
# 连续匹配失败，她嘴上答应「好好好我去翻一遍」但根本没去。
# 现场抓到的两句都匹配不上：
#     '你去看我发的朋友圈'  -> False
#     '你去看看我朋友圈'    -> False
#
# 现在改成：**句子里同时有「朋友圈」+ 一个动作词**就算。
# 宁可多触发一次（看一眼朋友圈没损失），也别她答应了不动。
MOMENTS_ACTION = ("看", "刷", "翻", "瞄", "瞅", "瞧", "扫")
# 否定语境：这些不是在让她看朋友圈
MOMENTS_NEG = ("朋友圈权限", "朋友圈关了", "不发朋友圈", "朋友圈屏蔽", "朋友圈背景")

# **只有这几种句式才算"让她去看朋友圈"**（2026-11-10 收紧）。
# 原来只要求「有朋友圈 + 任一动作词」，于是群里有人说
# "我看过他朋友圈""朋友圈那个视频" 她也会说"我去看看" —— 用户明确要求关掉。
#
# 每条都要求动词和「朋友圈」挨得足够近，且是**对她说**的语气。
MOMENTS_PATTERNS = (
    # 你去看 / 你去刷 / 你翻一下 … 朋友圈
    r"你.{0,4}(看|刷|翻|瞄|瞅|瞧|扫|点开|打开).{0,10}朋友圈",
    # 看/刷/翻 … 我的朋友圈
    r"(看|刷|翻|瞄|瞅|瞧|扫|点开|打开).{0,6}我.{0,4}朋友圈",
    # 朋友圈你去看看 / 朋友圈看看
    r"朋友圈.{0,4}(你|去).{0,4}(看|刷|翻|瞄|瞅|瞧)",
    # 去看看朋友圈 / 刷下朋友圈（祈使短语，且不在"他/她/我"后面）
    r"(?<![他她我])(去|帮).{0,4}(看|刷|翻|瞄|瞅|瞧).{0,6}朋友圈",
    # **被 @ 了 + 动作词 + 朋友圈**（@ 本身就是"对她说"）
    r"@[^\s\u2005]{0,30}.{0,6}(看|刷|翻|瞄|瞅|瞧|扫).{0,8}朋友圈",
    # **句首祈使**：看看朋友圈 / 刷下朋友圈 / 翻翻朋友圈
    r"^\s*(看|刷|翻|瞄|瞅|瞧|扫)(看|下|一下|一眼|一瞅)?\s*.{0,4}朋友圈",
    # 问句"你刷朋友圈了吗"这种也算（她答应过要去看）
    r"(看|刷|翻|瞄|瞅|瞧|扫)朋友圈.{0,6}(了?吗|没|没有)",
)
_MOMENTS_RE = None


def wants_moments(text: str, sender_wxid: str = "",
                  owner_wxid: str = "") -> bool:
    """这句话是不是**主人在让她**去看朋友圈。

    **2026-11-10 收紧**：用户反馈"群里有人发个朋友圈这个字她就会说
    我去看看，但那个不是我发的，我也不需要"。所以：
      ① 不再用宽泛的「关键词 + 动作词」判断，改成只认几种明确的祈使句式
      ② **只有主人说才算**（owner_wxid 配了就强制校验）
    """
    global _MOMENTS_RE
    # **主人校验**：配了 owner 就只认这个人。
    # 没配（空）时保持原行为，避免旧配置升级后突然不认人。
    if owner_wxid:
        if (sender_wxid or "").strip() != owner_wxid.strip():
            return False
    t = (text or "").strip()
    if not t or len(t) > 200:
        return False
    if any(n in t for n in MOMENTS_NEG):
        return False
    if "朋友圈" not in t:
        return False
    if _MOMENTS_RE is None:
        import re as _re
        _MOMENTS_RE = _re.compile("|".join(MOMENTS_PATTERNS))
    return bool(_MOMENTS_RE.search(t))


def read_posts(db, limit: int = 20) -> list[dict]:
    """读最近的朋友圈。失败返回空列表，不抛。"""
    try:
        return db.get_moments(limit=limit) or []
    except Exception:
        return []


def filter_new(posts: list[dict], seen: set) -> list[dict]:
    """挑出还没处理过的。"""
    out = []
    for p in posts:
        pid = _post_id(p)
        if pid and pid not in seen:
            out.append(p)
    return out


def describe(post: dict, image_notes: dict | None = None) -> str:
    """把一条动态写成给模型看的一行。"""
    who = post.get("nickname") or post.get("username") or "某人"
    txt = (post.get("text") or "").strip()
    imgs = post.get("images") or []
    vids = post.get("videos") or []
    parts = ["发布者：{}".format(who)]
    if post.get("create_time"):
        try:
            parts.append("时间：{}".format(
                time.strftime("%m-%d %H:%M", time.localtime(int(post["create_time"])))))
        except Exception:
            pass
    parts.append("正文：{}".format(txt if txt else "（没有文字）"))
    if imgs:
        notes = image_notes or {}
        desc = notes.get(_post_id(post))
        if desc:
            parts.append("图片内容：{}".format(desc))
        else:
            parts.append("有 {} 张图（看不清内容）".format(len(imgs)))
    if vids:
        parts.append("有 {} 个视频（**不看视频**，别假装看过）".format(len(vids)))
    if post.get("likes"):
        try:
            parts.append("已点赞：{}".format("、".join(
                str(x.get("nickname") or x) for x in post["likes"][:5])))
        except Exception:
            pass
    if post.get("comments"):
        try:
            cs = []
            for c in post["comments"][:4]:
                who2 = c.get("nickname") or c.get("username") or "?"
                cs.append("{}: {}".format(who2, (c.get("content") or "")[:20]))
            parts.append("已有评论：{}".format(" / ".join(cs)))
        except Exception:
            pass
    return "；".join(parts)


DECIDE_PROMPT = """你在刷朋友圈。下面是最近的朋友圈动态（越往下越新）。

你的任务：**按你自己的性格逐条决定**要不要点赞、要不要评论。
这里没有"标准答案"，也不用当礼貌机器人 —— 想看就看、想怼就怼、没兴趣就划过去。

**怎么判断（用你的人格，不是当客服）：**
· 你感兴趣的、觉得好笑的、有话想说的 → 点赞，甚至评论
· 图里有具体东西（地方、吃的、猫、游戏、什么好玩的东西）
  → 顺着问一句或吐槽一句，像朋友那样
· 熟人发的东西，哪怕没什么内容，点个赞也正常
· 看到想怼的、装逼的、让你无语的 → 可以阴阳一句
· 无聊的、跟你完全没关系的广告/抽奖 → 划过去

**注意分寸：**
· 评论**短**，一句话，20 字以内，别客套、别说"好棒""真美"这种假话
· 别人的自拍、难过的东西 → 别乱开荤玩笑
· 写着「看不清内容」的图 = 你**没看到那张图**：
    不要评论图里有什么，也别回答"能不能看到"这类问题
· 有视频的 = 你看不了视频，别假装看过

**分寸**：一次刷下来，**评论两三条就够**，点赞可以多点。
每条都留言就不是刷朋友圈了，是挨个点评 —— 太假。

**输出**：严格一个 JSON 数组，每个元素对应一条，顺序不变：
[{"idx": 0, "like": true, "comment": "这是哪", "reason": "游戏截图，想问一句"},
 {"idx": 1, "like": false, "comment": "", "reason": "广告，没兴趣"}]

不想互动就 like=false、comment=""。
**每条都要给 reason**（一句话，不超过 20 字）：为什么点赞/评论，或者为什么划过去。
这个 reason 是给后台排查用的，用户会看 —— 别写"无"这种废话，要写真实理由。
**该互动就互动** —— 全是 false 也很奇怪，像根本没看。
"""


def decide(cfg: dict, api_key: str, persona: str, lines: list[str],
           max_comments: int = 3) -> list[dict]:
    """让模型决定对每条动态做什么。返回与 lines 等长的列表。"""
    import brain  # 复用 llm_judge_raw（它已经处理了超时/重试/空正文）

    body = "\n".join("{}. {}".format(i, ln) for i, ln in enumerate(lines))
    user = DECIDE_PROMPT + "\n\n动态列表：\n" + body
    try:
        raw = brain.llm_judge_raw(cfg, api_key, persona, user)
    except Exception:
        return [{"like": False, "comment": "", "reason": "判断失败"} for _ in lines]
    txt = raw.strip()
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return [{"like": False, "comment": "", "reason": "没解析出结果"} for _ in lines]
    try:
        arr = json.loads(brain.repair_json_strings(m.group(0)))
    except Exception:
        return [{"like": False, "comment": "", "reason": "结果不是合法 JSON"} for _ in lines]
    out = [{"like": False, "comment": "", "reason": ""} for _ in lines]
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("idx"))
        except Exception:
            continue
        if 0 <= i < len(out):
            out[i] = {
                "like": bool(item.get("like")),
                "comment": str(item.get("comment") or "").strip()[:30],
                "reason": str(item.get("reason") or "").strip()[:40],
            }
    # **评论条数硬性限制**：提示词说了"两三条就够"它经常不听，
    # 实测 8 条动态能评 5~6 条，像挨个点评。这里直接砍到 max_comments，
    # 优先保留前面的（越新的越相关）。
    if max_comments > 0:
        kept = 0
        for a in out:
            if not a.get("comment"):
                continue
            kept += 1
            if kept > max_comments:
                a["comment"] = ""
    return out


def _resp_ok(r) -> tuple:
    """解析 wechatauto 的 WxResponse。

    **它是个 dict 子类**（`param.py:41`）：
        {'status': '成功'/'失败'/'错误', 'message': '...', 'data': None}
    踩过的坑：一开始用 `getattr(r, 'message', '')` 取消息 —— 那是**字典键
    不是属性**，永远取到空串，日志里只剩「点赞失败:」后面什么都没有，
    根本看不出为什么失败。
    """
    if isinstance(r, dict):
        return (r.get("status") == "成功"), str(r.get("message") or "")
    return bool(getattr(r, "is_success", False)), str(getattr(r, "message", "") or "")


def do_interact(moment, post: dict, act: dict, dry_run: bool = False) -> dict:
    """对一条动态执行点赞/评论。返回 {ok, like, comment, err}。

    `moment` 是 `wechatauto.Moment` 实例。它内部会打开朋友圈窗口、
    滚动找到这个人再点 —— 所以是**会动界面的**操作。
    """
    who = post.get("nickname") or post.get("username") or ""
    txt = (post.get("text") or "").strip()
    # 定位条件：优先用「昵称 + 正文关键词」，正文太短就只用昵称。
    # 只用昵称会有歧义（同一个人一天发好几条），加了关键词才定位得准。
    kw = txt[:12] if len(txt) >= 2 else None
    res = {"ok": True, "like": False, "comment": False, "err": ""}
    if dry_run:
        return res
    if act.get("like"):
        try:
            r = moment.LikeMoment(publisher=who, keyword=kw)
            res["like"], msg = _resp_ok(r)
            if not res["like"]:
                res["err"] += "点赞失败({}) ".format(msg[:40])
        except Exception as e:
            res["err"] += "点赞异常({}) ".format(type(e).__name__)
        time.sleep(1.0)
    if act.get("comment"):
        try:
            r = moment.CommentMoment(publisher=who, keyword=kw,
                                     content=act["comment"])
            res["comment"], msg = _resp_ok(r)
            if not res["comment"]:
                res["err"] += "评论失败({}) ".format(msg[:40])
        except Exception as e:
            res["err"] += "评论异常({}) ".format(type(e).__name__)
    return res


def post_key(post: dict) -> tuple:
    """把数据库里的动态转成和 UI 条目一样的匹配键："""
    return (str(post.get("nickname") or ""),
            str(post.get("text") or "")[:20])

# ──────────────────────────────────────────────────────────────
# 打开朋友圈触发同步 + 截图看图
# ──────────────────────────────────────────────────────────────

def open_and_refresh(wait: float = 3.0):
    """打开朋友圈页，**让本地 sns.db 同步一次**。

    为什么必须做：`sns.db` 是**懒加载缓存** —— 微信不在前台打开朋友圈页，
    就不会去拉新动态。实测（2026-10-18）：
        · 只读库          -> 最新还是 12:40 那条，0 条新的
        · 调 GetMoments() -> 读到了 7 条新的（20:45、21:07 的都出来了）
    所以定时任务如果只读库，6:30/12:00/21:00 三趟全是白跑，永远只看旧数据。

    返回 (Moment 实例, UI 条目列表)。失败返回 (None, [])。
    """
    try:
        from wechatauto import Moment, WeChat
    except Exception:
        return None, []
    # **先确认窗口在屏幕内、可见、没最小化。**
    # 微信有时候会莫名跑出屏幕 —— 界面还在但截不到，朋友圈也就看不到，
    # 于是误判"没有新动态"（用户 2026-10-19 反馈过）。
    try:
        _ok_vis, _why_vis = ensure_wechat_on_screen()
        if not _ok_vis:
            import logging as _lg
            _lg.getLogger("brain").warning("[朋友圈] 窗口不可用：%s", _why_vis)
    except Exception:
        pass
    # **先把微信置顶再干活。**不置前的话第一次点击只会激活窗口被吞掉。
    try:
        bring_wechat_front()
    except Exception:
        pass
    try:
        # **Moment 需要 WeChat 实例**，无参构造会 TypeError（踩过）
        mo = Moment(WeChat())
    except Exception:
        return None, []
    try:
        items = mo.GetMoments()
    except Exception:
        return None, []
    # **点一下右上角的「刷新」**（用户要求），确保拿到最新动态
    try:
        click_refresh()
    except Exception:
        pass
    if wait:
        import time as _t
        _t.sleep(wait)
    return mo, (items or [])


def capture_images(mo, items, save_dir, describe_path, want_keys=None) -> dict:
    """把带图的动态**截下来**，交给视觉模型描述。

    为什么必须截图：朋友圈图片拿不到数据 ——
      · 本地 sns.db 只有 md5 和 URL，没有图
      · CDN 直连返回 HTTP 400（要 Referer/token）
      · 微信本地图片缓存里也没有
    **唯一能看到图的地方就是渲染出来的界面。**所以直接截那一条的屏幕区域。

    参数：
      save_dir      截图存哪
      describe_path 回调，签名 (pathlib.Path) -> str

    返回 {匹配键: 图片描述}，匹配键 = (昵称, 正文前 20 字)。
    """
    out = {}
    if mo is None or not items:
        return out
    try:
        from PIL import ImageGrab
    except Exception:
        return out
    import pathlib as _pl
    save = _pl.Path(save_dir)
    try:
        save.mkdir(parents=True, exist_ok=True)
    except Exception:
        return out

    n_ok = 0
    for idx, it in enumerate(items):
        try:
            # **必须先读 publisher / text 属性**：MomentItem 是懒解析的，
            # 直接读 nickname / content / image_count 会全是空 ——
            # 实测一开始就是这里踩的坑：UI 明明有 2 条动态，
            # 读出来 nickname='' image_count=0，于是一张图都没截。
            _who = str(getattr(it, "publisher", "") or "")
            _txt = str(getattr(it, "text", "") or "")
            # **不用 image_count**：实测它经常是 0（明明有图），
            # 所以改由"数据库里这条有图"来驱动（want_keys），更可靠。
            if want_keys is not None and (_who, _txt[:20]) not in want_keys:
                continue
            ctrl = getattr(it, "control", None)
            if ctrl is None:
                continue
            r = ctrl.BoundingRectangle
            w = int(r.right - r.left)
            h = int(r.bottom - r.top)
            # 尺寸离谱就别截了（滚动中/未渲染完会给出怪值）
            if w < 120 or h < 60 or w > 2000 or h > 3000:
                continue
            img = ImageGrab.grab(
                bbox=(int(r.left), int(r.top), int(r.right), int(r.bottom)),
                all_screens=True)
            if img is None:
                continue
            fp = save / "moment_{}.png".format(idx)
            img.save(str(fp))
            desc = describe_path(fp)
            if not desc:
                continue
            out[(_who, _txt[:20])] = desc
            n_ok += 1
            if n_ok >= 8:          # 一轮最多看 8 张，别烧太多额度
                break
        except Exception:
            continue
    return out

def find_item(items, post: dict, used: set = None):
    """在 UI 条目里找到和这条动态对应的那个。

    为什么不用 `LikeMoment(publisher=...)`：那个会**重新滚动定位**，
    实测在截图之后窗口失焦的状态下经常报「未能打开朋友圈操作菜单」。
    而 `GetMoments()` 已经返回了条目对象，直接 `Like(item)` 稳得多 ——
    手动单独测 `LikeMoment` 能成功、放进流程里就失败，差别就在这。
    """
    who = str(post.get("nickname") or "")
    txt = str(post.get("text") or "").strip()
    if not items:
        return None

    # **`used`：已经处理过的 UI 条目，别再返回第二次**（2026-11-13）
    #
    # 为什么会需要：纯图片动态的 Name 是 `'四季亦然 包含1张图片 1分钟前'`，
    # 同一个人连发两条纯图，Name **完全一样** —— 按 Name 匹配永远命中第一条，
    # 于是第二条的处理全落到第一条上：同一条被点两次赞、评论也发到同一条。
    # 有了 used，调用方按处理顺序传进来，就能做到「第 1 条找第 1 个、
    # 第 2 条找第 2 个」。
    _used = used if used is not None else set()

    def _cand(items_iter):
        for _it in items_iter:
            if id(_it) in _used:
                continue
            yield _it

    # **判据换成 control.Name（2026-11-13 实测发现的关键）**
    #
    # 原来用 `it.publisher` 匹配 —— 但实测微信 4.2 里 UI 条目的
    # `publisher` / `nickname` 都是**空字符串**！
    #     publisher='' nickname='' pid=10408（所有条目都一样）
    # 而 `control.Name` 是完整的：
    #     '澜 别着急，一人一枪头 视频 2天前 '
    #     '四季亦然 sid是个人物 包含9张图片 2天前 '
    # 所以必须拿 Name 去配，否则永远匹配不上 -> 退化成乱配 ->
    # 用户看到的「同一条点两次赞」「评论跑到别人朋友圈下面」。
    def _ui_name(it) -> str:
        try:
            c = getattr(it, "control", None)
            return str(getattr(c, "Name", "") or "")
        except Exception:
            return ""

    # ① 最可靠：Name 里同时含 昵称 和 正文前 10 字
    if who and txt:
        for it in _cand(items):
            nm = _ui_name(it)
            if nm and who in nm and txt[:10] in nm:
                return it

    # ② 正文为空（纯图片/视频动态）：Name 含昵称 + 与动态类型有关的词
    if who and not txt:
        for it in _cand(items):
            nm = _ui_name(it)
            if nm and who in nm:
                return it

    # ③ 退路：只用昵称（Name 里含）
    if who:
        same = [it for it in _cand(items) if who in _ui_name(it)]
        if len(same) == 1:
            return same[0]

    # ④ 最后退路：原来的 publisher 匹配（老版微信/别的版本可能有用）
    for it in _cand(items):
        try:
            if (str(getattr(it, "publisher", "") or "") != who):
                continue
            it_txt = str(getattr(it, "text", "") or "").strip()
            if txt and it_txt and it_txt[:10] == txt[:10]:
                return it
        except Exception:
            continue
    return None


def do_interact_item(mo, item, act: dict, dry_run: bool = False) -> dict:
    """对**已拿到的 UI 条目**点赞/评论（不重新定位，最稳）。"""
    res = {"ok": True, "like": False, "comment": False, "err": ""}
    if dry_run or item is None:
        return res
    if act.get("like"):
        try:
            r = mo.Like(item)
            res["like"], msg = _resp_ok(r)
            if not res["like"]:
                res["err"] += "点赞失败({}) ".format(msg[:40])
        except Exception as e:
            res["err"] += "点赞异常({}) ".format(type(e).__name__)
        time.sleep(1.0)
    if act.get("comment"):
        try:
            r = mo.Comment(item, act["comment"])
            res["comment"], msg = _resp_ok(r)
            if not res["comment"]:
                res["err"] += "评论失败({}) ".format(msg[:40])
        except Exception as e:
            res["err"] += "评论异常({}) ".format(type(e).__name__)
    return res

def _click_more_and_float(mo, item, timeout: float = 2.0) -> bool:
    """自己点「…」并确认浮层弹出。**不用库里的 _locate_more_click。**

    为什么绕开：`mo.Like(item)` 内部走 `_locate_more_click`，实测稳定报
    「未能打开朋友圈操作菜单」（9.7 秒白跑）。但我手动一步步做是**能弹出**的：
        _find_more_button(item) -> (1546,185)
        点击后鼠标**留在原地** -> _find_float_button('评论') 找到了按钮

    两个关键点：
      ① **点完不能把鼠标移开** —— 微信这个浮层是 hover 才保持的，
         手一移开就消失（这个坑我踩了：先挪鼠标再验证，永远验证不到）
      ② 两次点击之间要停 0.6 秒以上，太快会被吞
    """
    try:
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
    except Exception:
        return False
    pt = find_more_button_any(mo, item)
    if not pt:
        return False
    try:
        pyautogui.moveTo(pt[0], pt[1], duration=0.15)
        _t.sleep(0.3)
        pyautogui.mouseDown()
        _t.sleep(0.06)
        pyautogui.mouseUp()
    except Exception:
        return False
    # **鼠标留在这儿**，别动
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        _t.sleep(0.4)
        for label in ("赞", "评论"):
            try:
                if mo._find_float_button(label, timeout=0.5) is not None:
                    return True
            except Exception:
                pass
    return False


def _click_float(mo, label: str) -> bool:
    """点浮层里的「赞」或「评论」。"""
    try:
        btn = mo._find_float_button(label, timeout=1.5)
    except Exception:
        return False
    if btn is None:
        return False
    try:
        r = btn.BoundingRectangle
        x = int((r.left + r.right) / 2)
        y = int((r.top + r.bottom) / 2)
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(x, y, duration=0.12)
        _t.sleep(0.25)
        pyautogui.mouseDown()
        _t.sleep(0.06)
        pyautogui.mouseUp()
        _t.sleep(0.8)
        return True
    except Exception:
        return False


def do_interact_manual(mo, item, act: dict, dry_run: bool = False) -> dict:
    """**自己实现**的点赞/评论：点「…」-> 点浮层里的「赞」/「评论」。"""
    res = {"ok": True, "like": False, "comment": False, "err": ""}
    if dry_run or item is None:
        return res
    need_like = bool(act.get("like"))
    need_cmt = bool(act.get("comment"))
    if not need_like and not need_cmt:
        return res

    if not _click_more_and_float(mo, item):
        res["err"] += "点不开「…」浮层 "
        return res

    if need_like:
        if _click_float(mo, "赞"):
            res["like"] = True
        else:
            res["err"] += "点不到「赞」 "
        time.sleep(1.2)

    if need_cmt:
        # 点完赞浮层可能收起了，重新弹一次
        if not _click_more_and_float(mo, item):
            res["err"] += "评论时浮层弹不出来 "
        elif _click_float(mo, "评论"):
            time.sleep(1.0)
            # 输入框：粘贴 + 找「发送」
            try:
                ok = mo._comment_input_focus()
            except Exception:
                ok = False
            if not ok:
                res["err"] += "评论框没聚焦 "
            else:
                try:
                    import pyperclip
                    pyperclip.copy(act["comment"])
                    import pyautogui
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.8)
                    if _click_float(mo, "发送"):
                        res["comment"] = True
                    else:
                        res["err"] += "点不到「发送」 "
                except Exception as e:
                    res["err"] += "输入失败({}) ".format(type(e).__name__)
        else:
            res["err"] += "点不到「评论」 "
    return res

def find_more_by_pixels(item):
    """**用像素找「…」按钮**，不用模板匹配。

    为什么要有这个：库里的 `_find_more_button` 靠模板匹配
    （`assets/moments_more_light.png`），实测**对带图的动态（高的 cell）
    稳定返回 None**，悬停/扩大搜索区域/换 region 都不行。
    而纯文字动态（矮的 cell）能正常找到 —— 所以问题出在模板对不上，
    不是坐标算错。

    实测出来的真实位置（2026-10-18，1200x440 的 cell）：
        「…」三个点在 cell 坐标 x=892~907, y=426~430
        颜色 RGB(108,126,162) 灰蓝
    也就是**右下角、时间戳的右边**。

    做法：截 cell → 在右下角区域找"有饱和度的彩色像素" → 取质心。
    背景是接近白的 (250,250,250)，所以只要找有色差的像素就够。

    返回屏幕坐标 (x, y) 或 None。
    """
    try:
        from PIL import ImageGrab
    except Exception:
        return None
    try:
        br = item.control.BoundingRectangle
    except Exception:
        return None
    left, top = int(br.left), int(br.top)
    w = int(br.right - br.left)
    h = int(br.bottom - br.top)
    if w < 100 or h < 40:
        return None
    try:
        img = ImageGrab.grab(bbox=(left, top, left + w, top + h),
                             all_screens=True).convert("RGB")
    except Exception:
        return None
    px = img.load()

    # 只在右下角找：x 55%~100%，y 80%~100%
    x0, x1 = int(w * 0.55), w
    y0, y1 = int(h * 0.80), h
    pts = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            mx = max(r, g, b)
            mn = min(r, g, b)
            # 有饱和度 + 不太暗 = 图标（背景是 250,250,250 近似纯灰）
            if mx - mn > 25 and mx > 100:
                pts.append((x, y))
    if len(pts) < 8:
        return None

    # 取最靠右下的一簇（避免把正文里带颜色的字也当按钮）
    pts.sort(key=lambda q: (q[1], q[0]))
    # 用中位数附近 40px 内的点算质心
    my = pts[-1][1]
    mxx = max(p[0] for p in pts if abs(p[1] - my) <= 4)
    cluster = [q for q in pts
               if abs(q[1] - my) <= 6 and abs(q[0] - mxx) <= 40]
    if len(cluster) < 5:
        cluster = pts[-30:]
    cx = sum(q[0] for q in cluster) / len(cluster)
    cy = sum(q[1] for q in cluster) / len(cluster)
    return (int(left + cx), int(top + cy))


def find_more_button_any(mo, item):
    """先试库的模板匹配，不行再用像素定位。"""
    try:
        pt = mo._find_more_button(item)
        if pt:
            return pt
    except Exception:
        pass
    return find_more_by_pixels(item)

# ──────────────────────────────────────────────────────────────
# 真正能用的点赞/评论（2026-10-18 深夜实测通过）
# ──────────────────────────────────────────────────────────────
#
# 踩了一整晚才把这条链路走通，记下来免得重走：
#
# ① 「…」按钮**只能靠像素定位**。库里的模板匹配对带图动态（高 cell）经常返回
#    None；而像素法在「cell 右下角、时间戳右边」找灰蓝色点，稳得多。
#    实测：cell 1200x440 时三个点在 (892~907, 426~430)，颜色 RGB(108,126,162)。
#
# ② 「…」**要点两下**。第一下只是激活窗口，被吞掉；第二下才真的弹出浮层。
#    这个最坑 —— 单点看起来"点到了"，其实什么都没发生。
#
# ③ 浮层是**深色圆角块**，出现在「…」左边。用"找暗像素"定位它，
#    然后按比例点：左 22% 是「赞」，左 74% 是「评论」。
#    不要用 mo._find_float_button() —— 那个在 UIA 树里一直有隐藏元素，
#    浮层没开也会返回 FOUND（假阳性，我信了它一整晚）。
#
# ④ 点完**不要把鼠标挪开**，浮层是 hover 保持的。


def _float_menu_by_pixels(more_pt, screen_h: int = 1080) -> dict:
    """在「…」左边找那个深色浮层，返回 {'赞':(x,y), '评论':(x,y), '_rect':...}。"""
    try:
        from PIL import ImageGrab
    except Exception:
        return {}
    x0 = max(0, more_pt[0] - 300)
    y0 = max(0, more_pt[1] - 60)
    x1 = min(9999, more_pt[0] + 10)
    y1 = min(screen_h, more_pt[1] + 60)
    try:
        img = ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True).convert("RGB")
    except Exception:
        return {}
    px = img.load()
    W, H = img.size
    dark = []
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            if (r + g + b) / 3 < 110:
                dark.append((x, y))
    if len(dark) < 200:
        return {}
    xs = [d[0] for d in dark]
    ys = [d[1] for d in dark]
    l, r = min(xs), max(xs)
    t, b = min(ys), max(ys)
    # 太扁/太窄不是浮层
    if (r - l) < 120 or (b - t) < 25:
        return {}
    ymid = y0 + (t + b) // 2
    return {
        "赞": (x0 + l + int((r - l) * 0.22), ymid),
        "评论": (x0 + l + int((r - l) * 0.74), ymid),
        "_rect": (x0 + l, y0 + t, x0 + r, y0 + b),
    }


def open_float_menu(mo, item, more_pt=None) -> dict:
    """点开「…」浮层，返回按钮坐标字典（空 = 失败）。"""
    try:
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
    except Exception:
        return {}
    if more_pt is None:
        more_pt = find_more_button_any(mo, item)
    if not more_pt:
        return {}
    # **点两下**：第一下激活窗口会被吞，第二下才生效
    for _ in range(2):
        pyautogui.moveTo(more_pt[0], more_pt[1], duration=0.2)
        _t.sleep(0.5)
        pyautogui.leftClick()
        _t.sleep(1.2)
    return _float_menu_by_pixels(more_pt)


def click_at(pt) -> bool:
    try:
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(pt[0], pt[1], duration=0.2)
        _t.sleep(0.4)
        pyautogui.leftClick()
        return True
    except Exception:
        return False


def like_or_comment(mo, item, act: dict, dry_run: bool = False) -> dict:
    """**实测可用的**点赞/评论：像素定位 + 双击「…」+ 点浮层。"""
    res = {"ok": True, "like": False, "comment": False, "err": ""}
    if dry_run or item is None:
        return res
    need_like = bool(act.get("like"))
    need_cmt = bool(act.get("comment"))
    if not need_like and not need_cmt:
        return res

    more_pt = find_more_button_any(mo, item)
    if not more_pt:
        res["err"] += "定位不到「…」 "
        return res

    menu = open_float_menu(mo, item, more_pt)
    if not menu:
        res["err"] += "浮层没弹出 "
        return res

    if need_like:
        if click_at(menu["赞"]):
            res["like"] = True
        else:
            res["err"] += "点不到「赞」 "
        time.sleep(1.5)

    if need_cmt:
        # 点完赞浮层会收起来，重新弹
        menu2 = open_float_menu(mo, item, find_more_button_any(mo, item))
        if not menu2:
            res["err"] += "评论时浮层弹不出来 "
            return res
        if not click_at(menu2["评论"]):
            res["err"] += "点不到「评论」 "
            return res
        time.sleep(1.2)
        try:
            import pyperclip
            import pyautogui
            pyperclip.copy(act["comment"])
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.8)
            # **必须点「发送」按钮，回车不生效**（用户实测指出）
            _send_pt = find_send_button()
            if _send_pt:
                pyautogui.moveTo(_send_pt[0], _send_pt[1], duration=0.2)
                time.sleep(0.3)
                pyautogui.leftClick()
                time.sleep(1.2)
                res["comment"] = True
            else:
                res["err"] += "找不到「发送」按钮 "
        except Exception as e:
            res["err"] += "输入失败({}) ".format(type(e).__name__)
    return res

def find_comment_cell():
    """找评论区那个控件（mmui::TimelineCommentCell），返回它的矩形或 None。"""
    try:
        from wechatauto import WeChatGUI
        win = getattr(WeChatGUI()._get_uia(), "_win", None)
    except Exception:
        return None
    found = []

    def walk(c, d=0):
        if d > 26 or found:
            return
        try:
            for ch in c.GetChildren():
                try:
                    if "TimelineCommentCell" in (ch.ClassName or ""):
                        found.append(ch)
                        return
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not found:
        return None
    try:
        r = found[0].BoundingRectangle
        if r.right - r.left < 200 or r.bottom - r.top < 60:
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def find_send_button(region=None) -> tuple:
    """找评论框右下角的「发送」按钮，返回屏幕坐标或 None。

    **评论必须点这个按钮，按回车不生效。**（用户实测指出）

    **不能用屏幕比例硬算** —— 踩过（2026-10-19）：原来用
    (sw*0.60 + 0.22*sw*0.72, sh*0.88 + 0.10*sh*0.52) 这种屏幕百分比估，
    窗口一挪位置就点偏，用户反馈"她朋友圈发送评论会点错位置"。

    现在：**先找评论区的 UIA 控件 mmui::TimelineCommentCell**，
    再按相对它的比例算按钮位置。实测（1920x1080，窗口在 (207,30)）：
        评论框 (668,882,1868,1042)，发送按钮中心 (1524,1008)
        -> 相对 x 71.3%，相对 y 78.8%
    """
    # ① **优先用 UIA 找真正的「发送」按钮控件**（2026-11-10 加）。
    #    以前只用评论区比例估算，窗口位置/DPI 一变就点偏 ——
    #    用户反馈"朋友圈发送按钮定位错地方"。
    try:
        from wechatauto import WeChatGUI
        win = getattr(WeChatGUI()._get_uia(), "_win", None)
        if win is not None:
            hits = []

            def _walk(c, d=0):
                if d > 24 or hits:
                    return
                try:
                    for ch in c.GetChildren():
                        try:
                            ct = ch.ControlTypeName
                            nm = (ch.Name or "").strip()
                            if ct == "ButtonControl" and nm in ("发送", "发送评论", "评论"):
                                rr = ch.BoundingRectangle
                                if rr.right - rr.left > 10 and rr.bottom - rr.top > 10:
                                    hits.append(rr)
                                    return
                        except Exception:
                            pass
                        _walk(ch, d + 1)
                except Exception:
                    pass

            _walk(win)
            if hits:
                rr = hits[0]
                return (int((rr.left + rr.right) / 2),
                        int((rr.top + rr.bottom) / 2))
    except Exception:
        pass

    # ② 退回：评论区比例
    cell = find_comment_cell() if region is None else None
    if cell:
        l, tp, r, b = cell
        return (int(l + (r - l) * 0.715), int(tp + (b - tp) * 0.79))
    if region is None:
        return None          # 找不到评论框就**不猜**，宁可失败也不点错地方
    try:
        return (int(region[0] + (region[2] - region[0]) * 0.72),
                int(region[1] + (region[3] - region[1]) * 0.52))
    except Exception:
        return None


def send_comment(comment: str, send_pt=None) -> bool:
    """输入评论并**点「发送」按钮**发出去（回车不管用）。"""
    try:
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
    except Exception:
        return False
    # 输入：粘贴最稳（微信自绘输入框，直接打字进不去）
    try:
        import pyperclip
        pyperclip.copy(comment)
        _t.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        _t.sleep(0.9)
    except Exception:
        return False
    pt = send_pt or find_send_button()
    if not pt:
        return False
    try:
        pyautogui.moveTo(pt[0], pt[1], duration=0.2)
        _t.sleep(0.3)
        pyautogui.leftClick()
        _t.sleep(1.2)
        # 点完检查输入框是否清空（清空 = 发出去了）
        return True
    except Exception:
        return False


def already_liked(post: dict, my_nick: str) -> bool:
    """这条我是不是已经赞过了。

    **「赞」是开关式的** —— 已经赞过再点一次就取消了。
    用户实测踩到过：以为在点赞，其实是把自己之前的赞撤了。
    所以点之前必须先判断。
    """
    if not my_nick:
        return False
    for l in (post.get("likes") or []):
        try:
            if str(l.get("nickname") or "") == my_nick:
                return True
            if str(l.get("username") or "") == post.get("_my_wxid"):
                return True
        except Exception:
            continue
    return False

# ──────────────────────────────────────────────────────────────
# 把微信窗口置顶（点击前必须做）
# ──────────────────────────────────────────────────────────────

def bring_wechat_front() -> bool:
    """把微信主窗口放到最上层并激活。

    **为什么必须做**：微信在后台时，第一次点击只会激活窗口、被吞掉，
    要点两下才生效。而且浮层是 hover 保持的，窗口不在前台时
    鼠标悬停也未必渲染得出来。
    实测（2026-10-18）：不置前时「…」经常定位不到 / 点了没反应；
    置前之后稳定得多。
    """
    try:
        import win32gui
        import ctypes
        import time as _t
    except Exception:
        return False
    hits = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls.startswith("Qt") and "微信" in title:
                hits.append(h)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return False
    if not hits:
        return False
    hwnd = hits[0]
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)      # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        _t.sleep(0.7)
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return False

def click_refresh(gui=None) -> bool:
    """点朋友圈右上角的「刷新」按钮。

    用户要求：每次看朋友圈前先点一下刷新，确保拿到最新动态。

    实测（2026-10-18）：这个按钮是 UIA 能直接定位的 ——
        name='刷新'  cls=mmui::XTabBarItem  rect=(1637,85,1687,120)
    不用像素猜，直接找控件点击。
    备注：旁边还有 name='消息'（铃铛）和 name='发表'（相机），别点错。
    """
    try:
        import time as _t
    except Exception:
        return False
    try:
        if gui is None:
            from wechatauto import WeChatGUI
            gui = WeChatGUI()
        win = getattr(gui._get_uia(), "_win", None)
    except Exception:
        return False
    if win is None:
        return False

    found = []

    def walk(c, d=0):
        if d > 24 or len(found) > 3:
            return
        try:
            for ch in c.GetChildren():
                try:
                    if (ch.Name or "").strip() == "刷新":
                        found.append(ch)
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not found:
        return False
    btn = found[0]
    # 先试 UIA 的 Click，不行再按坐标点
    try:
        btn.Click()
        _t.sleep(1.5)
        return True
    except Exception:
        pass
    try:
        r = btn.BoundingRectangle
        x = int((r.left + r.right) / 2)
        y = int((r.top + r.bottom) / 2)
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(x, y, duration=0.15)
        _t.sleep(0.3)
        pyautogui.leftClick()
        _t.sleep(1.5)
        return True
    except Exception:
        return False


def _scroll_point(rect) -> tuple:
    """滚轮该落在哪个坐标。

    **不能用列表中心**（2026-11-14 实测）：朋友圈每条动态的图片、
    正文、视频都是独立控件，鼠标落在中心多半压在图片区上，
    滚轮事件被内容控件吃掉，外面的滚动容器收不到 —— 界面一动不动。
    实测左侧边缘（头像右侧那条窄缝）最稳，右侧边缘也行。
    """
    l, t, r, b = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    x = l + 30
    y = (t + b) // 2
    return (x, y)


def scroll_to_top(times: int = 20, list_rect=None,
                  allow_fallback: bool = False) -> None:
    """把**朋友圈列表**滚到最顶部，让最新动态渲染出来。

    **必须滚**：朋友圈是虚拟滚动，没渲染出来的条目在 UIA 树里
    rect 全是 (0,0,0,0)，截出来的图是黑的（也因此读不到最新动态）。

    踩过的坑（2026-10-19）：原来用**硬编码坐标 (1200,300)** 当滚轮落点。
    如果那一刻朋友圈还没打开（微信停在聊天页），滚的就是**用户的聊天窗口** ——
    用户反馈"她为什么在我的聊天界面往上滑"。
    现在：
      ① 优先用传进来的 list_rect（朋友圈列表的 UIA 矩形）
      ② 没传就去找 mmui::TimeLineListView 的矩形
      ③ 都找不到**就完全不滚**，宁可看不到也别乱滚别人的窗口
    """
    try:
        import pyautogui
        import time as _t
    except Exception:
        return
    cx = cy = None
    if list_rect:
        try:
            cx, cy = _scroll_point(list_rect)
        except Exception:
            cx = cy = None
    if cx is None:
        _r = find_list_rect(allow_fallback=allow_fallback)
        if _r:
            cx, cy = _scroll_point(_r)
    if cx is None:
        r = find_list_rect(allow_fallback=allow_fallback)
        if r:
            cx, cy = _scroll_point(r)
    if cx is None:
        # 找不到朋友圈列表且不允许兜底 -> **不滚**，别乱动别人的窗口
        return
    # **滚前确保微信在前台**（和 scroll_down 同一个坑）
    try:
        bring_wechat_front()
        _t.sleep(0.15)
    except Exception:
        pass
    try:
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(cx, cy, duration=0.2)
        _t.sleep(0.2)
        for _ in range(times):
            pyautogui.scroll(600)      # 正数 = 向上
            _t.sleep(0.1)
        _t.sleep(1.0)
    except Exception:
        pass



# ══════════════════════ 朋友圈列表矩形（带兜底）══════════════════════
def _fallback_rect() -> tuple | None:
    """找不到 TimeLineListView 时的兜底：用微信窗口右侧区域。

    **只有调用方确认"当前停在朋友圈页"时才该用** ——
    否则滚的就是聊天窗口，会骚扰用户（2026-10-19 踩过）。

    微信布局：左侧会话列表约 22%，右侧是内容区。
    朋友圈列表占右侧内容区，去掉顶部标题栏和底部输入区。
    """
    try:
        from wechatauto import WeChatGUI
        gui = WeChatGUI()
        l = int(getattr(gui, "origin_x", 0) or 0)
        t = int(getattr(gui, "origin_y", 0) or 0)
        w = int(getattr(gui, "render_w", 0) or 0)
        h = int(getattr(gui, "render_h", 0) or 0)
        if w < 400 or h < 300:
            return None
        x0 = l + int(w * 0.24)
        y0 = t + int(h * 0.10)
        x1 = l + w - 30
        y1 = t + h - 90
        if x1 - x0 < 200 or y1 - y0 < 200:
            return None
        return (x0, y0, x1, y1)
    except Exception:
        return None


def find_list_rect(allow_fallback: bool = False) -> tuple | None:
    """找朋友圈列表的屏幕矩形。找不到且 allow_fallback 时用窗口区域兜底。"""
    try:
        from wechatauto import WeChatGUI
        win = getattr(WeChatGUI()._get_uia(), "_win", None)
        found = []

        def walk(c, d=0):
            if d > 24 or found:
                return
            try:
                for ch in c.GetChildren():
                    try:
                        if "TimeLineListView" in (ch.ClassName or ""):
                            found.append(ch)
                            return
                    except Exception:
                        pass
                    walk(ch, d + 1)
            except Exception:
                pass

        walk(win)
        if found:
            r = found[0].BoundingRectangle
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        pass
    return _fallback_rect() if allow_fallback else None


def warmup_scroll(rect=None, step: int = 240, allow_fallback: bool = True) -> None:
    """打开朋友圈后先小滑一段，让列表渲染稳定。

    用户反馈（2026-11-09）："切到朋友圈界面时有时候会看不到按钮"。
    原因是虚拟滚动列表刚切过来时还没渲染完，控件矩形是 (0,0,0,0)。
    先下滑一小段再判断，能逼它渲染。
    """
    try:
        import pyautogui
        import time as _t
    except Exception:
        return
    r = rect or find_list_rect(allow_fallback=allow_fallback)
    if not r:
        return
    try:
        bring_wechat_front()
        _t.sleep(0.15)
    except Exception:
        pass
    try:
        cx, cy = _scroll_point(r)
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(cx, cy, duration=0.15)
        _t.sleep(0.15)
        pyautogui.scroll(-int(step))
        _t.sleep(0.6)
        # 滑完回顶，保证后面从最新一条开始
        for _ in range(max(3, int(step / 100))):
            pyautogui.scroll(400)
            _t.sleep(0.05)
        _t.sleep(0.5)
    except Exception:
        pass




# ══════════════════════ 向下滑固定路程 / 滑到目标条目 ══════════════════════
def scroll_down(step: int = 600, list_rect=None,
                allow_fallback: bool = True) -> bool:
    """在朋友圈列表里向下滚一段。返回是否真的滚了。"""
    try:
        import pyautogui
        import time as _t
    except Exception:
        return False
    r = list_rect or find_list_rect(allow_fallback=allow_fallback)
    if not r:
        return False
    # **滚动前必须让微信在前台。**
    # 实测（2026-11-13）：窗口失焦时 pyautogui.scroll 的滚轮事件会被吞，
    # 界面一动不动，但本函数照样 return True —— 上层以为滑了，其实没滑。
    # 表现就是「连续两段没有新条目，滑到底了」提前结束。
    try:
        bring_wechat_front()
        _t.sleep(0.15)
    except Exception:
        pass
    try:
        cx, cy = _scroll_point(r)
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(cx, cy, duration=0.15)
        _t.sleep(0.1)
        pyautogui.scroll(-int(step))       # 负数 = 向下
        _t.sleep(0.5)
        return True
    except Exception:
        return False



def _ui_names_sig(mo) -> tuple:
    """当前 UI 条目的指纹（名字序列），用来判断滚动有没有真的生效。"""
    try:
        items = mo.GetMoments() or []
    except Exception:
        return ()
    out = []
    for it in items:
        try:
            c = getattr(it, "control", None)
            out.append(str(getattr(c, "Name", "") or ""))
        except Exception:
            out.append("")
    return tuple(out)


def _ui_nicks(mo) -> set:
    """当前 UI 里出现的所有「昵称」（取 Name 的第一个空格前片段）。

    比整串 Name 稳：同一条动态的 Name 会因为图片加载完成从
    「包含0张图片」变成「包含1张图片」，那是假变化 —— 用它判断滚动
    会误以为动了。昵称集合变了才是真的翻页。
    """
    out = set()
    for nm in _ui_names_sig(mo):
        head = nm.split(" ")[0].strip() if nm else ""
        if head:
            out.add(head)
    return out


def scroll_down_verified(mo, step: int = 900, list_rect=None,
                         allow_fallback: bool = True, tries: int = 3) -> bool:
    """在朋友圈列表里向下滚，**并验证内容真的变了**。

    为什么要验证：实测微信 4.2 上有时候滚轮事件被吞，界面一动不动，
    但旧版 scroll_down 照样 return True —— 上层以为滑了，然后报
    「连续三段没有新条目，滑到底了」提前收工，下面的动态永远处理不到。

    返回 True = 内容确实变了；False = 试了 tries 轮都没滚动。
    """
    try:
        import time as _t
    except Exception:
        return False

    r = list_rect or find_list_rect(allow_fallback=allow_fallback)
    if not r:
        return False
    cx, cy = _scroll_point(r)

    try:
        bring_wechat_front()
        _t.sleep(0.2)
    except Exception:
        pass

    before = _ui_nicks(mo)

    for attempt in range(tries):
        try:
            # 方式 A：mouse_event 滚轮（比 pyautogui 更底层，grid 数可调）
            # 注意：不要点击 —— 左边就是头像，点一下会打开详情页。
            import ctypes
            u = ctypes.windll.user32
            u.SetCursorPos(cx, cy)
            _t.sleep(0.12)
            # **先向上滚 3 格"离开顶部"**（2026-11-14 实测）。
            # 朋友圈在最顶部时，向下的滚轮会被识别成「下拉刷新」而不是滚动，
            # 列表一动不动 —— 这是"滚不动"的主因。先往上蹭两格跳出顶部就好了。
            if attempt == 0:
                for _g in range(3):
                    u.mouse_event(0x0800, 0, 0, 120, 0)   # 向上
                    _t.sleep(0.05)
                _t.sleep(0.35)
            grids = max(1, int(step) // 120) * (attempt + 1)
            for _g in range(grids):
                u.mouse_event(0x0800, 0, 0, -120, 0)   # 向下
                _t.sleep(0.05)
            _t.sleep(2.2)     # 等渲染（朋友圈是懒渲染，1.4s 不够）

            after = _ui_nicks(mo)
            if after - before:
                return True

            # 方式 B：pyautogui（部分环境下它更管用）
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                pyautogui.moveTo(cx, cy, duration=0.15)
                _t.sleep(0.1)
                pyautogui.scroll(-int(step) * (attempt + 1))
                _t.sleep(2.2)
                after = _ui_nicks(mo)
                if after - before:
                    return True
            except Exception:
                pass
        except Exception:
            continue

    return False

def scroll_until_item(mo, post, max_rounds: int = 10, step: int = 600,
                      list_rect=None, allow_fallback: bool = True,
                      settle: float = 0.8) -> tuple:
    """往下滑，直到界面渲染出目标动态。

    用户要求（2026-11-09）：
      · 每次向下滑动**固定路程**
      · 滑动后再找 / 再回复
      · 切过来时看不到按钮 -> 先滑一小段再判断

    返回 (item, mo)。找不到返回 (None, mo)。
    注意：**不自动回顶** —— 由调用方决定起点。按时间倒序（最新在上），
    从顶部开始逐条往下找，方向是一致的。
    """
    import time as _t
    items = []
    for i in range(max(1, int(max_rounds))):
        try:
            items = mo.GetMoments() or []
        except Exception:
            items = []
        it = find_item(items, post)
        if it is not None:
            if i:
                try:
                    import logging as _lg
                    _lg.getLogger("brain").info(
                        "[朋友圈] 滑了 %d 段找到了目标条目", i)
                except Exception:
                    pass
            return it, mo
        # 没找到 -> 往下滑一段再试
        if not scroll_down(step=step, list_rect=list_rect,
                           allow_fallback=allow_fallback):
            break
        _t.sleep(settle)
    return None, mo


def parse_target_person(text: str) -> str:
    """从「去看看XX的朋友圈」里提取 XX。提不出来返回空串。

    实测用户说法："看一下四季亦然的朋友圈"、"去看看澜"、
    "帮我看看鼬神最近发了啥"。这里做的是**尽量宽**地抠名字，
    抠不出来就当没指定（看全部）。
    """
    import re as _re
    t = (text or "").strip()
    if not t:
        return ""
    # 先去掉尾部的语气词
    t = _re.sub(r"[的了吧呢啊哦呀，。！？~～\s]+$", "", t)
    pats = [
        r"(?:看看|看一下|看一看|瞧瞧|瞅瞅|翻翻|刷一下)(.{1,12}?)的?朋友圈",
        r"(.{1,12}?)的?朋友圈(?:发了啥|怎么样|有啥|有什么)",
        r"(?:看看|看一下|去看看|帮我看看)(.{1,12})$",
    ]
    for p in pats:
        m = _re.search(p, t)
        if not m:
            continue
        name = m.group(1).strip()
        # 去掉动词残留和标点
        name = _re.sub(r"^(一下|看看|看一眼|去|帮我|给我)", "", name).strip()
        name = _re.sub(r"[的了吧呢啊哦呀，。！？~～\s]+$", "", name).strip()
        # 去掉尾部"最近发了啥"这类没说清楚的尾巴
        name = _re.sub(r"(最近|最近发了啥|最近怎么样|发了啥|怎么样|有啥|有什么|干嘛呢).*$",
                       "", name).strip()
        if 1 <= len(name) <= 12 and name not in ("朋友圈", "朋友", "他", "她"):
            return name
    return ""


def match_person(post: dict, name: str) -> bool:
    """这条动态是不是 name 这个人发的。"""
    if not name:
        return True
    who = str(post.get("nickname") or "")
    if who == name:
        return True
    return bool(who) and (name in who or who in name)



# ══════════════════════ 唤起微信（托盘 / 最小化 / 未启动）══════════════════════
def _find_wechat_exe() -> str:
    """找 WeChat.exe 路径：先查注册表，再试常见安装位置。"""
    import os
    # ① 注册表（腾讯官方的 InstallPath）
    try:
        import winreg
        for root, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\\Tencent\\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Tencent\\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\\WOW6432Node\\Tencent\\WeChat"),
        ):
            try:
                with winreg.OpenKey(root, key) as k:
                    for name in ("InstallPath", "InstallPathW"):
                        try:
                            p = winreg.QueryValueEx(k, name)[0]
                            exe = os.path.join(p, "WeChat.exe")
                            if os.path.isfile(exe):
                                return exe
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    # ② 从**正在运行的微信进程**反查 exe 路径（最可靠）
    try:
        import win32gui
        import win32process

        def cb(h, _):
            try:
                cls = win32gui.GetClassName(h) or ""
                if cls == "WeChatMainWndForPC" or cls.startswith("Qt"):
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    cb.pids.add(pid)
            except Exception:
                pass

        cb.pids = set()
        win32gui.EnumWindows(cb, None)
        for pid in list(cb.pids):
            try:
                import win32api
                import win32con
                hproc = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                    False, pid)
                try:
                    exe = win32process.GetModuleFileNameEx(hproc, 0)
                finally:
                    try:
                        win32api.CloseHandle(hproc)
                    except Exception:
                        pass
                if exe and exe.lower().endswith(".exe") and os.path.isfile(exe):
                    return exe
            except Exception:
                continue
    except Exception:
        pass

    # ③ 常见路径
    for p in (
        r"C:\Program Files\\Tencent\\WeChat\\WeChat.exe",
        r"C:\Program Files (x86)\\Tencent\\WeChat\\WeChat.exe",
        r"C:\Program Files\\Tencent\\Weixin\\Weixin.exe",
        r"C:\Program Files (x86)\\Tencent\\Weixin\\Weixin.exe",
    ):
        try:
            if os.path.isfile(p):
                return p
        except Exception:
            pass
    return ""


def wake_wechat(timeout: float = 6.0) -> tuple:
    """唤起微信窗口。返回 (ok, 说明)。

    覆盖三种情况：
      · 在托盘里（窗口隐藏）  -> ShowWindow(SW_SHOW)
      · 最小化了              -> SW_RESTORE
      · 根本没启动            -> 启动 WeChat.exe 并等它出现
    """
    try:
        import win32gui
        import win32con
        import time as _t
    except Exception:
        return False, "缺少 win32gui，无法唤起微信"

    def _collect() -> list:
        """枚举所有窗口（**不看可见性**），挑出微信主窗口候选。"""
        hits = []

        def cb(h, _):
            try:
                cls = win32gui.GetClassName(h) or ""
                title = win32gui.GetWindowText(h) or ""
                # 微信主窗口：新版本是 Qt 类、标题含"微信"；
                # 旧版本是 WeChatMainWndForPC。也可能是 '微信' 标题的隐藏窗口。
                is_main = (cls == "WeChatMainWndForPC"
                           or (cls.startswith("Qt") and "微信" in title)
                           or (cls.startswith("Qt") and not title))
                if not is_main:
                    return
                l, t, r, b = win32gui.GetWindowRect(h)
                w, hh = r - l, b - t
                if w < 400 or hh < 300:
                    return          # 太小的多半是通知/小窗，不要
                hits.append((h, cls, title))
            except Exception:
                pass

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
        return hits

    def _show(h) -> bool:
        try:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
            elif not win32gui.IsWindowVisible(h):
                win32gui.ShowWindow(h, win32con.SW_SHOW)
            else:
                win32gui.ShowWindow(h, win32con.SW_SHOW)
            _t.sleep(0.4)
            try:
                win32gui.SetForegroundWindow(h)
            except Exception:
                # 前台窗口抢占失败时退一步：先最小化再还原
                try:
                    win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
                    _t.sleep(0.2)
                    win32gui.ShowWindow(h, win32con.SW_RESTORE)
                except Exception:
                    pass
            _t.sleep(0.4)
            return bool(win32gui.IsWindowVisible(h))
        except Exception:
            return False

    # ① 窗口存在（可能隐藏）
    hits = _collect()
    for h, cls, title in hits:
        if _show(h):
            return True, "已唤起微信（原状态：{}）".format(
                "隐藏/托盘" if not win32gui.IsWindowVisible(h) else "可见")

    # ② 没窗口 -> 微信没启动，拉起它
    exe = _find_wechat_exe()
    if not exe:
        return False, "微信没启动，也找不到 WeChat.exe 路径"
    try:
        import subprocess
        subprocess.Popen([exe])
    except Exception as e:
        return False, "启动微信失败：{}".format(e)

    # 等窗口出现
    deadline = _t.time() + max(3.0, float(timeout))
    while _t.time() < deadline:
        _t.sleep(0.8)
        for h, cls, title in _collect():
            if _show(h):
                return True, "微信未运行，已启动并唤起"
    return False, "启动了微信但等不到窗口（可能还在登录/启动中）"


def ensure_wechat_on_screen() -> tuple:
    """确保微信主窗口**在屏幕内、可见、没被最小化**。返回 (ok, 说明)。

    踩过（2026-10-19）：微信有时候会莫名跑到屏幕外（或缩到 0 尺寸），
    界面还在但截不到、朋友圈视图也看不到 —— 于是日志写"没有新动态"，
    其实是根本没看到。用户的原话："微信会莫名从屏幕上方消失导致朋友圈视图看不到"。

    这里做三件事：
      ① 最小化了就还原
      ② 坐标跑到屏幕外就挪回来
      ③ 尺寸异常（太小/为 0）就恢复成合理大小
    """
    try:
        import win32gui
        import win32con
        import ctypes
        import time as _t
    except Exception:
        return True, "缺少 win32gui，跳过检查"

    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)

    hits = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls.startswith("Qt") and "微信" in title:
                hits.append(h)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return True, "枚举窗口失败"

    if not hits:
        # **先试着唤起**（托盘 / 最小化 / 没启动）——用户要求自动拉起。
        try:
            _ok, _why = wake_wechat()
            if _ok:
                hits.clear()
                win32gui.EnumWindows(cb, None)
                if not hits:
                    return False, "{}，但重新枚举仍没窗口".format(_why)
            else:
                return False, _why
        except Exception as _e:
            return False, "找不到微信窗口且唤起失败：{}".format(_e)

    hwnd = hits[0]
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            _t.sleep(0.8)
    except Exception:
        pass

    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, hgt = r - l, b - t
        # 尺寸异常 or 完全在屏幕外 -> 重置位置和大小
        bad = (w < 400 or hgt < 300 or r <= 0 or b <= 0 or
               l >= sw or t >= sh)
        if bad:
            nw = min(1400, sw - 80)
            nh = min(900, sh - 120)
            nl = max(40, (sw - nw) // 2)
            nt = max(40, (sh - nh) // 2)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, nl, nt, nw, nh,
                                  win32con.SWP_SHOWWINDOW)
            _t.sleep(1.0)
            l2, t2, r2, b2 = win32gui.GetWindowRect(hwnd)
            return True, "窗口原来不可见，已复位到 ({},{},{},{})".format(
                l2, t2, r2, b2)
        return True, "窗口正常 ({},{},{},{})".format(l, t, r, b)
    except Exception as e:
        return True, "读窗口位置失败：{}".format(type(e).__name__)


# ──────────────────────────────────────────────────────────────
#  一遍过：滑到一条 -> 判断 -> 就地做
# ──────────────────────────────────────────────────────────────

def _ui_name(item) -> str:
    """取 UI 条目的完整描述文字。

    微信 4.2 的 Moment 条目 `publisher` / `nickname` 都是**空字符串**，
    只有 `control.Name` 是完整的：
        '澜 别着急，一人一枪头 视频 2天前 '
        '四季亦然 包含1张图片 3分钟前 '
    """
    try:
        c = getattr(item, "control", None)
        return str(getattr(c, "Name", "") or "")
    except Exception:
        return ""


def _ui_uid(item):
    """UI 条目的身份标识，用来判断"这一屏的这条是不是已经处理过"。

    用 control 的 RuntimeId —— 同一次渲染里稳定，滑动重排后仍能区分。
    拿不到就退回 python 对象 id。
    """
    try:
        c = getattr(item, "control", None)
        rid = c.GetRuntimeId()
        if rid:
            return tuple(rid)
    except Exception:
        pass
    return ("obj", id(item))


def _name_matches_post(ui_name: str, post: dict) -> bool:
    """UI 的 Name 是否对应这条数据库动态。

    Name 的组成是「昵称 + (正文前若干字 | 包含N张图片 | 视频) + 时间」。
    所以：
      · 必须以昵称开头
      · 有正文的，正文前 10 字要出现在 Name 里
      · 没正文的（纯图/纯视频），只要昵称对上就算 —— 同名多条由
        "按顺序认领"的机制保证不会错配。
    """
    who = str(post.get("nickname") or "").strip()
    if not who or not ui_name.startswith(who):
        return False
    txt = str(post.get("text") or "").strip()
    if txt:
        return txt[:10] in ui_name
    # 纯图片/纯视频：Name 里会写「包含N张图片」或「视频」
    if post.get("images"):
        return ("图片" in ui_name) or ("视频" in ui_name)
    if post.get("videos"):
        return "视频" in ui_name
    # 纯文字但空正文 / 分享卡片之类：昵称对上就算
    return True


def scan_and_interact(mo, posts, seen, *,
                      judge, do_act, on_seen, log,
                      max_rounds: int = 12, step: int = 600,
                      list_rect=None, max_per_run: int = 8,
                      allow_fallback: bool = True,
                      my_nick: str = "") -> dict:
    """一遍过：从上往下逐条「看到 -> 判断 -> 就地做」。

    参数
    ----
    mo            Moment 实例
    posts         数据库里的动态列表（时间倒序，已过滤掉自己发的）
    seen          已看过的 post_id 集合（就地修改由 on_seen 负责持久化）
    judge         callable(post, item) -> {"like": bool, "comment": str}
    do_act        callable(item, act) -> dict（点赞/评论结果）
    on_seen       callable(pid)：记录"这条看过了"并落盘
    log           callable(str)：写日志
    max_rounds    最多往下滑几段
    step          每段滑多少
    max_per_run   这一轮最多处理几条（防止一口气全做）
    my_nick       机器人自己的昵称，用于点赞判重

    返回
    ----
    {"hit_seen": bool, "processed": [post...], "liked": [...],
     "commented": [...], "errs": [...]}
    """
    out = {"hit_seen": False, "processed": [], "liked": [],
           "commented": [], "errs": []}

    # 数据库里的动态按时间倒序；同名多条靠「按顺序认领」区分。
    # **不记 UI 条目**（2026-11-13 改）：原来用 control.GetRuntimeId() 去重，
    # 但滚动后 RuntimeId 会复用 —— 新条目被误判成"已处理"，
    # 结果滑到新一屏也认领不出任何东西，表现为「连续两段没有新条目」提前结束。
    # 现在只靠 `remaining` 消费：认领过的 post 会 pop 掉，天然不会重复。
    remaining = list(posts)
    idle_rounds = 0             # 连续多少段没处理到任何新东西

    # **先确保界面真的渲染出来了**（2026-11-14）。
    # 踩过：12:00 那轮日志显示「UI 上看到 1 条」就开跑了，
    # 而实际界面能显示 3 条 —— 少读的那两条根本没机会被处理。
    # 朋友圈是懒渲染，打开后要等一会/多读几次才稳定。
    _probe = []
    for _warm_i in range(4):
        try:
            _probe = mo.GetMoments() or []
        except Exception:
            _probe = []
        if len(_probe) >= 2:
            break
        # **读不到 2 条就重建 Moment 实例**（2026-11-14 实测关键）。
        # 调用方传进来的 mo 可能是早期 open_and_refresh 建的，
        # 它缓存的 UI 节点已经陈旧 —— 实测同一个窗口：
        #   旧实例 GetMoments() -> 1 条（还是 3 天前的）
        #   新建实例 GetMoments() -> 2 条（琦琦 2分钟前、澜 2小时前）
        # 重建一次往往立刻就能看到真实的最新动态。
        if _warm_i in (0, 2):
            try:
                from wechatauto import Moment as _M2, WeChat as _W2
                _mo2 = _M2(_W2())
                _p2 = _mo2.GetMoments() or []
                if len(_p2) > len(_probe):
                    mo = _mo2
                    _probe = _p2
                    if len(_probe) >= 2:
                        break
            except Exception:
                pass
        # **只渲染出 1 条时先小滑一下逼它渲染**（2026-11-14）
        # 实测同一条朋友圈，手动调 open_and_refresh 能看到 3 条，
        # 走流程却只有 1 条 —— 懒渲染没跟上的时候必须主动推一把。
        if _warm_i == 0:
            try:
                warmup_scroll(step=240, list_rect=list_rect, allow_fallback=True)
            except Exception:
                pass
        _import_time.sleep(1.2)
    if _probe:
        log("[朋友圈] 界面已渲染 {} 条，开始逐条处理".format(len(_probe)))

    for rnd in range(max_rounds):
        if len(out["processed"]) >= max_per_run:
            log("[朋友圈] 这一轮已经处理了 {} 条，先停".format(len(out["processed"])))
            break

        try:
            items = mo.GetMoments() or []
        except Exception as e:
            log(f"[朋友圈] 读界面条目失败：{type(e).__name__}")
            break

        if not items:
            log("[朋友圈] 界面上没有渲染任何条目，停")
            break

        progressed = False
        seen_in_screen = 0        # 本屏里"已看过"的条数
        known_in_screen = 0       # 本屏里能认领到的条数
        for it in items:
            if len(out["processed"]) >= max_per_run:
                break
            nm = _ui_name(it)

            # **按顺序认领**数据库里的动态：
            # posts 和 UI 都是时间倒序，所以从前往后找第一个能对上的。
            post = None
            for idx, p in enumerate(remaining):
                if _name_matches_post(nm, p):
                    post = remaining.pop(idx)
                    break
            if post is None:
                # 广告 / 不认识的条目，跳过
                continue

            known_in_screen += 1
            pid = _post_id(post)

            # **已看过的先跳过，但不算"到边界"**（2026-11-14 改）。
            # 原来一碰到 seen 就 return —— 但第一屏往往只渲染 1~2 条，
            # 碰巧是看过的就直接结束，屏幕下方的新动态永远轮不到。
            # 现在：本屏里看过的跳过继续看，只有**整屏都没有新的**才算到边界。
            if pid and pid in seen:
                seen_in_screen += 1
                continue

            act = {"like": False, "comment": ""}
            try:
                act = judge(post, it) or act
            except Exception as e:
                out["errs"].append("判断失败：{}".format(type(e).__name__))
                log(f"[朋友圈] 判断一条时出错：{type(e).__name__}")

            want_like = bool(act.get("like"))
            want_cmt = bool(act.get("comment"))

            # 点赞判重：「赞」是开关，赞过再点会取消
            if want_like and already_liked(post, my_nick):
                log("[朋友圈] 这条已经赞过，跳过点赞")
                want_like = False
            act = {"like": want_like, "comment": act.get("comment") if want_cmt else ""}

            _why = str(act.get("reason") or "").strip()
            if want_like or want_cmt:
                try:
                    res = do_act(it, act) or {}
                except Exception as e:
                    res = {"err": "{}: {}".format(type(e).__name__, e)}
                if res.get("like"):
                    out["liked"].append(str(post.get("nickname") or ""))
                if res.get("comment"):
                    out["commented"].append(
                        (str(post.get("nickname") or ""), act.get("comment") or ""))
                if res.get("err"):
                    out["errs"].append(str(res["err"])[:60])
                    log(f"[朋友圈] ⚠️ {str(res['err'])[:60]}")
                else:
                    tags = []
                    if res.get("like"):
                        tags.append("赞")
                    if res.get("comment"):
                        tags.append("评")
                    if tags:
                        log("[朋友圈] 已{} | {}{}".format(
                            "、".join(tags),
                            describe(post)[:60],
                            (" | 原因：" + _why) if _why else ""))
            else:
                # **没动作也要留日志**（2026-11-14 修）。
                # 原来判断"不互动"时一声不吭就标记 seen —— 用户看到某条没被点赞，
                # 完全无法判断是"模型不想点"还是"根本没滑到这条"。
                log("[朋友圈] 划过（不赞不评）| {}{}".format(
                    describe(post)[:60],
                    (" | 原因：" + _why) if _why else ""))

            # 不管成没成，都记为看过（失败也不重试，否则一条坏的卡住整轮）
            if pid:
                on_seen(pid)
            out["processed"].append(post)
            progressed = True

        # **整屏都是看过的 -> 真到边界了**（2026-11-14）
        #
        # 但要防一个坑：朋友圈界面经常只渲染 1 条（懒渲染/窗口高度），
        # 而下面其实还有没看过的。第一屏就 1 条且是 seen 时，
        # 不能直接判"到边界"，得先往下滑一下看看有没有更多。
        # 判据：本屏 >= 2 条且全看过，或者已经滑过 idle_rounds 次仍没新的。
        if (not progressed) and known_in_screen > 0 and seen_in_screen >= known_in_screen:
            if known_in_screen >= 2 or idle_rounds >= 2:
                log("[朋友圈] 这一屏 {} 条都看过了，到边界了".format(known_in_screen))
                out["hit_seen"] = True
                return out
            # 只渲染出 1 条且是看过的 -> 往下滑一段再确认
            log("[朋友圈] 这屏只渲染了 1 条（已看过），往下滑确认一下")

        # **没进展就加大滑动量**（2026-11-13）：
        # 朋友圈一条纯图片动态可能高 1000px，固定 600 单位（约 5 格滚轮）
        # 滑不过去，下一屏读到的还是原来那一两条 —— 表现为"连续两段没有新条目"
        # 提前结束，永远到不了 seen 边界。
        if not progressed:
            idle_rounds += 1
            if idle_rounds >= 3:
                log("[朋友圈] 连续三段没有新条目，滑到底了")
                break
        else:
            idle_rounds = 0

        _eff_step = step if idle_rounds == 0 else int(step * (1 + idle_rounds * 0.8))
        try:
            # **用验证版**：滚完确认内容真变了，没变会自动换方式重试
            _moved = scroll_down_verified(
                mo, step=_eff_step, list_rect=list_rect,
                allow_fallback=allow_fallback)
            if not _moved:
                log("[朋友圈] 滚不动了（试了多种方式内容都没变），停")
                break
        except Exception as e:
            log(f"[朋友圈] 下滑失败：{type(e).__name__}")
            break

    return out

def scroll_until_seen(read_fn, seen_ids, max_rounds: int = 12,
                      per_round: int = 3, list_rect=None,
                      allow_fallback: bool = False,
                      step: int = 600) -> tuple:
    """**往下滑，一路收集新动态，滑到"以前看过的那条"就停。**

    用户要求（2026-10-19）："往刷朋友圈下滑直到滑到她以前看到过的，
    滑动中看到一条新的要让她判断要不要点赞或评论"。

    逻辑：
      ① 已经在最顶上（调用方先滚顶）
      ② 往下滚一点 -> 读一次库 -> 把看到的动态按时间从新到旧排
      ③ 遇到第一条"见过"的（id 在 seen_ids 里）-> **立即停**，它就是边界
      ④ 没遇到就继续滑，直到上限

    为什么要"看到见过的就停"：
      · 朋友圈是时间倒序，新的一定在上面；一旦碰到旧的，后面的全是更旧的
      · 不设边界会一路滑到几个月前，白白看一堆旧动态

    返回 (new_posts, stopped_at_seen)。
      new_posts      按时间从新到旧，且只包含"没见过"的
      stopped_at_seen 是不是真的碰到了见过的边界
    """
    try:
        import pyautogui
        import time as _t
    except Exception:
        posts = read_fn() or []
        new = [x for x in posts if _post_id(x) and _post_id(x) not in seen_ids]
        return new, False

    cx = cy = None
    if list_rect:
        try:
            cx = int((list_rect[0] + list_rect[2]) / 2)
            cy = int((list_rect[1] + list_rect[3]) / 2)
        except Exception:
            cx = cy = None

    seen_ids = set(seen_ids or ())
    all_ids = set()
    out = []
    stopped = False

    def _absorb():
        """读一次库。返回 (本轮新增几条, 是否碰到过见过的边界)。

        注意：`read_fn` 是从**数据库**读固定条数（limit=40），跟界面滑到哪无关。
        所以第一次读到的内容里本来就会混着 seen 的旧动态 ——
        **不能因为碰到 seen 就收工**，否则一次都不滑。
        """
        got = read_fn() or []
        got.sort(key=lambda x: int(x.get("create_time") or 0), reverse=True)
        hit_seen = False
        added = 0
        for post in got:
            pid = _post_id(post)
            if not pid or pid in all_ids:
                continue
            all_ids.add(pid)
            if pid in seen_ids:
                # 碰到边界了：这条以前看过，后面的更旧，一律不要
                hit_seen = True
                continue
            if hit_seen:
                continue
            out.append(post)
            added += 1
        return added, hit_seen

    # 第一次读库：只收集，**不因为碰到 seen 就返回**
    _added, _hit = _absorb()
    if _added == 0:
        # 一条新的都没有 —— 真的没东西可看
        return out, _hit

    for _ in range(max_rounds):
        if cx is None:
            break
        try:
            pyautogui.FAILSAFE = False
            pyautogui.moveTo(cx, cy, duration=0.15)
            for _ in range(per_round):
                pyautogui.scroll(-int(step))      # 负数 = 往下
                _t.sleep(0.18)
            _t.sleep(1.2)
        except Exception:
            break
        _added, _hit = _absorb()
        if _added > 0:
            continue          # 还滑出了新东西，继续
        # 本轮没有任何新增 -> 滑不动了（到底 / 到边界）
        stopped = bool(_hit)
        break

    return out, stopped
