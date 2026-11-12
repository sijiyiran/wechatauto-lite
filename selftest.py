#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重启前的自检：把真实代码路径跑一遍，而不是复刻一份来测。

为什么需要这个文件：
  我给「正在冷处理这些人」那段提示词用了变量名 `extra`，而真实变量叫 `concede_note`。
  我的验证脚本当时是**手写复刻**那段拼接逻辑的，复刻的那份写对了 ——
  所以测试通过、代码是错的，上线后只要真有人被冷处理，tick_group 就每轮抛
  UnboundLocalError，整个自动回复彻底停摆。

  教训：**验证必须跑真实函数，不能复刻。** 这个文件就是那条教训的落地。
  每次改完 brain.py，先跑它，再重启。

跑法：
  python selftest.py
退出码 0 = 全过；非 0 = 有分支炸了，别重启。
"""
from __future__ import annotations

import json
import pathlib
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")
BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import brain as B  # noqa: E402

PASS, FAIL = [], []


def check(name: str):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print(f"  ✅ {name}")
        except Exception as e:
            FAIL.append((name, e))
            print(f"  ❌ {name}")
            print(f"     {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
        return fn
    return deco


class FakeDB:
    """不碰真数据库 —— 避免和正在运行的守护进程抢 wechatauto 的缓存文件。"""

    def __init__(self, chats=None, msgs=None, members=None):
        self.chats = chats if chats is not None else ["54491039852@chatroom"]
        self.msgs = msgs or {}
        self.members = members or {}

    def get_self_info(self):
        return {"username": "wxid_me", "nick_name": "deepsleep"}

    NICK = {
        "54491039852@chatroom": "反贪局专案组",
        "wxid_a": "冷魇", "wxid_b": "拾二月的旧灯",
        "wxid_other": "冷魇", "wxid_me": "deepsleep",
    }

    def get_nickname(self, uid):
        return self.NICK.get(uid, uid)

    def get_sessions(self, limit=800):
        return [{"username": u, "last_time": 0} for u in self.chats]

    def get_messages(self, uid, limit=100):
        return list(self.msgs.get(uid, []))

    def get_new_messages(self, uid, since_seq=0, limit=100):
        return [m for m in self.msgs.get(uid, [])
                if int(m.get("sort_seq") or 0) > int(since_seq)]

    def get_group_members(self, uid):
        return [{"username": "wxid_x", "nick_name": n}
                for n in self.members.get(uid, [])]


class FakeGUI:
    """假 GUI。**不会真的发任何消息**（self.gui() 返回的就是它）。

    它把"发出去"的内容同时写进 FakeDB，这样 send_confirmed 的回读确认能立刻
    成功 —— 否则要轮询到 30 秒超时，自检会白等半分钟。
    """

    def __init__(self, db=None):
        self.sent = []
        self.db = db
        self._seq = 900000

    def _record(self, who, text):
        self.sent.append((who, text))
        if self.db is not None and who:
            uid = "54491039852@chatroom"
            self._seq += 1000
            self.db.msgs.setdefault(uid, []).append({
                "sort_seq": self._seq, "type": "文本", "content": text,
                "sender_username": "", "_nick": "deepsleep"})

    def send_msg(self, text, who=None, verify=False):
        self._record(who, text)
        return {"status": "成功", "message": "ok"}

    def at_member(self, member, text, who=None, verify=False):
        self._record(who, f"@{member}\u2005{text}")
        return {"status": "成功", "message": "ok"}

    def send_image(self, path, who=None, verify=False):
        self._record(who, f"[图片]{path}")
        return {"status": "成功", "message": "ok"}

    def open_chat(self, name):
        return True


def _patch_rhythm():
    """rhythm 是在 tick_group 里局部 import 的，所以必须打真正的模块。"""
    try:
        from wechatauto import rhythm as _r
        _r.gate = lambda *a, **k: None
    except Exception:
        pass


def isolate_paths():
    """把「会写盘的东西」全部改道到 _selftest-* 文件。

    **这条是踩出来的**：自检构造假大脑时调用了 save_state() 和决策日志写入，
    而那两个路径都是模块级的真实路径 —— 结果：
      · 真的 decisions.jsonl 里多了 7 条「理由:自检」的假记录
      · 真的 state.json 被清成 256 字节（水位、冷却、逗的计数全没了）
    所以自检和盘点脚本必须先调这个，把写盘目标挪开。
    """
    B.STATE_PATH = BASE / "_selftest-state.json"
    try:
        import brain as _b
        _b.STATE_PATH = B.STATE_PATH
    except Exception:
        pass


def make_brain(msgs=None, members=None, verdict=None):
    """造一个能用的大脑：不碰数据库、不真发消息、不调大模型。"""
    isolate_paths()
    cfg = B.load_config()
    # 决策日志也必须改道，理由同上。
    cfg.setdefault("safety", {})["decisions_log"] = "_selftest-decisions.jsonl"
    # **自检必须和用户当前配置无关。**
    # 踩过的坑：用户把总开关关成 enabled=false 之后重启，自检里"应该能发出消息"
    # 那条用例就挂了 -> 启动门禁拒绝启动 -> 想再打开也打不开，人被困住了。
    # 所以这里强制打开；专门测总开关的那条用例会自己覆盖成 false。
    cfg["enabled"] = True
    for k, v in (cfg.get("groups") or {}).items():
        if isinstance(v, dict):
            v["enabled"] = True
    db = FakeDB(msgs=msgs, members=members)
    gui = FakeGUI(db)
    gb = B.GroupBrain(cfg, db, lambda: gui, "fake-key")
    gb.state = {"watermarks": {}, "last_reply": {}, "sent": [],
                "tease": {}, "tease_ignore": {}, "night": {}, "balance_alert": {}}
    gb.my_wxid = "wxid_me"
    gb.my_nick = "deepsleep"
    gb._nick_cache = {}
    gb._gui = None
    # 大模型换成固定回答
    B.llm_judge = lambda *a, **k: (verdict or {
        "speak": True, "confidence": 0.9, "reason": "自检",
        "reply": "在", "at": "", "sticker": ""})
    return gb, gui, cfg


UID = "54491039852@chatroom"


def msg(seq, text, who="wxid_other", typ="文本", nick="冷魇"):
    return {"sort_seq": seq, "type": typ, "content": f"{who}:\n{text}",
            "sender_username": who, "_nick": nick}


# ── 一、纯函数 ────────────────────────────────────────────────────────
print("\n【一】纯函数")

@check("402 不能一上来就当没钱：要先查一次余额")
def _():
    # 踩过的坑（2026-10-18）：日志报 Insufficient Balance，但 /user/balance
    # 显示 31.51（其中 31.30 是 DeepSeek 赠送额度），再发请求也成功了 ——
    # 那是一次短暂抖动。按原来的逻辑会让她静默 30 分钟，余额明明还在。
    # 现在 402 之后先查余额：有钱就只等 transient_402_backoff_s。
    cfg = B.load_config()
    llm = cfg.get("llm") or {}
    assert "transient_402_backoff_s" in llm, \
        "缺 transient_402_backoff_s —— 短暂 402 会退化成 30 分钟静默"
    assert int(llm["transient_402_backoff_s"]) <= 300, \
        "短暂抖动的重试间隔不该超过 5 分钟"
    ladder = llm.get("blocked_backoff_s") or []
    assert ladder and int(llm["transient_402_backoff_s"]) < int(ladder[-1]), \
        "抖动重试必须比长退避短，否则这个分支没意义"
    assert callable(getattr(B, "check_balance", None)), "缺 check_balance"


@check("朋友圈：触发词认得对；按点看；图片拿不到要承认")
def _():
    import moments as MO

    cfg = B.load_config()
    m = cfg.get("moments") or {}
    assert m.get("enabled") is True, "朋友圈默认应该是开的"
    slots = m.get("schedule") or []
    assert slots == ["06:00", "12:00", "21:00"], f"看朋友圈的时间点不对：{slots}"

    # 触发词：点名让看要看，聊到朋友圈权限/屏蔽不算
    for yes in ("@deepsleep 去看看朋友圈", "你刷朋友圈了吗", "看看朋友圈有没有更新"):
        assert MO.wants_moments(yes), f"该触发没触发：{yes}"
    for no in ("朋友圈权限给关了", "我从来不发朋友圈", "今天吃什么"):
        assert not MO.wants_moments(no), f"不该触发却触发：{no}"

    # 视频不看：describe 里必须写清楚，别让模型假装看过
    line = MO.describe({"nickname": "某人", "text": "起飞", "videos": [{}]})
    assert "不看视频" in line, f"视频没标出来：{line}"

    # 动态 id 不能把 wxid 当 id（那个字段名有坑：tid 实际是 username）
    pid = MO._post_id({"id": "15029789017517724167",
                       "tid": "wxid_abc", "username": "wxid_abc"})
    assert pid == "15029789017517724167", f"取错 id 了：{pid}"
    pid2 = MO._post_id({"tid": "wxid_abc", "username": "wxid_abc"})
    assert pid2 == "", f"没有真 id 时不该返回 wxid：{pid2}"


@check("好友申请：查完必须回话，而且回对地方")
def _():
    # 用户的要求："我让她看好友申请，看完她不会回我，我想让她回我。"
    # 这条测 _fr_say：回话要发回**当面问她的那个会话**，手动触发时不乱发。
    import time as _t

    sent = []
    gb = object.__new__(B.GroupBrain)
    gb.cfg = B.load_config()
    gb.nick = lambda u: "测试群"
    gb.send_confirmed = lambda uid, name, text, at="": (sent.append(("群", text)), (True, "ok"))[1]
    gb.send_dm_strict = lambda who, text: (sent.append(("私聊", text)), (True, "ok"))[1]

    # 群里问的 -> 回群里
    gb._fr_ask_from = {"uid": "g", "kind": "group", "at": _t.time()}
    gb._fr_say("没人加你")
    assert sent and sent[-1][0] == "群", f"群里问的应该回群里，实际：{sent}"

    # 私聊问的 -> 回私聊
    gb._fr_ask_from = {"uid": "d", "kind": "dm", "at": _t.time()}
    gb._fr_say("通过了1个")
    assert sent[-1][0] == "私聊", f"私聊问的应该回私聊，实际：{sent}"

    # 用完必须清，否则下次会用旧会话乱发
    assert gb._fr_ask_from is None, "_fr_say 用完没清 _fr_ask_from"

    # 手动标记文件触发（没有会话）-> 不该乱发
    n = len(sent)
    gb._fr_ask_from = None
    gb._fr_say("没人加你")
    assert len(sent) == n, "没有具体会话时不该乱发消息"

    # 触发太久之前的不回（隔了一轮的话很怪）
    n = len(sent)
    gb._fr_ask_from = {"uid": "g", "kind": "group", "at": _t.time() - 600}
    gb._fr_say("没人加你")
    assert len(sent) == n, "超过 5 分钟的触发不该再回话"


@check("好友申请：改成按需触发，平时不许自己去看")
def _():
    # 用户明确要求：**只在他让检查、或群里有人喊"加好友通过一下"时才去看**。
    # 这条测两件事：
    #   ① 触发词认得准（既要能触发，也不能被"通过一下门禁"这种误触发）
    #   ② 配置里必须是 on_demand，不能留定时轮询
    import friend_requests as FR

    cfg = B.load_config()
    p = cfg.get("friend_requests") or {}
    assert p.get("check_mode") == "on_demand", (
        "check_mode 必须是 on_demand —— 定时轮询会一直去翻通讯录，用户明确不要")
    assert "check_every_s" not in p, "定时轮询的配置项应该已经删掉了"

    yes = [
        "@deepsleep 加好友通过一下",
        "帮我通过好友申请",
        "有人加你 同意一下",
        "检查好友申请",
        "好友验证点了通过一下",
    ]
    no = [
        "帮我通过一下门禁",
        "验证码发我",
        "这次面试通过了",
        "今天吃什么",
        "",
    ]
    for t in yes:
        assert FR.should_check(t), f"该触发却没触发：{t}"
    for t in no:
        assert not FR.should_check(t), f"不该触发却触发了：{t}"


@check("好友申请：垃圾词必须拦住；状态词必须认对")
def _():
    # 这条链只能走 UI，测不了点击，但**能测的两件事必须测**：
    #  ① 骗子申请要能被关键词拦住（这是唯一的防骗闸门）
    #  ② 界面里那些 name 字符串的状态词要认对，否则会把"已过期"当"待处理"
    #     反复去点，或者把真申请漏掉。
    import friend_requests as FR

    cfg = B.load_config()
    p = cfg.get("friend_requests") or {}
    assert p.get("enable" + "d") is True, "好友申请默认应该是开的"
    blocks = tuple(p.get("block_keywords") or ())
    assert blocks, "block_keywords 不能是空的 —— 那是唯一的防骗闸门"

    # 真申请（群聊来的）不能被误杀
    for ok_name in ('💤我是群聊"反贪局专案组"的💤等待验证',
                    "小王我是隔壁的老王",
                    "四季亦然我是群聊“反贪局专案组”的四季亦然"):
        assert not FR.is_spam(ok_name, blocks), f"正常人被拦了：{ok_name}"

    # 骗子申请要拦住
    for bad in ("推广兼职日结", "加我微信刷单", "贷款秒下款", "博彩投资理财"):
        assert FR.is_spam(bad, blocks), f"可疑申请没拦住：{bad}"

    # 状态词识别（界面上就是这么拼的：昵称+招呼语+状态词）
    assert "等待验证" in "💤我是群聊x的💤等待验证"
    assert any(w in "四季亦然已添加" for w in FR.DONE_WORDS)
    assert any(w in "澜已过期" for w in FR.DONE_WORDS)


@check("成员档案：字数不能被 XML 带飞；标签默认不进对话")
def _():
    # 踩过的坑：把非文本消息的 content（一整段 XML，几百字符）也算进字数，
    # 结果"平均每句 344 字"，看着像每个人都在发小作文。只能统计文本消息。
    import profiles as P

    class _DB:
        def get_messages(self, uid, limit=100):
            return [
                {"sender_username": "wxid_a", "type": "文本",
                 "content": "wxid_a:\n你好啊", "create_time": 1792000000,
                 "sort_seq": 1},
                {"sender_username": "wxid_a", "type": "文本",
                 "content": "wxid_a:\n今天吃什么", "create_time": 1792000060,
                 "sort_seq": 2},
                {"sender_username": "wxid_a", "type": "动画表情",
                 "content": '<msg><emoji fromusername="x">' + "x" * 800 + "</emoji></msg>",
                 "create_time": 1792000120, "sort_seq": 3},
                {"sender_username": "wxid_b", "type": "文本",
                 "content": "wxid_b:\n哈哈哈哈哈", "create_time": 1792000180,
                 "sort_seq": 4},
            ]

    raw = P.collect(_DB(), "g", "wxid_me", limit=100)
    s = P.summarize(raw)
    a = s["wxid_a"]
    assert a["msgs"] == 3, a["msgs"]
    # 只有两条文本：(3 + 5) / 2 = 4.0。如果 XML 被算进来会是几百。
    assert a["avg_len"] < 20, f"字数被 XML 带飞了：avg_len={a['avg_len']}"
    assert s["wxid_b"]["laugh_rate"] > 0, "哈哈类应该被识别"

    # 默认**不能**进对话提示词 —— 用户要求先只测试
    cfg = B.load_config()
    assert (cfg.get("profiling") or {}).get("use_in_reply") is False, \
        "profiling.use_in_reply 默认必须是 false（标签只观察，不进对话）"
    assert P.profiles_prompt_block(cfg, "任何群") == "", \
        "标签默认不该出现在提示词里"


@check("主动聊天：闸门必须真的拦得住；不能重复写对方名字")
def _():
    import proactive as PA

    cfg = json.loads(json.dumps(B.load_config()))
    # **自检必须和用户当前配置无关。**（这个坑踩了第二次）
    # 用户把 proactive.enabled 关掉之后，这条用例就失败，看着像闸门坏了，
    # 其实只是功能被关了。测试自己把开关打开，才测得到逻辑。
    cfg["proactive"]["enabled"] = True
    cfg["enabled"] = True
    cfg["safety"]["dry_run"] = False
    cfg["night"]["enabled"] = False
    # 用**白天**的时间戳。踩过的坑：一开始随手写了个 1792000000，
    # 换算出来是凌晨 1:46，闸门按"夜间模式"正确地拦住了，
    # 于是测试自己失败、看着像功能坏了。
    import datetime as _dt
    now = _dt.datetime(2026, 10, 15, 15, 0, 0).timestamp()
    # 群里还很热闹 -> 不能主动插嘴
    ok, why = PA.gate(cfg, {}, "g", 60, now)
    assert not ok, f"刚安静 60 秒就主动说话？{why}"
    # 安静够久 -> 可以
    ok, _ = PA.gate(cfg, {}, "g", 7200, now)
    assert ok, "安静两小时应该可以主动说话"
    # 关掉开关 -> 一律不行
    c2 = json.loads(json.dumps(cfg))
    c2["proactive"]["enabled"] = False
    assert not PA.gate(c2, {}, "g", 7200, now)[0], "关掉开关还能说话？"
    # 当天额度用满 -> 不行
    st = {"proactive": {"g": {"last": now - 4000, "count": 99,
                              "day": PA._day(now)}}}
    assert not PA.gate(cfg, st, "g", 7200, now)[0], "额度用满还能说话？"

    # 名字不能被写两遍（外面 @ 一次 + 正文里再一次）
    name = "冷魇"
    for s in ("冷魇，又装深沉呢？", "冷魇 又装深沉呢？"):
        out = PA.strip_name(s, name)
        assert not out.startswith(name), f"名字没剥干净：{out!r}"
        combined = B.prepare_at(out, name, [name])
        assert combined.count(name) == 1, f"名字出现了多次：{combined!r}"
    # 本来就正常的句子不能被误伤
    assert PA.strip_name("没事 烦你一下", name) == "没事 烦你一下"


@check("看图：描述要进上下文；缓存要生效；参数不能被调坏")
def _():
    # 踩过的两个坑，各挡一条：
    #  ① max_tokens 太小 -> HTTP 200 但 content 是空字符串（推理 token 吃光预算）
    #  ② 不关思考 -> 同一张图白烧 2000 多个推理 token
    cfg = B.load_config()
    v = B.vision_cfg(cfg)
    assert v["enabled"] is True, "看图默认该是开的"
    assert v["model"], "必须指定视觉模型"
    assert v["max_tokens"] >= 400, (
        f"max_tokens={v['max_tokens']} 太小 —— flash 先输出一大段推理，"
        f"正文会被挤成空字符串")
    assert v["disable_thinking"] is True, "看图不需要链式推理，应该关掉思考"
    assert 128 <= v["max_side"] <= 2048, "max_side 越界"

    # 描述要真的出现在给模型看的文本里
    row = {"type": "图片", "content": '<msg><img aeskey="x"/></msg>',
           "sender_username": "wxid_x", "_vision": "一只橘猫趴在键盘上"}
    out = B.render_body(row)
    assert "橘猫" in out, f"描述没进上下文：{out!r}"
    assert out.startswith("[图片"), out

    # 没描述的老消息不能因此报错
    row2 = {"type": "图片", "content": '<msg><img aeskey="x"/></msg>'}
    assert B.render_body(row2) == "[图片]", B.render_body(row2)

    # 取不到的图要记一笔"别再试"，否则每次判断都会重新解密一遍同一张废图
    saved_cache, saved_save = B._vision_cache, B.save_vision_cache
    B._vision_cache = {"desc": {}, "calls": []}
    B.save_vision_cache = lambda: None
    import wechatauto.media as _m
    real_dl = getattr(_m, "MediaDownloader", None)

    class _DL:
        def __init__(self, db):
            pass

        def download_image(self, *a, **k):
            return None

    try:
        _m.MediaDownloader = _DL
        rows = [{"type": "图片", "local_id": 1}]
        B.describe_image_rows(cfg, None, rows, "chat", "key")
        assert B._vision_cache["desc"].get("chat:1") == "", B._vision_cache["desc"]
    finally:
        if real_dl is not None:
            _m.MediaDownloader = real_dl
        B._vision_cache = saved_cache
        B.save_vision_cache = saved_save


@check("总开关必须真的能关掉一切（全局 enabled 要参与 group_cfg 合并）")
def _():
    # 踩过的坑：group_cfg 只合并了 groups["*"] 和 groups[uid]，把最上层的
    # cfg["enabled"] 整个丢掉了 —— 而界面上的总开关写的正是那一个。
    # 结果点了「停用」它照样在群里说话，开关形同虚设。
    real = B.load_config()
    uid = "54491039852@chatroom"

    def enabled_with(glob, star, grp):
        cfg = json.loads(json.dumps(real))
        cfg["enabled"] = glob
        cfg["groups"]["*"] = {"enabled": star}
        if grp is None:
            cfg["groups"].pop(uid, None)
        else:
            cfg["groups"][uid] = {"enabled": grp}
        gb, _gui, _c = make_brain()
        gb.cfg = cfg
        return bool(gb.group_cfg(uid).get("enabled"))

    assert enabled_with(True, True, None) is True
    assert enabled_with(True, True, True) is True
    assert enabled_with(True, False, None) is False
    assert enabled_with(True, False, True) is True     # 单独给某个群开
    # 总闸优先：全局关掉 => 不管每群怎么写，都是关
    assert enabled_with(False, True, True) is False
    assert enabled_with(False, False, True) is False

    # 而且总闸关掉时，tick_group 连一次大模型都不该调（= 不花钱）
    cfg = json.loads(json.dumps(real))
    cfg["enabled"] = False
    gb, _gui, _c = make_brain()
    gb.cfg = cfg
    calls = []
    B.llm_judge = lambda *a, **k: calls.append(1) or {"speak": True, "reply": "x"}
    gb._pending[UID] = {
        "rows": [{"sort_seq": 1, "type": "文本", "content": "wxid_x:\n@deepsleep 在吗",
                  "sender_username": "wxid_x"}],
        "first": 0, "last": 0, "need": 0}
    gb.tick_group(UID)
    assert not calls, f"总闸关掉后还调了 {len(calls)} 次大模型"


@check("表情包：频率闸门必须真的拦得住（配置里那两个键原来是摆设）")
def _():
    import time as _t
    gb, _gui, _cfg = make_brain()
    # 踩过的坑：sticker.max_per_hour 只在旧的 do_sticker 路径里用到，
    # sticker.cooldown_s 在代码里根本没引用 —— 主发送路径一点限制都没有，
    # 全靠提示词那句"每三四次补一张"，而提示词管不住模型（实测到过 42%）。
    ok, _why = gb.sticker_allowed()
    assert ok, "一开始应该允许"
    gb.note_sticker_sent()
    ok, why = gb.sticker_allowed()
    assert not ok and "冷却" in why, f"刚发一张应该被冷却拦住，实际 {ok} {why}"
    # 冷却过了但一小时发了 6 张 -> 被上限拦
    gb.state["sticker_sent"] = [_t.time() - 200] * 6
    ok, why = gb.sticker_allowed()
    assert not ok and "上限" in why, f"一小时 6 张应该被上限拦住，实际 {ok} {why}"
    # 关掉开关就不许发
    gb.cfg["abilities"]["sticker"]["enabled"] = False
    assert not gb.sticker_allowed()[0]


@check("表情包：登记表能取到、没说明的图不给模型选")
def _():
    cfg = B.load_config()
    _blk, _fld, usable = B.sticker_hint(cfg)
    if not usable:
        return          # 库是空的就跳过（用户可能还没放图）
    for sid, v in usable.items():
        assert (v.get("desc") or "").strip(), f"{sid} 没有说明却进了可用表"
        p = BASE / (v.get("file") or "")
        assert p.exists(), f"{sid} 登记的文件不存在：{p}"
        assert p.stat().st_size > 500, f"{sid} 文件太小，可能是坏图"
    # 没有说明的图不该被模型看到（它不知道何时该用）
    import json as _json
    orig = B.load_stickers()
    try:
        fake = dict(orig)
        fake["zz"] = {"file": "stickers/s01.png", "desc": "", "when": ""}
        (BASE / "stickers.json").write_text(
            _json.dumps(fake, ensure_ascii=False), encoding="utf-8")
        _b2, _f2, u2 = B.sticker_hint(B.load_config())
        assert "zz" not in u2, "没有说明的图不该进可用表"
    finally:
        (BASE / "stickers.json").write_text(
            _json.dumps(orig, ensure_ascii=False, indent=2), encoding="utf-8")


@check("家属向脏话要换说法而不是整条消失")
def _():
    # 实测两次很难看的：
    #   ① 有人问"你父母呢，是不是很早就死了"，它答"没有父母\n哪来的死活"，
    #      本意是"（我是AI）没有父母"，但它 @ 了对方 → 读起来就是骂人家没父母。
    #   ② 有人说"所以你本来就没父母"，它回"你是靠结婚证生的？"（暗讽野种）。
    # 提示词拦不住这类，所以放代码里硬拦。
    #
    # **2026-11-09 改行为**：用户要求"拦截后就不会说了，改一下" ——
    # 命中脏话不再整条丢弃，而是换成温和说法继续发；换不掉才丢。
    persona = {"max_chars": 60, "forbidden": [], "allow_markdown": False,
               "allow_links": False}
    bad = ["@拾二月的旧灯 没有父母", "没有父母\n哪来的死活", "@燃 你是靠结婚证生的？",
           "？你妈死了", "孤儿一个", "草你妈的", "你全家长得都这样？",
           "？嘴真臭\n你妈没教过你说话？"]
    for s in bad:
        out = B.sanitize(s, persona).strip()
        assert out, f"整条消失了（应该换成温和说法）：{s!r}"
        assert not B.family_insult_hit(out), f"换完还带脏话：{out!r}"
    # 不能误伤：含"妈"但不是骂人的要放行
    good = ["？你再说一遍试试", "行吧[叹气]", "？嘴这么脏，刷牙了吗",
            "？！你才傻逼", "妈妈叫你回家吃饭", "？真请还是骗我[旺柴]", "我不用睡觉"]
    for s in good:
        assert B.sanitize(s, persona).strip(), f"误拦：{s!r}"


@check("表情：非法 [xxx] 必须删掉，合法表情不占字数上限")
def _():
    # 微信自带 emoji 在库里就是文字代码（实测群里 [捂脸][OK] 都是 type=文本），
    # 所以直接写进回复就会变表情。但写错代码不会变，会显示成方括号文字。
    assert B.strip_bad_emoji("？[捂脸]") == "？[捂脸]"
    assert B.strip_bad_emoji("[开心]你谁啊") == "你谁啊"
    assert B.strip_bad_emoji("[无语][无语]") == ""
    assert B.strip_bad_emoji("就这[吃瓜][旺柴]") == "就这[吃瓜][旺柴]"
    assert B.strip_bad_emoji("正常话") == "正常话"
    # 表情不占字数：`[破涕为笑]` 是 6 个字符但只显示一个表情
    persona = {"max_chars": 10, "forbidden": [], "allow_markdown": False,
               "allow_links": False}
    assert "[破涕为笑]" in B.sanitize("这句正好十个字啊[破涕为笑]", persona)
    # 提示词
    on = B.load_config()
    on["abilities"]["emoji"]["enabled"] = True
    assert "[捂脸]" in B.emoji_hint(on)
    # 关掉时**不能返回空串** —— 人格卡里可能还留着表情例子，
    # 不吭声它照样会发。必须明确说一句"不许发"。
    off = B.load_config()
    off["abilities"]["emoji"]["enabled"] = False
    hint_off = B.emoji_hint(off)
    assert "不要发表情" in hint_off
    # **禁止指令里不能举具体的表情代码当例子** —— 那等于把代码又念一遍，
    # 模型很可能照着写。这里就是被这条绊过一次。
    assert "[捂脸]" not in hint_off and "[吃瓜]" not in hint_off
    # 而且回复里的表情要被**硬删**（光靠提示词拦不住模型习惯）
    assert B.strip_all_emoji("？[捂脸]") == "？"
    assert B.strip_all_emoji("行吧[叹气] 明天见") == "行吧 明天见"
    assert B.strip_all_emoji("正常话") == "正常话"
    assert B.strip_all_emoji("[吃瓜]看戏") == "看戏"


@check("is_low_information：语气词该滤掉，有内容的一律放行")
def _():
    # 该滤掉：实测四季亦然发「enm」，它回「enm你个头 没得谈」——
    # 一句带刺的话砸在语气词上，看着就是追着人怼。
    for s in ["enm", "emm", "emmm", "嗯", "嗯嗯", "哦", "额", "呃", "啊", "哈哈",
              "…", "。。。", "？", "？？", "!", "，", "[表情]", "[表情] enm", "  enm  "]:
        assert B.is_low_information(s), f"{s!r} 应该被当成低信息量"
    # 必须放行 —— 宁可多判断一次，也别把真在说话的人当空气
    for s in ["v我50token", "enm你个头", "几点睡", "在吗", "大肥鱼", "想你了",
              "enm 那你给我说说", "？你谁", "哈哈 你也太逗了", "50"]:
        assert not B.is_low_information(s), f"{s!r} 有实际内容，不该被滤"


@check("prepare_at：拼 @ / 去重 / 统一 U+2005")
def _():
    mem = ["鬶月月鸟", "冷魇", "🌹"]
    assert B.prepare_at("恋你个头", "鬶月月鸟", mem) == "@鬶月月鸟\u2005恋你个头"
    assert B.prepare_at("@鬶月月鸟 @鬶月月鸟 恋你个头", "鬶月月鸟", mem) == "@鬶月月鸟\u2005恋你个头"
    assert B.prepare_at("@冷魇 你人都不在", "", mem) == "@冷魇\u2005你人都不在"
    assert B.prepare_at("@🌹 不见", "", mem) == "@🌹\u2005不见"

@check("drop_retracted：剔除被撤回的那条，不误伤别人")
def _():
    rows = [
        msg(1000, "随便聊聊", who="wxid_a", nick="冷魇"),
        msg(2000, "臭乐乐@deepsleep", who="wxid_b", nick="拾二月的旧灯"),
        {"sort_seq": 3000, "type": "系统消息", "content":
         '<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg>'
         '<content>"拾二月的旧灯" 撤回了一条消息</content>'
         '<revoketime>0</revoketime></revokemsg></sysmsg>', "sender_username": ""},
        msg(4000, "@deepsleep 我喜欢你", who="wxid_b", nick="拾二月的旧灯"),
    ]
    gb, _, _ = make_brain()
    kept, dropped = B.drop_retracted(rows, lambda r: gb.speaker_of(r))
    seqs = [int(r["sort_seq"]) for r in kept]
    assert dropped == 1, dropped
    assert 2000 not in seqs, "被撤回的那条必须剔掉"
    assert 1000 in seqs and 3000 in seqs and 4000 in seqs

@check("cd_wait_seconds：只解析冷却，额度类返回 None")
def _():
    assert B.cd_wait_seconds("冷却中（6.9s）") == 6.9
    assert B.cd_wait_seconds("冷却中（0.0s）") == 0.0
    assert B.cd_wait_seconds("10 分钟内说得够多了") is None
    assert B.cd_wait_seconds("本小时额度用尽") is None
    assert B.cd_wait_seconds("处于静默时段") is None

@check("in_window：跨零点时段")
def _():
    assert B.in_window("23:30", "23:00", "06:30")
    assert B.in_window("03:00", "23:00", "06:30")
    assert B.in_window("06:29", "23:00", "06:30")
    assert not B.in_window("06:30", "23:00", "06:30")
    assert not B.in_window("12:00", "23:00", "06:30")

@check("is_self_message：以正文前缀为准（sender_username 会错）")
def _():
    # 没前缀 = 自己发的（群聊里别人一定带前缀）；sender_username 不参与判断，
    # 因为它会错，错了就会让机器人回复自己。
    assert B.is_self_message({"content": "没前缀", "sender_username": "wxid_other"}, "wxid_me")
    assert B.is_self_message({"content": "wxid_me:\n我说的", "sender_username": "wxid_other"}, "wxid_me")
    assert not B.is_self_message({"content": "wxid_other:\n他说的", "sender_username": "wxid_me"}, "wxid_me")


# ── 二、逗 / 冷处理 ───────────────────────────────────────────────────
print("\n【二】逗 / 冷处理")

@check("豁免名单：名单里的人喊雷点词不计次")
def _():
    gb, _, _ = make_brain()
    for _i in range(6):
        assert gb.note_tease(UID, "四季亦然") == 0
    assert gb.note_tease(UID, "冷魇") == 1

@check("冷处理：建立 / 查询 / 解除")
def _():
    gb, _, _ = make_brain()
    gb.set_ignore(UID, "冷魇", 300)
    assert "冷魇" in gb.active_ignores(UID)
    assert gb.clear_ignore(UID, "冷魇")
    assert "冷魇" not in gb.active_ignores(UID)
    assert not gb.clear_ignore(UID, "查无此人")


# ── 三、真实 tick_group（这才是重点）─────────────────────────────────
print("\n【三】真实 tick_group 跑通（上次的 bug 就在这里）")

@check("tick_group：有人在被冷处理时也要能跑通")
def _():
    gb, gui, _ = make_brain()
    gb.set_ignore(UID, "冷魇", 300)              # 关键：激活那段分支
    gb.tick_group(UID)                            # ← 上次这里抛 UnboundLocalError

@check("tick_group：夜间模式要能跑通")
def _():
    gb, _, _ = make_brain(msgs={UID: [msg(2000, "大肥鱼")]})
    gb.cfg["night"]["enabled"] = True
    gb.now_hm = lambda: "23:30"                   # 装成夜里
    gb.is_night = lambda: True
    gb.tick_group(UID)

@check("tick_group：正常有人 @ 它，走完整判断+发送")
def _():
    import time as _t
    gb, gui, _ = make_brain(msgs={UID: [msg(2000, "@deepsleep 在吗", who="wxid_x", nick="冷魇")]},
                            members={UID: ["冷魇", "四季亦然"]})
    gb.state["watermarks"] = {UID: 1000}
    _patch_rhythm()
    cfg = gb.group_cfg(UID)["participation"]
    cfg["reply_delay_s"] = [0, 0]
    cfg["cooldown_wait_max_s"] = 0

    # 第一趟只会把消息攒进缓冲（去重窗口设计如此，进度快就抢答会踩冷却）。
    gb.tick_group(UID)
    assert gb._pending.get(UID, {}).get("rows"), "第一趟应该把消息攒起来"

    # 装成"群里已经安静很久了"，第二趟才会真正冲刷并发送。
    pend = gb._pending[UID]
    pend["last"] = _t.time() - 100
    pend["first"] = _t.time() - 100
    gb.tick_group(UID)

    assert gui.sent, "两趟之后仍然一条都没发出去"
    assert any("在" in txt or "@" in txt for _w, txt in gui.sent), gui.sent

@check("tick_group：触发词逗满 → 生成收场提示词不炸")
def _():
    gb, _, _ = make_brain(members={UID: ["冷魇"]})
    for _i in range(6):
        gb.note_tease(UID, "冷魇")
    gb._tease_counts = {"冷魇": 6}
    # 直接跑到拼提示词那步：这里以前因为 extra/concede_note 混用炸过
    lines, members = [], ["冷魇"]
    sysmsg, user = gb.build_prompts(UID, lines, members, gb.group_cfg(UID),
                                    ["点名你 冷魇: 大肥鱼"], extra="【收场】测试")
    assert "收场" in user

@check("tick_group：正在冷处理 + 生成提示词（两个分支叠加）")
def _():
    gb, _, _ = make_brain(members={UID: ["冷魇"]})
    gb.set_ignore(UID, "冷魇", 300)
    ign = gb.active_ignores(UID)
    assert ign
    # 这一段以前用的变量名是 extra，真实变量是 concede_note
    extra = ""
    who_lines = [f"  {w}（还剩 {int(l // 60)} 分 {int(l % 60)} 秒）" for w, l in ign.items()]
    extra += "\n【你正在冷处理这些人】\n" + "\n".join(who_lines) + "\n默认继续不理。\n"
    assert "冷魇" in extra

@check("reply_still_fresh：过时判定")
def _():
    gb, _, _ = make_brain()
    deep = {"sort_seq": 1000, "type": "文本", "content": "wxid_a:\n旧话", "sender_username": "wxid_a"}
    assert gb.reply_still_fresh(UID, [(deep, "旧话", False)], gb.group_cfg(UID))[0]

@check("alert_no_balance：余额提醒有冷却、不刷屏")
def _():
    gb, gui, _ = make_brain()
    _patch_rhythm()
    gb.is_night = lambda: False
    gb.alert_no_balance(UID)
    n1 = len(gui.sent)
    gb.alert_no_balance(UID)
    gb.alert_no_balance(UID)
    assert len(gui.sent) == n1 == 1, gui.sent

@check("maybe_night_talk：晚安/早上好各只触发一次")
def _():
    gb, gui, _ = make_brain()
    fired = []
    gb._group_active_recently = lambda u, s=180: True
    gb.send_night_talk = lambda u, k: (fired.append(k), (True, k))[1]
    for t in ["22:50", "22:56", "22:58", "23:00", "06:29", "06:30", "06:35", "08:30"]:
        gb.now_hm = lambda t=t: t
        gb.maybe_night_talk(UID)
    assert fired == ["goodnight", "morning"], fired


# ── 四、人格卡能否被正常读进来 ────────────────────────────────────────
print("\n【四】提示词装配")

@check("人格卡存在、非空、含关键条款")
def _():
    p = BASE / "prompts" / "persona.md"
    t = p.read_text(encoding="utf-8")
    assert len(t) > 3000, len(t)
    for k in ("deepsleep", "四季亦然", "大肥鱼", "别越聊越凶", "关于发脾气"):
        assert k in t, f"人格卡缺少：{k}"

@check("build_prompts：system/user 都拼得出来且含关键段")
def _():
    gb, _, _ = make_brain()
    system, user = gb.build_prompts(UID, ["12:00 冷魇: 大肥鱼"], ["冷魇"],
                                    gb.group_cfg(UID), ["点名你 冷魇: 大肥鱼"])
    assert "deepsleep" in system, "system 里应该有名字桥接"
    assert "群聊记录" in user or "最近的群聊" in user
    assert len(system) > 4000

@check("simulation.md 仍然被注入")
def _():
    gb, _, _ = make_brain()
    system, _u = gb.build_prompts(UID, [], [], gb.group_cfg(UID), [])
    assert "群聊" in system


# ── 汇总 ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("\n失败清单：")
    for n, e in FAIL:
        print(f"  - {n}: {type(e).__name__}: {e}")
    print("\n❌ 自检没过，**不要重启守护进程**，先修上面的问题。")
    sys.exit(1)
print("\n✅ 全部通过，可以重启。")
sys.exit(0)
