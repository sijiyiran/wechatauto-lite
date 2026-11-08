# -*- coding: utf-8 -*-
"""主动找人聊天：群里安静久了，她主动挑个人搭一句话。

用户要的感觉是「@四季亦然，没事，烦你一下」这种 —— 不是正式打招呼，
是熟人之间没事找事的闲扯。所以：
  · 内容**必须**由人格卡生成，不是模板
  · 短、随口、带点赖皮
  · 优先挑**最近说过话的活人**（对着一个半天没露面的人喊，很像机器人）
  · 挑一个具体的人 @ 出来（比泛泛地在群里自言自语自然）

闸门（宁可少说，别把群聊变成骚扰）：
  · 群必须安静够久（默认 40 分钟）
  · 两次主动之间至少隔 30 分钟
  · 每天最多 6 次
  · 只在 9~23 点之间
  · 夜间模式 / 总开关关着 -> 不动
  · 大模型拒绝服务期间 -> 不动
"""
from __future__ import annotations

import json
import re
import time


COMPOSE_PROMPT = """你在一个微信群里，现在群里安静了一会儿，你想主动找个人搭句话。

【要搭话的人】
{target}

【这个人平时的样子】
{target_desc}

【群里最近说的话】
{recent}

请写一句**主动搭话**的话，要求：
1. 15 字以内，短。像随口发的一句，不是文章。
2. 语气必须就是你的人格：嘴硬、爱贫、不正经，但不恶意。
3. 不要问"在吗""最近怎么样"这种客套话。
4. 不要提"我看到群里没人说话"这种把气氛点破的话。
5. 可以是调侃、挑衅、随口一句废话、找个由头挑刺。
6. 参考这个感觉：「没事 烦你一下」「你是不是又在装死」「出来说句话」
7. **不要自己写对方的名字** —— @ 会自动加在前面，你再写一遍就重复了
   （反例：「冷魇，又装深沉呢？」-> 发出去会变成「@冷魇 冷魇，又装深沉呢？」）
8. 只输出这句话本身，不要加引号、不要解释。

输出 JSON：{{"say": "要说的话"}}
"""


def _day(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def gate(cfg: dict, state: dict, uid: str, quiet_s: float,
         now: float | None = None) -> tuple[bool, str]:
    """能不能主动说话。返回 (可以吗, 原因/不能的原因)。"""
    now = time.time() if now is None else now
    p = (cfg.get("proactive") or {})

    if not p.get("enabled", False):
        return False, "主动聊天没开"
    if not cfg.get("enabled", True):
        return False, "总开关关着"
    # 注意：**dry_run 不在这里拦**。
    # 踩过的坑：一开始把 dry_run 当成"禁止条件"写在这，结果 maybe_proactive
    # 里那个「预演，只写日志不发送」的分支永远走不到 —— 想预览一下
    # 完全没法预览，只能真发。dry_run 该是"照常生成、只是不发送"，
    # 由调用方处理（见 brain.maybe_proactive）。

    # 夜间模式
    night = cfg.get("night") or {}
    if night.get("enabled"):
        import datetime as dt
        hm = dt.datetime.fromtimestamp(now).strftime("%H:%M")
        if _in_window(hm, night.get("start", "23:00"), night.get("end", "06:30")):
            return False, "夜间模式"

    lo = int(p.get("hour_from", 9))
    hi = int(p.get("hour_to", 23))
    hour = int(time.strftime("%H", time.localtime(now)))
    if not (lo <= hour < hi):
        return False, f"不在 {lo}-{hi} 点之间"

    if quiet_s < float(p.get("min_quiet_s", 2400)):
        return False, f"群里才安静 {int(quiet_s)} 秒，还不够久"

    st = (state.get("proactive") or {}).get(uid) or {}
    last = float(st.get("last") or 0)
    gap = float(p.get("min_interval_s", 1800))
    if last and now - last < gap:
        return False, f"距上次主动才 {int(now - last)} 秒（要 {int(gap)}）"

    today = _day(now)
    count = int(st.get("count") or 0) if st.get("day") == today else 0
    cap = int(p.get("max_per_day", 6))
    if count >= cap:
        return False, f"今天已经主动 {count} 次了（上限 {cap}）"

    return True, f"可以（今天第 {count + 1} 次）"


def _in_window(hm: str, start: str, end: str) -> bool:
    a, b = start.strip(), end.strip()
    if a <= b:
        return a <= hm < b
    return hm >= a or hm < b


def pick_target(summary: dict, exclude: list[str] | None = None) -> dict | None:
    """挑一个搭话对象。

    优先 **最近说过话 + 发言占比不低** 的人 —— 对着一个长期潜水的人喊，
    对方会觉得莫名其妙，旁观者会觉得像机器人随机抽签。
    """
    exclude = set(exclude or [])
    cands = []
    for who, s in (summary or {}).items():
        if who in exclude:
            continue
        if not s.get("msgs"):
            continue
        cands.append((s.get("last") or 0, s.get("msgs") or 0, who, s))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = cands[:5]
    # 前 5 个里按发言量随机，避免每次都是同一个人
    import random
    top.sort(key=lambda x: -x[1])
    pick = random.choice(top[:3]) if len(top) > 1 else top[0]
    return {"wxid": pick[2], **pick[3]}


def describe_target(s: dict | None) -> str:
    if not s:
        return "（没数据）"
    parts = []
    if s.get("tags"):
        parts.append("标签：" + "、".join(s["tags"]))
    if s.get("desc"):
        parts.append(s["desc"])
    if s.get("top_bucket"):
        parts.append("最常在" + str(s["top_bucket"]))
    if s.get("avg_len"):
        parts.append("平均每句 {} 字".format(s["avg_len"]))
    return "；".join(parts) or "（没数据）"


def strip_name(say: str, name: str) -> str:
    """把开头重复的对方名字去掉。

    踩过的坑：模型爱写成「冷魇，又装深沉呢？」，外面再拼 @冷魇，
    发出去就成了「@冷魇 冷魇，又装深沉呢？」—— 一眼看出是模板拼的。
    prepare_at 只剥「@名字」和「名字+空格」，剥不掉「名字，」这种带标点的，
    所以在这里单独处理。
    """
    s = (say or "").strip()
    if not name:
        return s
    for sep in ("，", "、", "：", ":", ",", "\u2005", " "):
        pre = name + sep
        if s.startswith(pre):
            return s[len(pre):].lstrip("".join(["，、：:, ", "\u2005"]))
    if s.startswith(name) and len(s) > len(name):
        rest = s[len(name):]
        if rest[:1] in "，、：:, \u2005？?！!":
            return rest.lstrip("，、：:, \u2005")
    return s


def compose(cfg: dict, api_key: str, target_name: str, target_desc: str,
            recent_lines: list[str]) -> str:
    """让模型按人格写一句搭话。失败返回空串。"""
    import httpx

    if not api_key:
        return ""
    prompt = (COMPOSE_PROMPT
              .replace("{target}", target_name)
              .replace("{target_desc}", target_desc or "（没数据）")
              .replace("{recent}", "\n".join(recent_lines[-14:]) or "（群里暂时没说话）"))
    body = {
        "model": cfg["llm"]["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.25,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    try:
        r = httpx.post(cfg["llm"]["base_url"].rstrip("/") + "/chat/completions",
                       json=body,
                       headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json"},
                       timeout=60)
        if r.status_code != 200:
            return ""
        txt = r.json()["choices"][0]["message"]["content"]
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip())
        say = (json.loads(txt).get("say") or "").strip()
        # 兜底：模型偶尔把整段 JSON 或引号带出来
        say = say.strip("\"'“”「」 ").replace("\n", " ")
        # 万一它还是把名字写进去了，剥掉，免得和外面的 @ 重复
        say = strip_name(say, target_name)
        return say[:40]
    except Exception:
        return ""


def note_sent(state: dict, uid: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    pr = state.setdefault("proactive", {})
    st = pr.setdefault(uid, {})
    st["last"] = now
    today = _day(now)
    if st.get("day") != today:
        st["day"] = today
        st["count"] = 0
    st["count"] = int(st.get("count") or 0) + 1
