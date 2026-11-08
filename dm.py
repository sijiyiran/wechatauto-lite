# -*- coding: utf-8 -*-
"""私聊：逐个好友独立判断、独立回复。

和群聊的区别（这套逻辑是分开写的，不硬套群的提示词）：

  群聊                    私聊
  ─────────────────────  ─────────────────────────────
  一堆人说话              只有你俩
  可以沉默（沉默是常态）    别人单独找你，装没看见很伤人
  @ 是主要点名方式         不需要 @
  语气偏表演、给观众看      语气更真、更像回一个人的话

用户还要求了三件"像人"的事：
  1. **多人同时找她**时，可以跟他们抱怨"你们一起发我回不过来"
     —— 但只抱怨"人多"，**绝不能把 A 说的私事告诉 B**
  2. **好奇心**：聊到有意思的事要追问后续（明天面试 / 要去吃的东西 / 说了一半的话）
  3. **自己判断要不要回**：不是每条都回，但也不能把单独找她的人晾着
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parent
TOPIC_PATH = BASE / "topics.json"

# 哪些会话不是"人"，不该当私聊对象处理
NON_HUMAN = {"weixin", "newsapp", "filehelper", "fmessage", "medianote",
             "qqmail", "tmessage", "brandsessionholder"}


def is_private_chat(uid: str) -> bool:
    u = (uid or "").lower()
    if not u or u.endswith("@chatroom"):
        return False
    if u in NON_HUMAN:
        return False
    # 公众号/服务号等
    if u.startswith("gh_"):
        return False
    return True


# ───────────────────────── 好奇心：记住聊到一半的事 ─────────────────────────
def load_topics() -> dict:
    try:
        d = json.loads(TOPIC_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError
    except Exception:
        d = {}
    d.setdefault("version", 1)
    d.setdefault("people", {})
    return d


def save_topics(d: dict) -> None:
    try:
        tmp = TOPIC_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(TOPIC_PATH)
    except Exception:
        pass


def add_topic(uid: str, topic: str, when: float | None = None) -> None:
    """记一条"值得回头问一句"的事。同一个人最多留 6 条。"""
    topic = (topic or "").strip()[:40]
    if not topic:
        return
    d = load_topics()
    arr = d["people"].setdefault(uid, [])
    # 已经有类似的就不重复记
    for t in arr:
        if topic[:6] and topic[:6] in (t.get("topic") or ""):
            return
    arr.append({"topic": topic, "at": when or dt.datetime.now().timestamp(),
                "asked": False, "asked_at": 0})
    d["people"][uid] = arr[-6:]
    save_topics(d)


# 用户明确表示"别聊这个"的说法。
DROP_TOPIC_WORDS = (
    "别提了", "别聊这个", "换个话题", "说点别的", "不想说",
    "这事翻篇", "翻篇了", "行吧随你", "算了", "不聊了",
    "别揪着", "别追问", "别问了", "你让我别提", "关你什么事",
)


def wants_drop(text: str) -> bool:
    """对方是不是在说"别聊这个了"。"""
    t = (text or "").strip()
    return any(w in t for w in DROP_TOPIC_WORDS)


def clear_topics(uid: str) -> int:
    """把这个人的待追问话题全清掉。返回清了几条。

    代码级保险：提示词说了"该放下就放下"，但模型不一定听。
    用户一说"别提了"，直接把清单清空 —— 她连可追的东西都没有了。
    """
    d = load_topics()
    arr = (d.get("people") or {}).get(uid) or []
    n = len(arr)
    if n:
        d.setdefault("people", {})[uid] = []
        save_topics(d)
    return n

def pending_topics(uid: str, max_age_s: float = 3 * 86400) -> list[str]:
    """还没问过的、够新的那些话题。"""
    d = load_topics()
    now = dt.datetime.now().timestamp()
    out = []
    for t in (d["people"].get(uid) or []):
        if t.get("asked"):
            continue
        if now - float(t.get("at") or 0) > max_age_s:
            continue
        out.append(t.get("topic") or "")
    return [x for x in out if x]


def mark_asked(uid: str, topic_kw: str = "") -> None:
    """把话题标记成"问过了"，别再追。"""
    d = load_topics()
    now = dt.datetime.now().timestamp()
    arr = d["people"].get(uid) or []
    for t in arr:
        if not t.get("asked") and (not topic_kw or topic_kw[:4] in (t.get("topic") or "")):
            t["asked"] = True
            t["asked_at"] = now
    # 清掉太老的，别无限长
    cutoff = now - 7 * 86400
    d["people"][uid] = [t for t in arr
                        if float(t.get("at") or 0) > cutoff or not t.get("asked")]
    save_topics(d)


# ───────────────────────────── 提示词 ─────────────────────────────
DM_PROMPT = """你在微信上和人私聊。下面是你们之前的对话（越往下越新）。

【你现在是谁】
{persona}

【对方】
{who}

{others}
{topics}
【最近的对话】
{chat}

请判断这条私聊要不要回、回什么。

判断要点：
- 这是**私聊**，不是群。对方单独找到你，不说话会很像被无视。
- 但也不是每条都必须回：如果你刚说过话、对方只是发个表情、或者说了句"嗯"，可以不回。
- 语气是**你的人格**，但对一个人说话要比在群里更真、更放松，不用表演给别人看。
- 短。一两句。私聊里发小作文很吓人。
- **想多说两句时，可以用换行拆成 2~3 条发**（真人就是连发两条短句，
  而不是写一条长文）。reply 字段里用 \n 分隔，每一行会发成一条独立消息。
  最多 3 行，别刷屏。
{stop_hint}
{curiosity_hint}
{multi_hint}

输出严格 JSON：
{{"speak": true/false, "confidence": 0~1, "reason": "为什么", "reply": "要回的话", "topic": "对方提到的、值得以后追问的事（没有就填空字符串）"}}
"""

# **最重要的一条：知道什么时候该闭嘴。**
# 踩过（2026-10-26）：用户明确说"别提了""你让我别提我就不提？"，
# 她还是揪着"广告和 key 的事"不放，连回三条都在要交代。
# 根因是提示词里**根本没有"放下"这条规则**，而 CURIOSITY_HINT 还在鼓励追问。
# 真人的分寸感就在这儿：朋友说换话题，就是换了。
STOP_HINT = """
【最重要的一条：知道什么时候闭嘴】

对方说了下面这类话 ——
  "别提了" "别聊这个" "换个话题" "算了" "行吧随你" "不想说"
  "这事翻篇" "你让我别提" "不聊了" "关我什么事"
→ **这一句之后，那件事不准再提。一个字都不许再碰。**

**不要在心里想"他是在逃避，我不能让他糊弄过去"** ——
那是 AI 的执念，不是真人。真人被要求换话题，换就是了，
心里可以不爽，嘴上要放过对方。

**反复揪同一件事 = 最像 AI、最招人烦的失败。**
遇到这种情况：平掉、换个话题、或者干脆只回一个"行"。
"""
CURIOSITY_HINT = """
- 如果对方之前提过某件**具体的事**（面试、考试、要去的地方、要做的东西），
  现在可以自然地问一句"那个怎么样了"。别生硬，一句就够。
- **但如果对方已经表现出不想聊、或者你之前问过一次没得到回应 —— 就别再问。**
  最多问一次。追第二遍就是骚扰。"""

MULTI_HINT = """
- 现在**还有 {n} 个人也在给你发消息**，你有点回不过来。
  如果气氛合适，可以顺口抱怨一句（比如"你们怎么都这时候找我"），
  但**绝对不要**把别人跟你说的私事复述出来 —— 那是最没分寸的事。"""


def build_dm_prompt(persona: str, who: str, chat_lines: list[str],
                    topics: list[str], others: list[str]) -> str:
    others_block = ""
    if others:
        others_block = "\n【现在还同时找你的人】\n" + "、".join(others[:5]) + "\n"
    topics_block = ""
    if topics:
        topics_block = "\n【你之前想追问的事】\n" + "\n".join("- " + t for t in topics[:4]) + "\n"
    return (DM_PROMPT
            .replace("{persona}", persona or "（没有特别人设，像个正常人就行）")
            .replace("{who}", who or "一个人")
            .replace("{others}", others_block)
            .replace("{topics}", topics_block)
            .replace("{chat}", "\n".join(chat_lines[-30:]) or "（还没有对话）")
            .replace("{stop_hint}", STOP_HINT)
            .replace("{curiosity_hint}", CURIOSITY_HINT if topics else "")
            .replace("{multi_hint}", MULTI_HINT.replace("{n}", str(len(others)))
                     if others else ""))


def repair_json_strings(text: str) -> str:
    """把 JSON 里**字符串内部的裸控制字符**转义掉。

    为什么必须有：允许她连发多条之后，模型会在 reply 里写**真实换行**：
        {"reply": "？你认真的
        行吧 随你"}
    这不是合法 JSON（字符串里不能有裸换行），json.loads 直接抛
    "Expecting ',' delimiter"，整条回复就白丢了。

    群聊那边早就有这个函数，私聊的 parse_verdict 一直没接上 ——
    结果私聊里模型一想发多条，整轮回复就解析失败、被丢掉，
    表现就是「一次只发一条，要等下一轮才补」（用户 2026-10-30 反馈）。

    做法：逐字符扫，只在"字符串内部"把控制字符换成转义形式，
    字符串外面的结构（逗号、引号）一个字都不动。
    """
    out = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ord(ch) < 0x20:
                out.append("\\u{:04x}".format(ord(ch)))
                continue
        out.append(ch)
    return "".join(out)


def parse_verdict(txt: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip())
    # **先修裸换行再解析。**
    # 模型想发多条时会在 reply 里写真实换行，那是非法 JSON。
    # 不修的话整轮回复直接丢（2026-10-30 踩到）。
    t = repair_json_strings(t)
    try:
        d = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
        except Exception:
            return {}
    return d if isinstance(d, dict) else {}
