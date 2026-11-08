#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从你自己的历史群聊发言里提炼说话风格，产出 style.json。

产出两样东西：
  1. stats —— 长度/标点/表情/口头禅画像（用来生成语气描述）
  2. examples —— 真实的「别人说 → 你当时怎么回」配对，作为少样本示例

少样本示例是这套东西里最有效的部分：它教的是"你怎么回"，
比任何形容词都准。素材越多、跑得越勤，画像越贴近你。

用法：
  python profile_style.py            # 提炼并写入 style.json
  python profile_style.py --show     # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import random
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent
STYLE_PATH = BASE / "style.json"

sys.path.insert(0, str(BASE))
import brain as B  # noqa: E402

PER_GROUP = 500
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def harvest(db, me: dict) -> tuple[list[dict], list[dict]]:
    """返回 (我发的文本消息, 配对示例)。"""
    my = me["username"]
    mine, pairs, per_group = [], [], {}
    for s in db.get_sessions(limit=800):
        uid = s.get("username") or ""
        if not uid.endswith("@chatroom"):
            continue
        gname = db.get_nickname(uid)
        try:
            rows = list(reversed(db.get_messages(uid, limit=PER_GROUP)))
        except Exception:
            continue
        n = 0
        pending: list[str] = []
        for r in rows:
            if (r.get("type") or "") != "文本":
                continue
            body = B.strip_sender_prefix(r.get("content") or "").strip()
            if not body:
                continue
            if B.is_self_message(r, my):
                n += 1
                mine.append({"text": body, "group": gname})
                if pending:
                    heard = pending[-1]
                    if 1 <= len(body) <= 60 and 2 <= len(heard) <= 80:
                        pairs.append({"group": gname, "heard": heard[:80], "said": body})
                pending = []
            else:
                pending.append(body)
                if len(pending) > 3:
                    pending.pop(0)
        if n:
            per_group[gname] = n
    return mine, pairs, per_group


def analyze(mine: list[dict]) -> dict:
    texts = [m["text"] for m in mine]
    lens = sorted(len(t) for t in texts)
    n = len(lens)

    def q(p: float) -> int:
        return lens[min(n - 1, int(n * p))]

    ends = collections.Counter()
    for t in texts:
        if t.endswith("。"):
            ends["。" ] += 1
        elif t[-1:] in "！!":
            ends["！"] += 1
        elif t[-1:] in "？?":
            ends["？"] += 1
        elif t[-1:] in "~～":
            ends["~"] += 1
        else:
            ends["无"] += 1

    buckets = collections.Counter()
    for l in lens:
        if l <= 4: buckets["1-4"] += 1
        elif l <= 10: buckets["5-10"] += 1
        elif l <= 20: buckets["11-20"] += 1
        elif l <= 40: buckets["21-40"] += 1
        else: buckets["40+"] += 1

    laughs = ["哈哈哈哈", "哈哈哈", "哈哈", "hhh", "嘿嘿", "笑死", "xswl", "呵呵"]
    catch = ["草", "卧槽", "我靠", "确实", "emmm", "emm", "呃", "啊这", "好家伙",
             "麻了", "离谱", "绝了", "可以", "行吧", "好的", "收到", "在呢", "咋了",
             "啥", "不知道", "不是", "没有", "对", "嗯", "哦", "啊"]

    return {
        "count": n,
        "avg": round(sum(lens) / n, 1),
        "median": q(0.5), "p75": q(0.75), "p90": q(0.9), "max": lens[-1],
        "buckets": dict(buckets),
        "endings": dict(ends),
        "emoji_pct": round(sum(1 for t in texts if EMOJI_RE.search(t)) * 100 / n),
        "laughs": {k: sum(1 for t in texts if k in t) for k in laughs},
        "catch": {k: v for k, v in sorted(
            ((k, sum(1 for t in texts if k in t)) for k in catch),
            key=lambda x: -x[1]) if v > 0},
        "per_group": {},
    }


def brief(st: dict) -> str:
    """把统计变成一句人话，塞进提示词。"""
    parts = []
    med = st["median"]
    if med <= 3:
        parts.append(f"你说话极短，一半以上的回复不超过 {med} 个字")
    elif med <= 8:
        parts.append(f"你说话很短，典型长度 {med} 个字左右")
    else:
        parts.append(f"你说话长度中等，典型 {med} 个字左右")
    parts.append(f"最长一般不超过 {st['p90']} 字")
    ends = st["endings"]
    total = sum(ends.values()) or 1
    if ends.get("无", 0) / total > 0.7:
        parts.append("句子结尾基本不加句号，也不加感叹号")
    elif ends.get("无", 0) / total > 0.4:
        parts.append("结尾经常不加标点")
    if st["emoji_pct"] <= 5:
        parts.append("几乎不用 emoji 和表情")
    elif st["emoji_pct"] <= 20:
        parts.append("偶尔用 emoji")
    laughs = {k: v for k, v in st["laughs"].items() if v}
    if not laughs:
        parts.append("不用『哈哈』这类笑声词")
    else:
        top = max(laughs, key=laughs.get)
        parts.append(f"要笑的时候用「{top}」")
    catch = list(st["catch"].items())[:6]
    if catch:
        parts.append("常用词：" + "、".join(f"「{k}」" for k, _ in catch))
    return "；".join(parts) + "。"


def pick_examples(pairs: list[dict], limit: int = 24) -> list[dict]:
    """挑有代表性、彼此不重复的配对。"""
    seen_said, out = set(), []
    # 按回复长度分层，保证短句占多数（那才是你的主体风格）
    short = [p for p in pairs if len(p["said"]) <= 4]
    mid = [p for p in pairs if 5 <= len(p["said"]) <= 10]
    long_ = [p for p in pairs if len(p["said"]) > 10]
    random.seed(11)
    for pool, quota in ((short, 12), (mid, 8), (long_, 4)):
        random.shuffle(pool)
        for p in pool:
            if len(out) >= limit:
                break
            key = p["said"]
            if key in seen_said:
                continue
            if len(key) > 40:
                continue
            seen_said.add(key)
            out.append(p)
            quota -= 1
            if quota <= 0:
                break
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    from wechatauto import WeChatDB
    db = WeChatDB()
    me = db.get_self_info()
    print(f"账号 {me['username']}（{me['nick_name']}）\n")

    mine, pairs, per_group = harvest(db, me)
    if not mine:
        print("没采到你自己发的文本消息，无法提炼。")
        return 1

    st = analyze(mine)
    st["per_group"] = per_group
    examples = pick_examples(pairs)
    style = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"own_text_messages": len(mine), "reply_pairs": len(pairs),
                   "groups": len(per_group)},
        "stats": st,
        "brief": brief(st),
        "examples": examples,
    }

    print("=== 素材 ===")
    for g, c in sorted(per_group.items(), key=lambda x: -x[1]):
        print(f"  {g:26} {c:4} 条")
    print(f"  合计 {len(mine)} 条文本；可用配对 {len(pairs)} 组")
    print("\n=== 风格描述（会塞进提示词）===")
    print("  " + style["brief"])
    print("\n=== 选中的示例 ===")
    for e in examples:
        print(f"  「{e['heard']}」 → 「{e['said']}」")

    if args.show:
        print("\n(--show：没有写文件)")
        return 0
    STYLE_PATH.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {STYLE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
