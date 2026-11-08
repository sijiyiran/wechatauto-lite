"""让机器人把用户本地的文件发给他。

场景：主人在私聊里说「把桌面上的金币修改器.zip 发给我」，
她找到文件、通过微信发过去、再回一句「发了」。

设计要点
--------
1. **只认主人**。这个能力等于「读你硬盘」，不能对任何人开放。
   用 config 的 relay.owner_wxid 判断，别人发同样的话不动。
2. **只搜白名单目录**。桌面 / 下载 / 文档。绝不允许 `..` 路径穿越。
3. **模糊匹配**。用户不会打全名：「金币修改器」要能匹配「金币修改器.zip」。
4. **大小上限**。超过 100MB 直接拒。
5. **快**。第一版遍历桌面要 41 秒 —— 微信里没人等得起。
   现在分两阶段：先扫顶层（毫秒级），没有再递归两层，并加进程内缓存。
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import re
import time

MAX_SIZE = 100 * 1024 * 1024
MAX_DEPTH = 2

SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".svn", "AppData",
    "$RECYCLE.BIN", "System Volume Information", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", "Library", "System",
}

# 句子里带这些前缀要先剥掉，否则会被当成文件名的一部分
_PREFIX_JUNK = re.compile(
    r"^(?:能不能|可不可以|可以|麻烦你?|帮我|帮忙|给我|你|请|把|将|"
    r"桌面上的?|桌面的?|电脑上的?|的文件|文件|那个|这个|我的|一个|一下|我)\s*"
)

_REQUEST_RE = re.compile(
    r"(?:把|将)?\s*(?P<what>[^，,。;；\n]{1,80}?)\s*"
    r"(?:发|传|送|给)\s*(?:给)?\s*我",
    re.I,
)
_REQUEST_RE2 = re.compile(
    r"(?:发|传|送)(?:一下|个|份)?\s*(?P<what>[^，,。;；\n]{1,80})",
    re.I,
)
_NOT_FILE_WORDS = re.compile(
    r"^(?:我|你|他|她|微信|消息|句话|短信|语音|位置|红包|钱|图|照片|视频|表情|"
    r"这个|那个|一下|个|张|条|遍|顿|声|文件)$"
)
_HAS_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")

# 进程内目录缓存：{(dir, depth): [(path, size, mtime)]}，5 分钟有效
_DIR_CACHE: dict = {}
_CACHE_TTL = 300.0


def parse_request(text: str) -> str:
    """从一句话里抽出「要发的文件描述」。抽不出返回空串。"""
    s = (text or "").strip()
    if not s or len(s) < 3:
        return ""
    for pat in (_REQUEST_RE, _REQUEST_RE2):
        m = pat.search(s)
        if not m:
            continue
        what = (m.group("what") or "").strip()
        # 反复剥前缀，因为可能叠了两层（"能不能把桌面上的..."）
        for _ in range(4):
            new = _PREFIX_JUNK.sub("", what).strip()
            if new == what:
                break
            what = new
        what = re.sub(r"\s+(?:文件|这个文件|那个文件)$", "", what)
        what = what.strip(" 　\"'“”‘’")
        if not what or len(what) < 2:
            continue
        if _NOT_FILE_WORDS.match(what):
            continue
        return what
    return ""


# ─────────────────────────────────────── 桌面/下载目录 ──
def _shell_folder(csidl: int) -> pathlib.Path | None:
    """用 Win32 API 拿系统文件夹真实路径（桌面可能被重定向到 F:\\zhuomian）。"""
    try:
        buf = ctypes.create_unicode_buffer(260)
        r = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        if r == 0 and buf.value:
            p = pathlib.Path(buf.value)
            if p.exists():
                return p
    except Exception:
        pass
    return None


def search_dirs() -> list:
    """返回允许搜索的目录列表（去重、只保留存在的）。"""
    out, seen = [], set()

    def add(p):
        if not p:
            return
        try:
            p = pathlib.Path(p)
            if p.exists() and p.is_dir():
                k = str(p).lower()
                if k not in seen:
                    seen.add(k)
                    out.append(p)
        except Exception:
            pass

    add(_shell_folder(0x0010))   # 桌面（含重定向）
    add(_shell_folder(0x0005))   # 我的文档
    home = pathlib.Path.home()
    for name in ("Desktop", "桌面", "Downloads", "下载", "Documents", "文档"):
        add(home / name)
    add(pathlib.Path(r"F:\zhuomian"))
    return out


def _scan(dirs, depth: int, cur_depth: int) -> list:
    """扫一层目录，返回 [(path, size)]。dir 级结果带缓存。"""
    out = []
    for d in dirs:
        key = (str(d).lower(), cur_depth)
        now = time.time()
        cached = _DIR_CACHE.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            out.extend(cached[1])
            continue
        items = []
        try:
            with os.scandir(str(d)) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            items.append((pathlib.Path(e.path), e.stat().st_size))
                        elif e.is_dir(follow_symlinks=False) and cur_depth < depth:
                            if e.name in SKIP_DIRS or e.name.startswith("."):
                                continue
                            items.extend(
                                (p, s) for p, s in _scan([pathlib.Path(e.path)],
                                                          depth, cur_depth + 1))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass
        _DIR_CACHE[key] = (now, items)
        out.extend(items)
    return out


def _score(name_lower: str, want_lower: str) -> float:
    """文件名和用户描述像不像。越高越像。"""
    if not want_lower:
        return 0.0
    stem = name_lower
    for ext in (".zip", ".rar", ".7z", ".exe", ".pdf", ".docx", ".xlsx",
                ".pptx", ".txt", ".png", ".jpg", ".mp4", ".mp3"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    if name_lower == want_lower or stem == want_lower:
        return 100.0
    if stem.startswith(want_lower):
        return 80.0
    if want_lower in stem:
        return 70.0
    if stem and stem in want_lower:
        return 50.0
    # 逐字重合：要求**用户说的字几乎都在文件名里**才算，
    # 否则"不存在的文件xyzabc"也能蹭上随便一个 exe（实测踩过）。
    wchars = [c for c in want_lower if not c.isspace()]
    if not wchars:
        return 0.0
    matched = sum(1 for c in wchars if c in stem)
    ratio = matched / len(wchars)
    if ratio >= 0.85 and len(wchars) >= 3:
        return min(40.0, 40.0 * ratio)
    return 0.0


def find_file(desc: str, max_results: int = 6) -> list:
    """在允许的目录里找文件。返回 [(score, path)]，按分数降序。

    两阶段：先扫顶层（快），不够再递归。这样「桌面上的 zip」通常
    第一轮就命中，不用等递归。
    """
    want = (desc or "").strip().lower()
    if not want:
        return []
    if ".." in want or want.startswith("/") or re.match(r"^[a-z]:", want):
        return []

    dirs = search_dirs()
    # 阶段 1：只扫顶层
    files = _scan(dirs, depth=0, cur_depth=0)
    hits = _rank(files, want)
    # 阶段 2：顶层没找到像样的才递归
    if not hits or hits[0][0] < 60:
        files = _scan(dirs, depth=MAX_DEPTH, cur_depth=0)
        hits = _rank(files, want)

    hits.sort(key=lambda x: (-x[0], -x[1].stat().st_size if x[1].exists() else 0))
    return hits[:max_results]


def _rank(files, want: str) -> list:
    out = []
    for p, size in files:
        try:
            name = p.name.lower()
        except Exception:
            continue
        sc = _score(name, want)
        if sc <= 0:
            continue
        if _HAS_EXT.search(want) and name.endswith(want):
            sc += 20
        out.append((sc, p))
    return out


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} TB".format(n)


def is_owner(brain, uid: str) -> bool:
    """是不是主人。发文件是危险能力，只认 owner_wxid。"""
    try:
        owner = str((brain.cfg.get("relay") or {}).get("owner_wxid") or "").strip()
        return bool(owner) and str(uid) == owner
    except Exception:
        return False


def handle(brain, uid: str, text: str) -> dict:
    """处理一次「发文件」请求。

    返回 {"matched": bool, "ok": bool, "reply": str}
    """
    desc = parse_request(text)
    if not desc:
        return {"matched": False, "ok": False, "reply": ""}

    if not is_owner(brain, uid):
        return {"matched": True, "ok": False, "reply": "这我发不了"}

    hits = find_file(desc)
    # 60 分以上才算靠谱（50 分是「用户说多了」，70+ 才是真像）
    hits = [h for h in hits if h[0] >= 60]
    if not hits:
        return {"matched": True, "ok": False,
                "reply": "没找到「{}」".format(desc)}

    best_score, best = hits[0]
    # 同档候选（比如 金币修改器.exe 和 金币修改器.zip 都是 100 分）就问他。
    # **不能因为「分数高」就直接发** —— 100 分的两个文件一样高，
    # 蒙一个发过去很可能发错（.exe 139MB vs .zip 61MB，差得离谱）。
    same = [h for h in hits if abs(h[0] - best_score) < 5]
    if len(same) > 1:
        names = "\n".join("· {}（{}）".format(
            h[1].name, human_size(h[1].stat().st_size if h[1].exists() else 0))
            for h in same[:5])
        return {"matched": True, "ok": False,
                "reply": "找到好几个，你要哪个？\n" + names}

    try:
        size = best.stat().st_size
    except Exception:
        size = 0
    if size > MAX_SIZE:
        return {"matched": True, "ok": False,
                "reply": "「{}」有 {}，太大了发不了".format(best.name, human_size(size))}

    # ── 真正发送 ──
    try:
        gui = brain.gui()
    except Exception as e:
        return {"matched": True, "ok": False,
                "reply": "发不了：{}".format(type(e).__name__)}

    ok, detail = False, ""
    try:
        r = gui.send_file(str(best), who=brain.nick(uid), verify=False)
        # **WxResponse 是 dict 子类，key 是 status/message/data。**
        # 踩过：以为是 {"success": ...}，结果 r.get("success") 永远是 None，
        # 文件明明发出去了却报"发失败"。
        # 正确判断：bool(r)（__bool__ 返回 is_success）或 r["status"]=="成功"。
        # 优先看 status 字段（不依赖 WxResponse.__bool__，
        # 这样即使拿到的是普通 dict 也能正确判断）
        if isinstance(r, dict) and "status" in r:
            ok = (r.get("status") == "成功")
        else:
            ok = bool(r) if r is not None else False
        detail = str(r.get("message") or "") if isinstance(r, dict) else ""
    except Exception as e:
        detail = "{}: {}".format(type(e).__name__, str(e)[:80])

    if ok:
        return {"matched": True, "ok": True,
                "reply": "发了「{}」（{}）".format(best.name, human_size(size))}
    return {"matched": True, "ok": False,
            "reply": "发失败了：{}".format(detail[:60] or "未知原因")}
