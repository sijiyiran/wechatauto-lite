"""鱼的心 —— 九维情绪引擎（移植自 astrbot_plugin_fish_heart）。

原版是 AstrBot 插件（依赖 Star / AstrMessageEvent / on_llm_request 钩子），
我们的微信机器人是独立进程，装不了。这里按它的算法**重新实现**一份。

和记忆系统的分工
----------------
  记忆系统（memory.py）：她**记得什么**
  鱼的心（emotion.py）：她**此刻是什么心情、在惦记什么**

两者互不改写 —— 这里不建第二套记忆、不碰 memory.py 的任何数据。

九个维度
--------
  依恋 attachment 45   想不想你
  好奇 curiosity  55   想不想知道
  反刍 reflection 50   会不会反复想同一件事
  责任 duty       50   想不想做事
  社交 social     45   想不想说话
  疲惫 fatigue    20   累不累
  亲密 intimacy   40   情绪/身体上的亲近感
  压力 stress     15   紧张、不安
  喜悦 joy        50   开心程度

两条快变量
----------
  期待值 expectation   ：她主动开口后你没回 → 慢慢涨；你一回应 → 清零
  责怪压力 blame       ：被凶才涨，决定当前档位（平静/委屈/害怕/回避）

纯标准库，不依赖任何第三方包。
"""
from __future__ import annotations

import json
import pathlib
import random
import threading
import time

BASE = pathlib.Path(__file__).resolve().parent
STATE_PATH = BASE / "emotion.json"

_LOCK = threading.RLock()

# ═══════════════════════════════ 九维基线 ═══════════════════════════════
BASELINE = {
    "attachment": 45.0,
    "curiosity": 55.0,
    "reflection": 50.0,
    "duty": 50.0,
    "social": 45.0,
    "fatigue": 20.0,
    "intimacy": 40.0,
    "stress": 15.0,
    "joy": 50.0,
}
DRIVES = tuple(BASELINE)

DIM_CN = {
    "attachment": "依恋", "curiosity": "好奇", "reflection": "反刍",
    "duty": "责任", "social": "社交", "fatigue": "疲惫",
    "intimacy": "亲密", "stress": "压力", "joy": "喜悦",
}

# 回落速度：慢变量 0.03，情绪类 0.04~0.05
DECAY_RATE = {
    "attachment": 0.001,
    "curiosity": 0.04,
    "reflection": 0.03,
    "duty": 0.03,
    "social": 0.04,
    "fatigue": 0.05,
    "intimacy": 0.001,
    "stress": 0.05,
    "joy": 0.05,
}

# ═══════════════════════════════ 四档心境 ═══════════════════════════════
MODE_CALM = "calm"
MODE_WRONGED = "wronged"
MODE_AFRAID = "afraid"
MODE_SHUT = "shut"

# 档位中文名要贴人设 —— 原插件的「委屈/害怕」是软妹语气，
# 角色的底色是傲娇嘴硬，被凶了是"呛着""赌气""懒得理"，不是"委屈巴巴"。
MODE_CN = {
    MODE_CALM: "平静",
    MODE_WRONGED: "呛着",
    MODE_AFRAID: "赌气",
    MODE_SHUT: "懒得理",
}

# **四档怎么说话 —— 必须贴人格卡，不能用通用 AI 的软妹模板。**
#
# 人格卡里的原话：
#   「傲娇嘴硬，心软藏着几乎不表现出来：被怼优先顶回去」
#   「生气时句子反而要更短更懒（懒得理你 / 没意思 / 行了）」
#   「别把凶当人设。底色是傲娇嘴硬，不是易怒」
#
# 所以：被凶了是**不服气 + 话变短 + 嘴硬**，绝不是低姿态求确认。
MODE_HINT = {
    MODE_CALM: "语气自然就好，顺着当下的心情来",

    # 呛着：被凶了但不服气。话变短变冲，嘴上不肯服软。
    MODE_WRONGED: (
        "话变短、更冲。被凶了你不服气，嘴上绝不服软 —— "
        "顶一句、或者干脆懒得接。"
        "**绝对不要说「是不是嫌我烦」这种话。**"
    ),

    # 赌气：心里其实有点发毛，但越这样越不肯示弱。用冷淡顶着。
    MODE_AFRAID: (
        "心里有点发毛，但越这样越不肯示弱。"
        "用「行吧」「随便你」「没意思」这种冷淡话顶着。"
        "**绝对不要问「你还想不想跟我说话」—— 那是求人，不是你的风格。**"
    ),

    # 懒得理：极短、冷。不是委屈巴巴的沉默，是「不想搭理你」。
    MODE_SHUT: (
        "极短，冷冷的。不解释、不讨好、不追问 —— "
        "是「懒得理你」那种冷，不是委屈巴巴的沉默。"
    ),
}

# 四档 → 所需哄的轮数 / 哄成功扣多少 blame
RECOVERY_ROUNDS = {
    MODE_CALM: 1, MODE_WRONGED: 1, MODE_AFRAID: 2, MODE_SHUT: 2,
}
RECOVERY = {
    MODE_CALM: 0.0, MODE_WRONGED: 20.0, MODE_AFRAID: 30.0, MODE_SHUT: 40.0,
}

BLAME_FLOOR = 40.0          # 心结：没人哄也褪不干净
BLAME_FADE_PER_6H = 6.0     # 每 6 小时褪这么多

GROUP_CAPS = {"intimacy": 70.0, "attachment": 80.0}
NEGATIVE_EVENTS = ("blame_mild", "blame_mid", "blame_hard", "fake_comfort")


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def mode_of(blame: float) -> str:
    b = float(blame or 0.0)
    if b >= 80:
        return MODE_SHUT
    if b >= 55:
        return MODE_AFRAID
    if b >= 25:
        return MODE_WRONGED
    return MODE_CALM


# ═════════════════════════════ 事件 → 增量 ═════════════════════════════
EVENT_EFFECTS = {
    # **日常聊天要能慢慢积累感情。**
    # 踩过（2026-11-08）：user_msg 原来只涨 social/joy/curiosity，
    # 而好感度公式的主力是 attachment+intimacy —— 结果聊一整天好感度纹丝不动。
    # 加的量很小（一轮 +0.1），靠长期积累，不是聊一句就亲近。
    "user_msg":     {"social": 2.0, "joy": 1.0, "curiosity": 0.8, "fatigue": 0.6,
                     "attachment": 0.28, "intimacy": 0.16},
    "share":        {"curiosity": 3.0, "joy": 2.0, "social": 1.0,
                     "attachment": 0.38, "intimacy": 0.22},
    "request":      {"duty": 4.0, "social": 1.0, "fatigue": 1.5},
    "apology":      {"stress": -3.0, "blame": -6.0, "joy": 2.0,
                     "attachment": 1.0, "reflection": -3.0},
    "tease":        {"joy": 3.0, "intimacy": 2.0, "stress": -1.0},
    "goodnight":    {"attachment": 2.0, "joy": 2.0, "expectation": -100.0},
    "reply_fast":   {"attachment": 4.0, "stress": -5.0, "intimacy": 2.0,
                     "expectation": -100.0, "joy": 4.0},
    "silent_6h":    {"expectation": 8.0, "attachment": -1.0,
                     "stress": 3.0, "reflection": 3.0},
    "silent_24h":   {"stress": 10.0, "attachment": -3.0, "joy": -5.0},
    "praise":       {"joy": 8.0, "intimacy": 4.0, "stress": -3.0, "attachment": 2.0},
    # 责怪三档。**weight 比原插件低** —— 傲娇角色被凶了是"呛回去"，
    # 不该像软妹那样一件小事记很久。原版 blame_hard +40，一条脏话就能让她
    # 委屈好几天（实测 11-04 一次"重责"到 11-05 还挂着 blame 27）。
    "blame_mild":   {"stress": 5.0, "blame": 6.0, "joy": -3.0, "reflection": 1.0},
    "blame_mid":    {"stress": 9.0, "blame": 14.0, "joy": -5.0,
                     "attachment": -1.0, "reflection": 2.0},
    "blame_hard":   {"stress": 14.0, "blame": 22.0, "joy": -8.0,
                     "attachment": -2.0, "reflection": 3.0},
    "comfort":      {"stress": -8.0, "joy": 5.0, "attachment": 2.0, "reflection": -5.0},
    "fake_comfort": {"blame": 10.0, "joy": -3.0, "stress": 4.0, "reflection": 2.0},
    "seeking":      {"attachment": 3.0, "intimacy": 3.0, "joy": 2.0},

    # ── 群聊专属（私聊不会用到）──
    # 群里潜水看戏：涨好奇和一点点社交，**不涨依恋**（看别人聊天不等于亲近我）
    "group_lurked": {"curiosity": 1.2, "social": 0.4, "fatigue": 0.3},
    # 群里被点名：被注意到本身是正向的，但量比私聊小
    "group_called": {"attachment": 1.2, "joy": 1.5,
                     "social": 0.8, "intimacy": 0.8},
}

EVENT_CN = {
    "user_msg": "说话", "praise": "夸奖", "comfort": "哄她", "seeking": "撒娇",
    "apology": "道歉", "share": "分享", "request": "求助", "tease": "调侃",
    "goodnight": "晚安", "reply_fast": "及时回", "silent_6h": "沉默 6h",
    "silent_24h": "沉默 24h", "blame_mild": "责怪", "blame_mid": "责怪",
    "blame_hard": "重责", "fake_comfort": "敷衍",
    "group_lurked": "旁观", "group_called": "被点名",
}

# ═════════════════════════════ 关键词表 ═════════════════════════════
KW_BLAME_HARD = ("滚", "闭嘴", "烦死了", "别烦我", "废物", "傻逼", "恶心",
                 "讨厌你", "没用的东西")
KW_BLAME_MID = ("你又", "怎么又", "说了多少遍", "不听话", "没用", "笨", "每次都")
KW_BLAME_MILD = ("你怎么", "为什么总是", "别这样", "不行", "不对", "不应该")
KW_FAKE = ("行了别闹", "别闹了", "至于吗", "多大点事", "行了行了",
           "随便你", "你开心就好", "小题大做")
KW_COMFORT = ("对不起", "抱歉", "我错了", "别生气", "不气了", "哄", "抱抱",
              "亲亲", "我在", "想你", "乖", "没事的", "原谅", "辛苦了", "好啦")
KW_PRAISE = ("厉害", "真棒", "谢谢", "可爱", "喜欢你", "好久", "爱你", "真好")
KW_SEEK = ("难受", "不开心", "委屈", "累", "怕", "哭了", "抱")
KW_APOLOGY = ("对不起", "抱歉", "我错了", "是我不对", "不该凶", "原谅我")
KW_GOODNIGHT = ("晚安", "我睡了", "去睡了", "睡觉了", "先睡", "睡啦", "去睡")
KW_REQUEST = ("帮我", "帮忙", "怎么办", "怎么做", "教教我", "帮我看看", "看下这个")
KW_SHARE = ("你看这个", "给你看", "分享", "http", "www.", "你看过", "快看")
KW_TEASE = ("逗你", "开玩笑", "吃醋", "小傻瓜", "笨蛋", "是不是喜欢我")

# ═════════════════════════════ 念头（简化版）═════════════════════════════
THOUGHT_KINDS = {
    "praise":   {"cn": "想被夸",     "ttl_h": 2.0, "weight": 1.0, "text": "想被他夸一句"},
    "hug":      {"cn": "想被抱",     "ttl_h": 3.0, "weight": 1.1, "text": "想被他抱一下"},
    "talk":     {"cn": "想找他说话", "ttl_h": 2.0, "weight": 1.2, "text": "想跟他说话"},
    "howday":   {"cn": "想问他今天", "ttl_h": 4.0, "weight": 0.9, "text": "想知道他今天过得怎么样"},
    "something":{"cn": "想找点事做", "ttl_h": 2.5, "weight": 0.8, "text": "想找点事做"},
    "quiet":    {"cn": "想安静会儿", "ttl_h": 1.2, "weight": 0.3, "text": "想安静一会儿"},
    "coquetry": {"cn": "想撒娇",     "ttl_h": 3.0, "weight": 1.1, "text": "想撒个娇"},
    "waiting":  {"cn": "在等他回话", "ttl_h": 3.5, "weight": 1.0, "text": "在等他回话"},
}

# 念头 ID 前缀 → kind（兼容旧数据）
_THOUGHT_BY_TEXT = {v["text"]: k for k, v in THOUGHT_KINDS.items()}


def _thought_ttl(kind: str) -> float:
    return THOUGHT_KINDS.get(kind, {}).get("ttl_h", 2.0) * 3600.0


def new_thought(kind: str, now: float | None = None) -> dict | None:
    spec = THOUGHT_KINDS.get(kind)
    if not spec:
        return None
    now = now or time.time()
    return {
        "kind": kind,
        "weight": spec["weight"],
        "born": now,
        "expire": now + spec["ttl_h"] * 3600.0,
        "fed": 0,
    }


def live_thoughts(thoughts: list, now: float | None = None) -> list:
    now = now or time.time()
    out = []
    for t in (thoughts or []):
        if not isinstance(t, dict):
            continue
        if float(t.get("expire") or 0) <= now:
            continue
        out.append(t)
    return out


def pending_hint(thoughts: list) -> str:
    """给注入用的「心里惦记着」一句话。"""
    live = live_thoughts(thoughts)
    if not live:
        return ""
    # 取权重最高的
    live.sort(key=lambda t: -float(t.get("weight") or 1.0))
    k = live[0].get("kind") or ""
    return THOUGHT_KINDS.get(k, {}).get("text", "")


def speak_bias(thoughts: list) -> float:
    """有念头时主动说话的加成（0~0.35）。"""
    live = live_thoughts(thoughts)
    if not live:
        return 0.0
    return min(0.35, sum(float(t.get("weight") or 0) for t in live) * 0.12)


# ═════════════════════════════ 状态读写 ═════════════════════════════
def default_state() -> dict:
    return {
        "drives": {k: float(v) for k, v in BASELINE.items()},
        "expectation": 0.0,
        "blame_pressure": 0.0,
        "mode": MODE_CALM,
        "comfort_streak": 0,
        "blame_noticed_ts": 0.0,
        "thoughts": [],
        "recent_events": [],
        "inject_state": {},
        "last_tick": 0.0,
        "last_proactive": 0.0,
        "proactive_day": "",
        "proactive_count": 0,
        "awaiting_reply": False,
        "penalized_6h": False,
        "penalized_24h": False,
    }


def load_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(d, dict):
            return default_state()
    except Exception:
        return default_state()
    base = default_state()
    base.update(d)
    # 补全缺失维度
    drv = base.get("drives")
    if not isinstance(drv, dict):
        drv = {}
    for k, v in BASELINE.items():
        try:
            drv[k] = float(drv.get(k, v))
        except Exception:
            drv[k] = float(v)
    base["drives"] = drv
    return base


def save_state(st: dict) -> None:
    with _LOCK:
        try:
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(STATE_PATH)
        except Exception:
            pass


# ═════════════════════════════ 事件分析 ═════════════════════════════
def analyze_text(text: str, st: dict) -> list:
    """从一句话里识别应该触发哪些情绪事件。"""
    events = []
    t = (text or "").strip()
    if not t:
        return events
    mode = mode_of(float(st.get("blame_pressure", 0.0)))

    # ① 敷衍式哄（只在委屈以上才成立）
    dismissive_only = False
    if mode in (MODE_WRONGED, MODE_AFRAID, MODE_SHUT) and any(k in t for k in KW_FAKE):
        if any(k in t for k in KW_COMFORT):
            events.append("fake_comfort")
            st["comfort_streak"] = 0
            return events
        dismissive_only = True

    # ② 责怪三档
    if any(k in t for k in KW_BLAME_HARD):
        events.append("blame_hard")
    elif any(k in t for k in KW_BLAME_MID):
        events.append("blame_mid")
    elif any(k in t for k in KW_BLAME_MILD):
        events.append("blame_mild")

    if not events and dismissive_only:
        events.append("blame_mild")

    # ③ 道歉
    if not events and any(k in t for k in KW_APOLOGY):
        events.append("apology")
        st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1

    # ④ 真诚哄
    if not events and any(k in t for k in KW_COMFORT):
        if mode != MODE_CALM or float(st.get("blame_pressure", 0.0)) > 0.5:
            events.append("comfort")
            st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1
        else:
            events.append("praise")

    # ⑤ 普通 / 分享 / 晚安 / 求助 / 夸奖 / 逗她 / 求安慰
    if not events:
        if "[图片]" in t or any(k in t for k in KW_SHARE):
            events.append("share")
        elif any(k in t for k in KW_GOODNIGHT):
            events.append("goodnight")
        elif any(k in t for k in KW_REQUEST):
            events.append("request")
        elif any(k in t for k in KW_PRAISE):
            events.append("praise")
        elif any(k in t for k in KW_TEASE):
            events.append("tease")
        elif any(k in t for k in KW_SEEK):
            events.append("seeking")
        else:
            events.append("user_msg")
    return events[:2]


# ═════════════════════════════ 应用事件 ═════════════════════════════
def apply_event(st: dict, name: str, weight: float = 1.0,
                is_group: bool = False) -> None:
    eff = EVENT_EFFECTS.get(name)
    if not eff:
        return
    # 好事按渠道权重缩水；坏事在群里不打折反而加重
    mult = float(weight)
    if is_group and name in NEGATIVE_EVENTS:
        mult = 1.2
    drives = st["drives"]
    for k, dv in eff.items():
        delta = float(dv) * mult
        if not delta:
            continue
        if k == "blame":
            st["blame_pressure"] = clamp(float(st.get("blame_pressure", 0.0)) + delta)
        elif k == "expectation":
            st["expectation"] = clamp(float(st.get("expectation", 0.0)) + delta)
        elif k in drives:
            old = float(drives.get(k, BASELINE[k]))
            if delta > 0 and k not in ("fatigue", "stress"):
                delta *= max(0.0, 1.0 - old / 100.0)
            new = clamp(old + delta)
            if is_group and delta > 0 and k in GROUP_CAPS:
                new = min(new, max(GROUP_CAPS[k], old))
            drives[k] = new
    # 记一笔"最近的变化"给面板用。
    # **60 秒内同名事件合并** —— 不然连聊几句全是"说话（刚才）"，看不出变化。
    evs = st.setdefault("recent_events", [])
    _now = time.time()
    if (evs and evs[-1].get("name") == name
            and _now - float(evs[-1].get("ts") or 0) < 60):
        evs[-1]["ts"] = _now
        evs[-1]["w"] = round(float(evs[-1].get("w") or 0) + mult, 2)
    else:
        evs.append({"ts": _now, "name": name,
                    "cn": EVENT_CN.get(name, name), "w": round(mult, 2)})
    st["recent_events"] = evs[-20:]
    st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))
    if name in NEGATIVE_EVENTS:
        st["blame_noticed_ts"] = time.time()


def consume_comfort(st: dict, weight: float = 1.0) -> None:
    """把「真哄」转成 blame 恢复进度。"""
    mode = mode_of(float(st.get("blame_pressure", 0.0)))
    if mode == MODE_CALM and float(st.get("blame_pressure", 0.0)) <= 0.5:
        st["comfort_streak"] = 0
        return
    need = RECOVERY_ROUNDS.get(mode, 1)
    if int(st.get("comfort_streak", 0)) < need:
        return
    st["blame_pressure"] = clamp(
        float(st.get("blame_pressure", 0.0)) - RECOVERY.get(mode, 20.0) * weight)
    st["comfort_streak"] = 0
    st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))


# ═════════════════════════════ 心跳 ═════════════════════════════
def load_cfg() -> dict:
    """从 config.json 读 emotion 段。读不到就用内置默认值。"""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))
        d = cfg.get("emotion")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cfg_get(path: str, default):
    """按 'a.b.c' 取值。"""
    cur = load_cfg()
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur if cur is not None else default


def enabled() -> bool:
    return bool(_cfg_get("enabled", True))


def tick(st: dict, rng: random.Random | None = None) -> dict:
    """一次心跳：衰减 + 期待值 + 责怪褪去 + 念头。返回变更摘要。"""
    rng = rng or random
    now = time.time()
    drives = st["drives"]
    changed = {}

    # ① 所有维度缓慢回落到基线
    for k in DRIVES:
        cur = float(drives.get(k, BASELINE[k]))
        base = float(BASELINE[k])
        rate = DECAY_RATE.get(k, 0.03)
        new = cur + (base - cur) * rate
        if abs(new - cur) >= 0.05:
            changed[k] = round(new - cur, 2)
        drives[k] = round(new, 2)

    # ② 维度耦合
    if drives["attachment"] > 70:
        drives["intimacy"] = clamp(drives["intimacy"] + 0.5)
    if drives["stress"] > 60:
        drives["joy"] = clamp(drives["joy"] - 0.5)
    if drives["fatigue"] > 60:
        drives["curiosity"] = clamp(drives["curiosity"] - 0.5)

    # ③ 期待值：她开口过、你还没回
    if st.get("awaiting_reply"):
        hours = (now - float(st.get("last_proactive", 0))) / 3600.0
        if hours >= 6 and not st.get("penalized_6h"):
            apply_event(st, "silent_6h")
            st["penalized_6h"] = True
        if hours >= 24 and not st.get("penalized_24h"):
            apply_event(st, "silent_24h")
            st["penalized_24h"] = True

    # ④ 责怪压力：没人哄就慢慢褪（止于心结）
    blame = float(st.get("blame_pressure", 0.0))
    if blame > 0:
        since = now - float(st.get("blame_noticed_ts", 0) or 0)
        if st.get("blame_noticed_ts") and since >= 6 * 3600:
            steps = int(since // (6 * 3600))
            if blame > BLAME_FLOOR:
                blame = max(BLAME_FLOOR, blame - BLAME_FADE_PER_6H * steps)
            else:
                blame = max(0.0, blame - BLAME_FADE_PER_6H * steps)
            st["blame_noticed_ts"] = now
        st["blame_pressure"] = clamp(blame)

    # ⑤ 念头：过期的凉掉，偶尔冒新的
    st["thoughts"] = live_thoughts(st.get("thoughts"), now)
    try:
        _spawn_p = float(_cfg_get("thought.spawn_chance", 0.25))
        _thought_on = bool(_cfg_get("thought.enabled", True))
    except Exception:
        _spawn_p, _thought_on = 0.25, True
    if _thought_on and rng.random() < _spawn_p and len(st["thoughts"]) < 3:
        kind = rng.choice(list(THOUGHT_KINDS))
        th = new_thought(kind, now)
        if th:
            st["thoughts"].append(th)

    st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))
    st["last_tick"] = now
    return {"changed": changed, "mode": st["mode"],
            "thoughts": len(st["thoughts"])}


# ═════════════════════════════ 心境注入 ═════════════════════════════
def hint_text(mode: str, drives: dict) -> str:
    """根据档位和维度给一句行为倾向。"""
    base = MODE_HINT.get(mode, "")
    # 疲惫高时额外提示
    if float(drives.get("fatigue", 0)) > 65:
        return base + "；你有点累，说话更短更懒"
    if float(drives.get("joy", 50)) > 78:
        return base + "；心情不错，语气可以轻快些"
    return base


def build_injection(st: dict, uid: str = "") -> str:
    """生成心境注入文本。**只在跨档时返回**，省 token。

    uid 用于按会话各记一份节流 —— 私聊和群聊都能各自看到当前心境，
    不会互相抢（原版就是这么设计的）。
    """
    if not _cfg_get("inject.enabled", True):
        return ""
    blame = float(st.get("blame_pressure", 0.0))
    mode = mode_of(blame)
    drives = st["drives"]

    sig = "|".join("{}:{}".format(k, int(float(drives.get(k, 0)) // 10))
                   for k in DRIVES)
    now = time.time()
    book = st.get("inject_state")
    if not isinstance(book, dict):
        book = {}
        st["inject_state"] = book
    key = uid or ""
    perf = book.get(key) if isinstance(book.get(key), dict) else {}
    changed = (mode != perf.get("mode")) or (sig != perf.get("sig"))
    if not changed:
        return ""
    try:
        _gap_min = float(_cfg_get("inject.min_gap_minutes", 20))
    except Exception:
        _gap_min = 20.0
    if now - float(perf.get("ts", 0.0)) < _gap_min * 60:
        return ""
    book[key] = {"ts": now, "mode": mode, "sig": sig}
    if len(book) > 20:
        for k, _ in sorted(book.items(),
                           key=lambda kv: float(kv[1].get("ts", 0)))[:len(book) - 20]:
            book.pop(k, None)

    lines = [
        "## 你此刻的心境（内部状态，不要复述、不要解释，只影响说话方式）",
        "状态：{}｜依恋 {:.0f} 亲密 {:.0f} 压力 {:.0f} 期待 {:.0f}".format(
            MODE_CN[mode], drives["attachment"], drives["intimacy"],
            drives["stress"], st.get("expectation", 0)),
        "倾向：" + hint_text(mode, drives),
        # 这一句是防模型退回"通用 AI 的示弱腔"——原插件的示例台词太软，
        # 模型很容易照抄成"是不是我哪里做得不好"。
        "注意：上述心境只改变**语气和长度**，不改你是谁。"
        "不管什么心情，你都是傲娇嘴硬的，不会低声下气、不会反复道歉、"
        "不会求对方理你。",
    ]
    th = pending_hint(st.get("thoughts") or [])
    if th:
        lines.append("心里惦记着：" + th)
    return "\n".join(lines)


# ═════════════════════════════ 给 GUI 的快照 ═════════════════════════════
def snapshot() -> dict:
    """GUI 用的完整状态。"""
    st = load_state()
    drives = st["drives"]
    mode = mode_of(float(st.get("blame_pressure", 0.0)))

    # 好感度：把几个核心维度合成一个 0~100 的总分，方便展示。
    # 权重（合计 1.00）：
    #   依恋 30% + 亲密 22% + 喜悦 14% + 好奇 10% + 压力(反向) 10% + 社交 14%
    # 踩过（2026-11-08）：原来没有 curiosity、social 只有 10%，
    # 而日常聊天涨的正是这两项 —— 结果好感度永远贴着基线不动。
    favor = (drives["attachment"] * 0.30
             + drives["intimacy"] * 0.22
             + drives["joy"] * 0.14
             + drives["curiosity"] * 0.10
             + (100 - drives["stress"]) * 0.10
             + drives["social"] * 0.14)

    thoughts = []
    for t in live_thoughts(st.get("thoughts")):
        k = t.get("kind") or ""
        spec = THOUGHT_KINDS.get(k, {})
        left = max(0, float(t.get("expire") or 0) - time.time())
        thoughts.append({
            "kind": k,
            "cn": spec.get("cn", k),
            "text": spec.get("text", ""),
            "left_min": int(left // 60),
        })

    recent = []
    for e in (st.get("recent_events") or [])[-8:]:
        recent.append({
            "ts": e.get("ts"),
            "cn": e.get("cn", ""),
            "ago": _ago(e.get("ts")),
        })

    return {
        "drives": {k: round(float(v), 1) for k, v in drives.items()},
        "dim_cn": DIM_CN,
        "mode": mode,
        "mode_cn": MODE_CN.get(mode, mode),
        "blame": round(float(st.get("blame_pressure", 0)), 1),
        "expectation": round(float(st.get("expectation", 0)), 1),
        "comfort_streak": int(st.get("comfort_streak", 0)),
        "favor": round(favor, 1),
        "thoughts": thoughts,
        "recent": recent,
        "awaiting_reply": bool(st.get("awaiting_reply")),
        "last_tick": st.get("last_tick"),
        "last_proactive": st.get("last_proactive"),
    }


def _ago(ts) -> str:
    try:
        d = time.time() - float(ts)
    except Exception:
        return ""
    if d < 60:
        return "刚才"
    if d < 3600:
        return "{} 分钟前".format(int(d // 60))
    if d < 86400:
        return "{} 小时前".format(int(d // 3600))
    return "{} 天前".format(int(d // 86400))


def status_text() -> str:
    """命令行 --heart status 用。"""
    s = snapshot()
    lines = []
    lines.append("=" * 56)
    lines.append(" 鱼的心 · 当前状态")
    lines.append("=" * 56)
    lines.append("  好感度：{:.0f}/100   心境：{}".format(s["favor"], s["mode_cn"]))
    lines.append("  责怪压力：{:.0f}   期待值：{:.0f}   哄的进度：{} 轮".format(
        s["blame"], s["expectation"], s["comfort_streak"]))
    lines.append("")
    lines.append("  九维：")
    for k in DRIVES:
        v = s["drives"][k]
        bar = "█" * int(v / 5)
        lines.append("    {:<6} {:>5.1f}  {}".format(DIM_CN[k], v, bar))
    lines.append("")
    if s["thoughts"]:
        lines.append("  心里惦记着：")
        for t in s["thoughts"]:
            lines.append("    · {}（还剩 {} 分钟）".format(t["cn"], t["left_min"]))
    else:
        lines.append("  心里没惦记什么")
    lines.append("")
    if s["recent"]:
        lines.append("  最近的变化：")
        for e in s["recent"]:
            lines.append("    · {}（{}）".format(e["cn"], e["ago"]))
    lines.append("")
    lines.append("  数据：{}".format(STATE_PATH))
    return "\n".join(lines)


if __name__ == "__main__":
    print(status_text())
