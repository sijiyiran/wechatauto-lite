"""陪伴 AI 的冷热分层记忆 —— 照着 Companion-AI-Memory-Context-Token-Optimization 的思路实现。

上游那个项目（dsh-companion-memory）是 **DSH 插件**，依赖 ctx.tools / session/event，
跑在 DSH 进程里。我们的微信机器人是**独立 Python 进程**，两者运行时不通，
所以这里按它的设计**重新实现**一份，不复制代码。

解决的三个问题
--------------
1. **记不住事**：每次调用只能看到最近几条消息，昨天说好的事情今天就忘了。
2. **Token 烧得快**：为了不漏事就把全部历史塞进 prompt，越聊越贵。
3. **人设漂移**：性格、习惯、当前状态和普通事件混在一起，被淘汰后像换了个人。

三个机制
--------
- **分层记忆**：pinned（人格/相处状态，永不淘汰）/ hot（近事，按热度）/ cold（旧事，线索召回）
- **热度冷却**：heat = 0.45*F + 0.35*R + 0.20*O，F=有效提及频率 R=最近性 O=次数
- **预算控制**：每轮只带 1600 token 的记忆快照（固定 160 / 活跃 960 / 召回 480）

数据（放在 <程序目录>/companion-memory/）
-----------------------------------------
archive.jsonl   原文归档，追加写，永不删
records.json    记忆卡（结构化事实：约定、喜好、事件）
state.json      连续性状态 + 各种计数器

纯标准库，不依赖任何第三方包。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import threading
import time

BASE = pathlib.Path(__file__).resolve().parent
MEM_DIR = BASE / "companion-memory"
ARCHIVE = MEM_DIR / "archive.jsonl"
RECORDS = MEM_DIR / "records.json"
STATE = MEM_DIR / "state.json"

_LOCK = threading.RLock()

# ── 预算（token）──
BUDGET_TOTAL = 1600
BUDGET_PINNED = 160
BUDGET_HOT = 960
BUDGET_RECALL = 480

# ── 冷却参数 ──
HOT_MAX_AGE_H = 72.0        # 72 小时内的细节还算"热"
SUMMARY_MIN_AGE_H = 24.0    # 超过 24 小时可以压成摘要
COLD_GRACE_DAYS = 7.0       # 进冷库前的宽限期
COLD_HEAT = 0.30            # 热度低于这个值 → 冷
COLD_SILENT_DAYS = 14.0     # 或者 14 天没被提及 → 冷

# ── 召回 ──
RECALL_MAX_ITEMS = 3
RECALL_MIN_SCORE = 0.15


# ══════════════════════════════════════════════ 工具 ──
def _ensure_dir() -> None:
    MEM_DIR.mkdir(parents=True, exist_ok=True)


def now_ts() -> float:
    return time.time()


def _read_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json(path: pathlib.Path, obj) -> None:
    _ensure_dir()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def est_tokens(text: str) -> int:
    """粗估 token 数。中文 1 字≈1 token，拉丁 4 字符≈1 token。

    不追求精确 —— 这是给"别超预算"用的，不是给账单核对用的。
    """
    if not text:
        return 0
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cn
    return int(cn + other / 4 + 0.5)


def _trim_to_tokens(text: str, limit: int) -> str:
    """截断到大约 limit 个 token。"""
    if est_tokens(text) <= limit:
        return text
    out = []
    used = 0
    for ch in text:
        cost = 1 if "\u4e00" <= ch <= "\u9fff" else 0.25
        if used + cost > limit:
            break
        out.append(ch)
        used += cost
    return "".join(out).rstrip() + "…"


def _today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


# ══════════════════════════════════════════ 原文归档 ──
def archive_append(speaker: str, text: str, who: str = "", uid: str = "",
                   meta: dict | None = None) -> None:
    """归档一条对话原文。

    speaker: "user"（对方说的）/ "assistant"（她说的）
    who:     说话人昵称
    uid:     会话 id（群 id 或私聊 wxid）
    """
    text = (text or "").strip()
    if not text:
        return
    rec = {
        "ts": now_ts(),
        "speaker": speaker,
        "who": who or "",
        "uid": uid or "",
        "text": text[:2000],
    }
    if meta:
        rec["meta"] = meta
    with _LOCK:
        _ensure_dir()
        try:
            with ARCHIVE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


def archive_load(limit: int = 0) -> list:
    """读归档。limit>0 时只读最后 limit 条。"""
    if not ARCHIVE.exists():
        return []
    out = []
    try:
        with ARCHIVE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-limit:] if limit else out


# ══════════════════════════════════════════ 记忆卡 ──
def load_records() -> dict:
    return _read_json(RECORDS, {})


def save_records(d: dict) -> None:
    _write_json(RECORDS, d)


def _norm_text(s: str) -> str:
    """归一化：只留字母数字汉字，小写。用于粗相似度比较。"""
    return "".join(ch for ch in (s or "") if ch.isalnum()).lower()


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（不是子序列）。

    中文换个说法时，核心短语常常原样保留：
      「川木不强、有点不想抽」 vs 「不太想抽川木这个角色」
      共同子串 = 「川木」（2）或「不想抽」（3）
    连续 5 个字一样，基本就是同一件事了。
    """
    if not a or not b:
        return 0
    # 短文本用经典 DP，够快（记忆卡 < 100 字）
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ca = a[i - 1]
        for j in range(1, len(b) + 1):
            if ca == b[j - 1]:
                v = prev[j - 1] + 1
                cur[j] = v
                if v > best:
                    best = v
        prev = cur
    return best


def _similar(a: str, b: str, threshold: float = 0.62,
             wa: str = "", wb: str = "") -> bool:
    """判断两条记忆是不是在说同一件事。

    用字符集合的 Jaccard 相似度 —— 不追求语义精确，只挡住"完全复述"
    和"换个说法说同一句"这两种最常见的重复。
    实测例子：
      「四季亦然不喝酒」 vs 「我不喝酒」        → 重合度 0.6 → 判重复
      「AI 名为 deepsleep」 vs 「我的名字是 deepsleep」→ 判重复
      「四季亦然不喝酒」 vs 「四季亦然爱喝可乐」  → 不判重复
    """
    sa = set(_norm_text(a))
    sb = set(_norm_text(b))
    if not sa or not sb:
        return False
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    if inter / union >= threshold:
        return True
    # **包含关系**：短句的字符基本都被长句包住，也算同一件事。
    # 例子：「我不喝酒」⊂「四季亦然明确表示自己不喝酒」—— Jaccard 只有 0.25，
    # 但显然是同一条。代价是会误并「我不喝酒」和「冷魇不喝酒」这种同谓语
    # 不同主语的，所以要求重合度更高（0.9）。
    small = min(len(sa), len(sb))
    if small >= 2 and inter / small >= 0.9:
        return True
    # **先看实体（who）**：主语不同就不合并。
    # 踩过（2026-11-01）：不判断主语时，「群里有人叫 deep meta」和
    # 「群里有人叫 Ember🌨」因为共同套话「群里有人叫」被判成重复，
    # 结果把两个不同的人合并成一条，信息直接丢了。
    wa = (wa or "").strip()
    wb = (wb or "").strip()
    if wa and wb and not _name_match(wa, wb):
        # 主语不同 —— 只有文本几乎一模一样才合并
        return inter / union >= 0.85
    # **最长公共子串**：连续 6 个字一样 → 同一件事。
    # 阈值 6 而不是 5：「群里有人叫」正好 5 个字，是套话不是内容。
    na, nb = _norm_text(a), _norm_text(b)
    if na and nb:
        L = _lcs_len(na, nb)
        if L >= 6:
            return True
        # 短句场景：专名（「川木」「大肥鱼」）占比高
        if L >= 3 and L / max(1, min(len(na), len(nb))) >= 0.6:
            return True
    return False


def dedupe(threshold: float = 0.55) -> dict:
    """清掉已有的重复记忆卡，把重复的提及次数并到最先那条上。

    返回 {'removed': n, 'kept': m}。
    """
    d = load_records()
    items = sorted(d.items(), key=lambda kv: float(kv[1].get("created") or 0))
    kept = []
    removed = 0
    for rid, r in items:
        if r.get("superseded"):
            kept.append((rid, r))
            continue
        dup_of = None
        for kid, kr in kept:
            if kr.get("superseded"):
                continue
            if _similar(r.get("text"), kr.get("text"), threshold,
                        r.get("who") or "", kr.get("who") or ""):
                dup_of = kr
                break
        if dup_of is not None:
            # 合并：提及次数相加，取更晚的 last_mention
            dup_of["mentions"] = int(dup_of.get("mentions") or 1) + int(
                r.get("mentions") or 1)
            days = list(dup_of.get("mention_days") or [])
            for dd in (r.get("mention_days") or []):
                if dd not in days:
                    days.append(dd)
            dup_of["mention_days"] = days[-30:]
            lm = max(float(dup_of.get("last_mention") or 0),
                     float(r.get("last_mention") or 0))
            dup_of["last_mention"] = lm
            removed += 1
        else:
            kept.append((rid, r))
    newd = {rid: r for rid, r in kept}
    save_records(newd)
    return {"removed": removed, "kept": len(newd)}


def remember(text: str, kind: str = "fact", topic: str = "",
             who: str = "", when: float | None = None,
             source: str = "", uid: str = "", gname: str = "",
             scope: str = "") -> str:
    """写一条记忆卡。返回卡片 id。

    kind:  fact（事实）/ promise（约定）/ preference（喜好）/ event（事件）
           / reflection（猜想，单独存，不当真）
    """
    text = (text or "").strip()
    if not text:
        return ""
    d = load_records()
    # **先查重**：同一条事实不重复建新卡，改成给旧卡 +1 提及。
    # 不查重的话，每轮自动提取都会把「四季亦然不喝酒」再记一遍
    # （实测一条被记了 10 次，把 960 token 的活跃预算吃光）。
    for _rid, _r in d.items():
        if _r.get("superseded"):
            continue
        if _similar(_r.get("text"), text, 0.55,
                    _r.get("who") or "", who or ""):
            touch_record(_rid)
            return _rid
    # **同会话记忆卡封顶**：超了就淘汰热度最低的那条。
    # 不封顶的话，同一个群聊几百轮后能堆上百条（实测反贪局群 35 条里有
    # 7 条都在说「💤 不想抽川木」）。规则再严也挡不住措辞变化。
    if uid:
        same = [(k, v) for k, v in d.items()
                if (v.get("uid") or "") == uid and not v.get("superseded")]
        cap = 25
        if len(same) >= cap:
            same.sort(key=lambda kv: _heat(kv[1]))
            drop_n = len(same) - cap + 1
            for k, _v in same[:drop_n]:
                d.pop(k, None)
    rid = "m{}".format(len(d) + 1)
    while rid in d:
        rid = "m{}".format(int(rid[1:]) + 1)
    ts = float(when or now_ts())
    d[rid] = {
        "id": rid,
        "text": text[:500],
        "kind": kind,
        "topic": (topic or "").strip()[:40],
        "who": (who or "").strip()[:60],
        "created": ts,
        "last_mention": ts,
        "mentions": 1,
        "mention_days": [_today()],
        "source": (source or "")[:200],
        "tier": "hot",
        # 分域：这条记忆是哪来的、能在哪些会话里用
        "scope": scope or infer_scope(uid),
        "uid": (uid or "")[:80],
        "gname": (gname or "")[:40],
    }
    save_records(d)
    return rid


def touch_record(rid: str) -> None:
    """记一次"被提到"（去重到天，天天提也只算一次频率）。"""
    d = load_records()
    r = d.get(rid)
    if not r:
        return
    today = _today()
    days = r.get("mention_days") or []
    if today not in days:
        days.append(today)
        r["mention_days"] = days[-30:]
        r["mentions"] = int(r.get("mentions") or 0) + 1
    r["last_mention"] = now_ts()
    save_records(d)


def forget(rid: str, mode: str = "delete") -> bool:
    """删除 / 更正 / 取消标注。"""
    d = load_records()
    r = d.get(rid)
    if not r:
        return False
    if mode == "supersede":
        r["superseded"] = True
        r["superseded_at"] = now_ts()
    elif mode == "recall_only":
        r["tier"] = "cold"
    elif mode == "unpin":
        r["tier"] = "hot"
    else:
        d.pop(rid, None)
    save_records(d)
    return True


# ═══════════════════════════════════════ 热度与冷却 ──
def _heat(r: dict, now: float | None = None) -> float:
    """heat = 0.45*F + 0.35*R + 0.20*O

    F: 有效提及频率（去重到天，30 天窗口内天数 / 30，封顶 1）
    R: 最近性（越近越接近 1，30 天衰减到 0）
    O: 出现次数（1 - exp(-n/5)，5 次以上接近 1）
    """
    now = now or now_ts()
    days = len(r.get("mention_days") or [])
    F = min(1.0, days / 30.0)
    age_days = max(0.0, (now - float(r.get("last_mention") or now)) / 86400.0)
    R = max(0.0, 1.0 - age_days / 30.0)
    n = int(r.get("mentions") or 1)
    O = 1.0 - math.exp(-n / 5.0)
    return round(0.45 * F + 0.35 * R + 0.20 * O, 4)


def update_tiers() -> dict:
    """跑一次冷却：把该降级的记忆降到冷库。返回统计。"""
    d = load_records()
    now = now_ts()
    demoted = 0
    for rid, r in d.items():
        if r.get("superseded"):
            continue
        if r.get("tier") == "pinned":
            continue
        h = _heat(r, now)
        r["heat"] = h
        if r.get("tier") == "cold":
            continue
        age_days = (now - float(r.get("created") or now)) / 86400.0
        silent_days = (now - float(r.get("last_mention") or now)) / 86400.0
        if age_days >= COLD_GRACE_DAYS and (h < COLD_HEAT or silent_days >= COLD_SILENT_DAYS):
            r["tier"] = "cold"
            r["demoted_at"] = now
            demoted += 1
    save_records(d)
    return {"demoted": demoted, "total": len(d)}


# ═══════════════════════════════════════ 连续性状态 ──
def load_state() -> dict:
    return _read_json(STATE, {"personality": "", "current_state": "",
                              "reflections": [], "counters": {}})


def save_state(d: dict) -> None:
    _write_json(STATE, d)


def set_continuity(personality: str = "", current_state: str = "") -> None:
    """写基础人格 / 当前相处状态。这两样**独立保留，不参与淘汰**。"""
    s = load_state()
    if personality:
        s["personality"] = personality.strip()[:800]
    if current_state:
        s["current_state"] = current_state.strip()[:800]
        s["current_state_at"] = now_ts()
    save_state(s)


def add_reflection(text: str) -> None:
    """记一条"猜想"。单独存，永远不当成真实经历。"""
    text = (text or "").strip()
    if not text:
        return
    s = load_state()
    refs = s.setdefault("reflections", [])
    refs.append({"ts": now_ts(), "text": text[:300]})
    s["reflections"] = refs[-50:]
    save_state(s)


def clear_continuity() -> None:
    s = load_state()
    s["personality"] = ""
    s["current_state"] = ""
    save_state(s)


# ═══════════════════════════════════════ 分域 ──
_WHO_SPLIT = re.compile(r"[、,，/／;；\s]+")


def _who_list(r: dict) -> list:
    """把记忆卡的 who 字段拆成人名列表。

    who 可能是 "四季亦然、deepsleep" / "冷魇/deepsleep" / "群聊" 这种。
    """
    who = (r.get("who") or "").strip()
    if not who:
        return []
    out = []
    for x in _WHO_SPLIT.split(who):
        x = x.strip()
        if x and len(x) <= 30:
            out.append(x)
    return out


def _name_match(a: str, b: str) -> bool:
    """昵称模糊匹配。emoji/后缀常常不一致：
    记忆里写「四季亦然」，成员列表里是「四季亦然🌨」—— 要能对上。
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def infer_scope(uid: str) -> str:
    """根据会话 id 判断作用域。"""
    uid = (uid or "").strip()
    if not uid:
        return "global"
    if uid.endswith("@chatroom"):
        return "group"
    return "dm"


def _visible(r: dict, uid: str, members: list) -> tuple:
    """判断这条记忆在当前会话能不能用。

    返回 (是否可见, 来源标签)。来源标签为空表示"就是本会话的事"，
    非空会拼在记忆前面，让模型知道「这是另一个群的事」。
    """
    scope = r.get("scope") or "global"
    src = r.get("uid") or ""

    # ① 全局事实 / 老数据（没有 uid）→ 永远可见
    if scope == "global" or not src:
        return True, ""

    # ② 本会话自己的记忆 → 永远可见
    if src == uid:
        return True, ""

    whos = _who_list(r)
    if not whos:
        # 不知道涉及谁的跨会话记忆，保守起见不带
        return False, ""

    # ③ 别的群的记忆：涉及的人必须也在当前会话的可见人里
    #    （群聊 → 群成员；私聊 → 就对方一个人）
    hit = None
    for w in whos:
        for m in members:
            if _name_match(w, m):
                hit = m
                break
        if hit:
            break
    if not hit:
        return False, ""

    # ④ 来源标签
    if scope == "group":
        gname = r.get("gname") or ""
        tag = "来自群「{}」".format(gname) if gname else "来自另一个群"
    else:
        tag = "来自和「{}」的私聊".format(hit)
    return True, tag


# ═══════════════════════════════════════════ BM25 召回 ──
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")


def _tokenize(text: str) -> list:
    """中文单字 + 英文单词。够用，且不需要分词库。"""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _bm25_index(docs: list):
    """给 docs 建内存倒排索引。docs = [(id, text)]。"""
    N = len(docs)
    df = {}
    doc_len = {}
    postings = {}
    for did, text in docs:
        toks = _tokenize(text)
        doc_len[did] = len(toks) or 1
        seen = set()
        for t in toks:
            postings.setdefault(t, []).append(did)
            if t not in seen:
                seen.add(t)
                df[t] = df.get(t, 0) + 1
    avgdl = (sum(doc_len.values()) / N) if N else 1.0
    return {"N": N, "df": df, "doc_len": doc_len, "postings": postings,
            "avgdl": avgdl, "docs": dict(docs)}


def _bm25_search(idx: dict, query: str, k1: float = 1.5, b: float = 0.75,
                 limit: int = 10) -> list:
    """返回 [(score, id)]，按分数降序。**不读时间，不惩罚年代。**"""
    toks = _tokenize(query)
    if not toks or not idx["N"]:
        return []
    N = idx["N"]
    scores = {}
    for t in toks:
        ids = idx["postings"].get(t)
        if not ids:
            continue
        df = idx["df"].get(t, 1)
        # 词频噪声过滤：只在语料足够大时才启用。
        # 踩过（2026-10-30 自测）：记忆卡只有 3 条时，"胶片"出现在全部 3 条里
        # → df/N = 100% > 35% → 被当噪声丢掉 → 查"胶片"返回"没找到"。
        # 小语料里高频词恰恰就是关键词，不能过滤。
        if N >= 10 and df / N > 0.6:
            continue
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for did in ids:
            dl = idx["doc_len"].get(did, 1)
            tf = 1.0
            denom = tf + k1 * (1 - b + b * dl / idx["avgdl"])
            scores[did] = scores.get(did, 0.0) + idf * (tf * (k1 + 1)) / denom
    if not scores:
        # BM25 一个 token 都没命中时，退回原样子串匹配
        # （中文短查询经常整词命中但单字 idf 太低）
        q = (query or "").strip().lower()
        if q:
            for did, text in idx["docs"].items():
                if q in (text or "").lower():
                    scores[did] = 1.0
        if not scores:
            return []
    # 原样子串命中的额外加权（精确短语 >> 单字散命中）
    q_low = (query or "").strip().lower()
    if q_low and len(q_low) >= 2:
        for did, text in idx["docs"].items():
            if q_low in (text or "").lower():
                scores[did] = scores.get(did, 0.0) + 2.0
    mx = max(scores.values()) or 1.0
    return sorted(((min(1.0, s / mx), d) for d, s in scores.items()),
                  reverse=True)[:limit]


def recall(query: str, budget_tokens: int = BUDGET_RECALL) -> dict:
    """按线索召回。返回 {status, items:[{text, kind, when, score}]}。

    status: matched（找到） / ambiguous（有候选但不够确定） / not_found
    """
    query = (query or "").strip()
    if not query:
        return {"status": "not_found", "items": []}

    # 通道 1：记忆卡
    cards = []
    for rid, r in load_records().items():
        if r.get("superseded"):
            continue
        cards.append((rid, "{} {} {}".format(
            r.get("topic") or "", r.get("text") or "", r.get("who") or "")))
    # 通道 2：归档原文（只取对话内容）
    arch = [(str(i), (a.get("text") or "")) for i, a in enumerate(archive_load())]

    idx_cards = _bm25_index(cards) if cards else None
    idx_arch = _bm25_index(arch) if arch else None

    hits = []
    if idx_cards:
        for sc, rid in _bm25_search(idx_cards, query, limit=RECALL_MAX_ITEMS * 2):
            r = load_records().get(rid) or {}
            hits.append({"score": sc, "kind": r.get("kind") or "fact",
                         "text": r.get("text") or "", "when": r.get("created"),
                         "source": "card"})
    if idx_arch and len(hits) < RECALL_MAX_ITEMS:
        for sc, aid in _bm25_search(idx_arch, query, limit=RECALL_MAX_ITEMS * 2):
            a = arch[int(aid)][1] if aid.isdigit() else ""
            if a:
                hits.append({"score": sc * 0.8, "kind": "archive",
                             "text": a[:200], "when": None, "source": "archive"})

    hits.sort(key=lambda x: -x["score"])
    strong = [h for h in hits if h["score"] >= RECALL_MIN_SCORE]

    # 预算内只留 3 条
    items = []
    used = 0
    for h in strong:
        cost = est_tokens(h["text"])
        if used + cost > budget_tokens or len(items) >= RECALL_MAX_ITEMS:
            break
        items.append(h)
        used += cost

    if not hits:
        return {"status": "not_found", "items": []}
    if not items:
        return {"status": "ambiguous", "items": strong[:RECALL_MAX_ITEMS]}
    if len(items) == 1 and items[0]["score"] >= 0.5:
        return {"status": "matched", "items": items}
    return {"status": "ambiguous", "items": items}


# ════════════════════════════════════════ 记忆快照 ──
def _fmt_when(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m-%d")
    except Exception:
        return "?"


def snapshot(uid: str = "", query: str = "", budget: int = BUDGET_TOTAL,
             members: list | None = None, gname: str = "") -> str:
    """生成注入 prompt 的记忆快照，总预算 1600 tokens。

    结构：
      【连续性】人格底色 + 当前相处状态（≤160）
      【近况】热度最高的几条活跃记忆（≤960）
      【线索】按 query 召回（≤480，只有给了 query 才用）
    """
    parts = []
    used = 0

    # ① 连续性（固定 160）
    s = load_state()
    cont = []
    if s.get("personality"):
        cont.append("你是谁：\n" + s["personality"])
    if s.get("current_state"):
        cont.append("最近的状态：\n" + s["current_state"])
    if cont:
        block = "【连续性】\n" + "\n".join(cont)
        block = _trim_to_tokens(block, BUDGET_PINNED)
        parts.append(block)
        used += est_tokens(block)

    # ② 活跃记忆（按热度取，预算 960）
    # **分域筛选**：全局事实 + 本会话的永远带；
    # 别的群/别的私聊的，只有当"涉及的人"也在当前会话里才带。
    d = load_records()
    mem_list = list(members or [])
    hot = []
    for rid, r in d.items():
        if r.get("superseded") or r.get("tier") == "cold":
            continue
        vis, tag = _visible(r, uid, mem_list)
        if not vis:
            continue
        h = r.get("heat")
        if h is None:
            h = _heat(r)
        hot.append((h, r, tag))
    hot.sort(key=lambda x: -x[0])
    lines = []
    hot_used = 0
    cross = []          # 跨会话的单独列一段，让模型明确知道"不是这个群的事"
    cross_used = 0
    for h, r, tag in hot:
        kind = {"promise": "约定", "preference": "喜好",
                "event": "事件", "fact": "事实"}.get(r.get("kind"), "事实")
        base = "[{}] {}（{}）".format(kind, r.get("text") or "",
                                     _fmt_when(r.get("last_mention")))
        if tag:
            # 跨会话记忆：带来源标签，单独一段
            line = "- （{}）{}".format(tag, base)
            cost = est_tokens(line)
            if cross_used + cost > BUDGET_HOT // 3 or len(cross) >= 4:
                continue
            cross.append(line)
            cross_used += cost
        else:
            line = "- " + base
            cost = est_tokens(line)
            if hot_used + cost > BUDGET_HOT or len(lines) >= 10:
                continue
            lines.append(line)
            hot_used += cost
    if lines:
        block = "【你记得的事】\n" + "\n".join(lines)
        parts.append(block)
        used += est_tokens(block)
    if cross:
        # 明确告诉模型：这是**别的会话**里的事，别当成当前群发生的
        block = ("【你在别的会话里记得的事】\n"
                 "（这些不是当前这个群里发生的事。只有当话题自然相关、"
                 "而且这里的人正好也认识当事人才提；别硬扯。）\n"
                 + "\n".join(cross))
        parts.append(block)
        used += est_tokens(block)

    # ③ 线索召回（预算 480，只在有 query 时）
    remaining = budget - used
    if query and remaining > 60:
        res = recall(query, budget_tokens=min(BUDGET_RECALL, remaining))
        if res.get("items"):
            ls = ["- " + (it.get("text") or "")[:160] for it in res["items"]]
            block = "【相关的旧事】\n" + "\n".join(ls)
            block = _trim_to_tokens(block, min(BUDGET_RECALL, remaining))
            parts.append(block)

    if not parts:
        return ""
    return "\n\n".join(parts)


def stats() -> dict:
    """给调试/状态面板用。"""
    d = load_records()
    a = archive_load()
    s = load_state()
    tiers = {}
    for r in d.values():
        t = r.get("tier") or "hot"
        tiers[t] = tiers.get(t, 0) + 1
    hs = [(_heat(r), r) for r in d.values() if not r.get("superseded")]
    hs.sort(key=lambda x: -x[0])
    return {
        "records": len(d),
        "archive": len(a),
        "tiers": tiers,
        "reflections": len(s.get("reflections") or []),
        "has_personality": bool(s.get("personality")),
        "has_state": bool(s.get("current_state")),
        "top": [{"text": r.get("text", "")[:40], "heat": h,
                 "tier": r.get("tier")} for h, r in hs[:5]],
    }
