# -*- coding: utf-8 -*-
"""成员档案：发言频率 / 习惯 / 性格标签。

**只用于观察，绝不进对话提示词。**
用户的要求是「先测试」，所以这里产出的标签只写进 member-profiles.json，
供 --profiles 查看。`profiles_prompt_block()` 只有在配置里明确打开
`profiling.use_in_reply` 时才会返回内容，默认返回空串 —— 免得以后有人
顺手把它接进提示词，把"测试"变成"上线"。

两个数据来源结合使用（这也是用户的要求）：
  1. **发言频率**：条数、占比、活跃时段、平均字数、连续发言的密度
  2. **发言内容**：常用词、表情使用、@ 别人的次数、跟谁互动最多

光看频率会把"话少的深沉人"和"话少的冷淡人"混为一谈；
光看内容会漏掉"这人只在半夜出现"这种极重要的特征。所以两个都要。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parent
PROFILE_PATH = BASE / "member-profiles.json"

HOUR_BUCKETS = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜")

# 常见语气词/口头禅：用来判断语气风格（不追求严谨，够用就好）
FILLER_RE = re.compile(r"^(?:哈+|嘿+|呵+|嗯+|哦+|额+|呃+|啊+|唉+|诶+|草|靠|卧槽|我靠)")
LAUGH_RE = re.compile(r"(?:哈{2,}|hhh+|233+|笑死|草生|乐死)")


def hour_bucket(h: int) -> str:
    if 0 <= h < 5:
        return "凌晨"
    if 5 <= h < 8:
        return "早上"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 17:
        return "下午"
    if 17 <= h < 19:
        return "傍晚"
    if 19 <= h < 23:
        return "晚上"
    return "深夜"


def load_profiles() -> dict:
    try:
        d = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError
    except Exception:
        d = {}
    d.setdefault("version", 1)
    d.setdefault("groups", {})
    return d


def save_profiles(d: dict) -> None:
    try:
        tmp = PROFILE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROFILE_PATH)
    except Exception:
        pass


def _peer_of(row: dict, me: str) -> str:
    su = row.get("sender_username") or ""
    if su and su != me:
        return su
    return ""


def collect(db, uid: str, me: str, limit: int = 3000) -> dict:
    """从数据库统计一个群的所有成员。返回 {wxid: stats}。"""
    rows = db.get_messages(uid, limit=limit) or []
    rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0))

    stats: dict[str, dict] = {}
    last_speaker = ""
    last_time = 0

    for r in rows:
        su = r.get("sender_username") or ""
        # 自己发的消息 sender_username 是空的（微信库就这么标的）
        who = su or me
        if not who:
            continue
        mtype = (r.get("type") or "")
        if mtype == "系统消息":
            continue

        s = stats.setdefault(who, {
            "msgs": 0, "chars": 0, "text_msgs": 0, "emoji": 0, "ats": 0,
            "laugh": 0, "hours": {}, "to": {}, "first": 0, "last": 0,
        })
        body = r.get("content") or ""
        # 去掉 "wxid_xxx:\n" 前缀
        body = re.sub(r"^\s*(?:wxid_[0-9a-z]+|[\w\-]{6,}):\s*\n?", "", body)

        s["msgs"] += 1
        # **只统计文本消息的字数。**
        # 踩过的坑：一开始把所有类型都算进去，结果平均每句 344 字 ——
        # 因为表情/链接/卡片这些消息的 content 是一整段 XML（几百字符），
        # 把中位数彻底带飞了，看着像"每个人都在发小作文"。
        if mtype == "文本":
            s["text_msgs"] += 1
            s["chars"] += len(body)
            if re.search(r"\[[^\]]{1,12}\]", body):
                s["emoji"] += 1
            if LAUGH_RE.search(body):
                s["laugh"] += 1
            if "@" in body:
                s["ats"] += 1

        ts = r.get("create_time")
        if ts:
            t = int(ts)
            d0 = dt.datetime.fromtimestamp(t)
            k = str(d0.hour)
            s["hours"][k] = s["hours"].get(k, 0) + 1
            if not s["first"] or t < s["first"]:
                s["first"] = t
            if t > s["last"]:
                s["last"] = t

        # 谁在接谁的话：60 秒内紧跟着上一个人的发言，算一次互动
        if last_speaker and last_speaker != who and ts:
            if 0 < int(ts) - last_time <= 60:
                s["to"][last_speaker] = s["to"].get(last_speaker, 0) + 1
        if ts:
            last_time = int(ts)
        last_speaker = who

    return stats


def summarize(stats: dict, names: dict | None = None) -> dict:
    """把原始计数整理成人能读的特征。"""
    names = names or {}
    total = sum(s["msgs"] for s in stats.values()) or 1
    out = {}
    for who, s in stats.items():
        hours = {int(k): v for k, v in (s.get("hours") or {}).items()}
        buckets: dict[str, int] = {}
        for h, n in hours.items():
            buckets[hour_bucket(h)] = buckets.get(hour_bucket(h), 0) + n
        top_bucket = max(buckets, key=buckets.get) if buckets else "未知"
        peak_hours = sorted(hours, key=hours.get, reverse=True)[:2] if hours else []
        top_to = sorted((s.get("to") or {}).items(), key=lambda kv: -kv[1])[:2]
        span_days = max(1, (s["last"] - s["first"]) / 86400.0) if s.get("last") and s.get("first") else 1
        out[who] = {
            "name": names.get(who) or who,
            "msgs": s["msgs"],
            "share": round(s["msgs"] * 100.0 / total, 1),
            "avg_len": round(s["chars"] / s["text_msgs"], 1) if s.get("text_msgs") else 0,
            "per_day": round(s["msgs"] / span_days, 1),
            "emoji_rate": round(s["emoji"] * 100.0 / s["msgs"], 1) if s["msgs"] else 0,
            "laugh_rate": round(s["laugh"] * 100.0 / s["msgs"], 1) if s["msgs"] else 0,
            "ats": s["ats"],
            "top_hours": peak_hours,
            "top_bucket": top_bucket,
            "buckets": buckets,
            "closest": [{"who": names.get(w, w), "n": n} for w, n in top_to],
            "first": s.get("first") or 0,
            "last": s.get("last") or 0,
        }
    return out


LABEL_PROMPT = """你是群聊观察员。下面是一个微信群里每个人的发言统计。请给**每个人**打一组性格标签。

统计字段说明：
- msgs 发言条数，share 占比(%)，avg_len 平均每条字数
- per_day 平均每天发言条数（判断话多话少）
- emoji_rate 用表情的比例(%)，laugh_rate 用"哈哈"类大笑的比例(%)
- ats @别人的次数，top_bucket 最活跃时段，top_hours 最活跃的两个小时
- closest 最常接谁的话（互动最紧密的人）
- 还有**最近 8 条发言原文**，用来感受语气

要求：
1. 每人给 2~4 个短标签，用「」括起来，例如：话痨、夜猫子、爱呛人、三字党、潜水员、气氛组、理性派、表情包达人
2. 再写一句 30 字以内的性格描述，要具体（引用统计里的数字或原文特征），不要空话
3. **只根据数据说话**，数据不足就写"样本太少"
4. 输出严格 JSON：{"标签": {"<wxid>": {"tags": ["话痨","夜猫子"], "desc": "..."}}}

群里的人：
{people}

最近发言原文：
{recent}
"""


SALVAGE_RE = re.compile(
    r'"(wxid_[^"]+)"\s*:\s*\{[^{}]*?"tags"\s*:\s*\[([^\]]*)\][^{}]*?'
    r'(?:"desc"\s*:\s*"([^"]*)")?', re.S)


def salvage_tags(txt: str) -> dict:
    """从**被截断的** JSON 里把已经完整的条目捞出来。

    踩过的坑：10 个人的标签 JSON 大概 1200+ 字符，max_tokens 设 1600 时
    输出被硬生生切断在某个 desc 中间，json.loads 直接抛
    "Expecting ',' delimiter"，于是**整批标签全丢**。
    其实前面 6 个人的标签已经完整写完了 —— 捞出来总比一个都没有强。
    """
    out = {}
    for who, tags_raw, desc in SALVAGE_RE.findall(txt or ""):
        tags = [t.strip().strip('"\'') for t in tags_raw.split(",") if t.strip()]
        tags = [t for t in tags if t][:5]
        if not tags and not desc:
            continue
        out[who] = {"tags": tags, "desc": (desc or "")[:80]}
    return out


def label_members(cfg: dict, api_key: str, group_name: str,
                  summary: dict, recent_lines: list[str]) -> dict:
    """让大模型根据统计+原文给出性格标签。失败返回 {}。"""
    import httpx

    if not summary or not api_key:
        return {}
    people = []
    for who, s in summary.items():
        people.append({
            "wxid": who, "name": s["name"],
            "msgs": s["msgs"], "share": s["share"], "avg_len": s["avg_len"],
            "per_day": s["per_day"], "emoji_rate": s["emoji_rate"],
            "laugh_rate": s["laugh_rate"], "ats": s["ats"],
            "top_bucket": s["top_bucket"], "top_hours": s["top_hours"],
            "closest": [c["who"] for c in s["closest"]],
        })
    prompt = (LABEL_PROMPT
              .replace("{people}", json.dumps(people, ensure_ascii=False, indent=1))
              .replace("{recent}", "\n".join(recent_lines[:40]) or "（无）"))
    body = {
        "model": cfg["llm"]["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        # 给足：10 个人的标签+描述大约 1200 字符，而 flash 还要先输出推理，
        # 1600 会被截断（实测整批标签全丢）。
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    txt = ""
    try:
        r = httpx.post(cfg["llm"]["base_url"].rstrip("/") + "/chat/completions",
                       json=body,
                       headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json"},
                       timeout=120)
        if r.status_code != 200:
            print("    [标签] HTTP {}: {}".format(r.status_code, r.text[:200]))
            return {}
        data = r.json()
        choice = data["choices"][0]
        txt = choice["message"]["content"]
        finish = choice.get("finish_reason")
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip())
        if not txt:
            rt = (data.get("usage") or {}).get(
                "completion_tokens_details", {}).get("reasoning_tokens", "?")
            print("    [标签] 正文为空 finish_reason={} reasoning_tokens={} "
                  "—— 多半是 max_tokens 不够".format(finish, rt))
            return {}
        try:
            d = json.loads(txt)
            tags = d.get("标签") or d.get("tags") or {}
            if isinstance(tags, dict) and tags:
                return tags
        except Exception:
            pass
        # 完整 JSON 解析失败（多半是被截断）—— 捞已经写完整的那些
        rescued = salvage_tags(txt)
        print("    [标签] JSON 不完整（finish_reason={}），抢救出 {} 条".format(
            finish, len(rescued)))
        return rescued
    except Exception as e:
        print("    [标签] 解析失败 {}: {}".format(type(e).__name__, str(e)[:160]))
        if txt:
            print("    [标签] 原文前 300 字: {!r}".format(txt[:300]))
        return {}


def profiles_prompt_block(cfg: dict, group_name: str) -> str:
    """给对话用的提示词块。**默认返回空串** —— 用户明确要求先只测试。

    打开方式（想上线时再改）：config.json -> profiling.use_in_reply = true
    """
    p = (cfg.get("profiling") or {})
    if not p.get("use_in_reply", False):
        return ""
    d = load_profiles()
    g = (d.get("groups") or {}).get(group_name) or {}
    mem = g.get("members") or {}
    if not mem:
        return ""
    lines = []
    for who, m in mem.items():
        tags = "、".join(m.get("tags") or [])
        desc = m.get("desc") or ""
        if tags or desc:
            lines.append(f"- {m.get('name') or who}：{tags}。{desc}")
    if not lines:
        return ""
    return "\n—— 群里这些人大概是什么样（观察所得，只作参考）——\n" + "\n".join(lines) + "\n"
