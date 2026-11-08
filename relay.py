# -*- coding: utf-8 -*-
"""转述模块：把一段话发到桌面上的另一个应用。

用户需求（2026-10-19）：
  「帮我把这段话给 deepseek harness（示例示例）然后发给桌面上的 deepseek harness」
  → 找到那个窗口，把内容打进输入框、发送，然后回一句"发好了"。
  **只有主人（指定的 wxid）发才有用。**

设计要点：
  · 目标窗口靠**标题模糊匹配**（配置里给关键字），不写死 hwnd ——
    窗口重开会换句柄。
  · 输入框靠 **UIA 找 Edit 控件**，不猜坐标 ——
    之前朋友圈评论点错位置就是猜坐标猜的（用户实测指出）。
    实测这个应用：Edit (710,929,1546,974) 名字里带"发消息"。
  · 发送用 **剪贴板粘贴 + 回车**，不用逐字打 ——
    中文逐字键入在 Chromium 里容易丢字。
  · 全程**先把目标窗口置前**，发完把焦点还给原来的窗口。
"""
from __future__ import annotations

import re
import time

# ──────────────────────────────────────────────
# 命令解析
# ──────────────────────────────────────────────

# 用户可能这么说：
#   帮我把这段话给 deepseek harness（示例示例）然后发给桌面上的 deepseek harness
#   帮我把这段话发到 harness：示例示例
#   把「示例示例」发给 harness
#   @deepsleep 发给 harness 示例示例
_QUOTED = re.compile(r"[（(【\[](.*?)[）)】\]]|「(.*?)」|“(.*?)”|\"(.*?)\"", re.S)

# 「发给 X」里的 X：允许带空格（"deep seek harness"），
# 遇到中文标点/冒号/换行就停。
_TARGET_PAT = re.compile(
    r"(?:发给|发到|发去|发往|转给|转交给)\s*"
    r"(?:桌面上的?|电脑上的?)?\s*"
    # 名字可以由「空格分隔的若干词」组成：deep seek harness
    r"((?:[A-Za-z0-9_\u4e00-\u9fa5][A-Za-z0-9_\-\u4e00-\u9fa5]*"
    r"(?: [A-Za-z0-9_\u4e00-\u9fa5][A-Za-z0-9_\-\u4e00-\u9fa5]*){0,6})?)"
    r"(?=[\u3000-\u303f\uff00-\uffef\s]|$)",
)


def _clean_target(s: str) -> str:
    s = (s or "").strip()
    # **先把会话参数从目标名里剥掉。**
    # 目标正则允许"空格分隔的若干词"（为了认出 deep seek harness），
    # 于是"harness 新建会话"会被整串当成目标名 —— 实测踩过：
    # 报"找不到名字里带「harness 新建会话」的窗口"。
    s = re.sub(r"\s*(?:新建|新开|开个新)?\s*新?(?:建)?会话\s*$", "", s).strip()
    s = re.sub(r"\s*(?:会话|对话)\s*[：:].*$", "", s).strip()
    s = re.sub(r"[的了吧呢啊哦呀~～]+$", "", s).strip()
    # 去掉"然后发给..."这类尾巴
    s = re.split(r"然后|再把|接着", s)[0].strip()
    return s


def parse_command(text: str) -> dict:
    """解析"把这段话发给 X"这类指令。

    认得这几种说法：
      帮我把这段话给 harness（示例示例）然后发给桌面上的 harness
      把「你好世界」发给 harness
      发给 harness：示例示例
      把下面这段发到 harness 示例

    返回 {"ok": bool, "target": str, "content": str, "why": str}
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "why": "空消息"}

    # ① 目标：先找，定出边界好切内容
    target = ""
    tstart = tend = -1
    tm = _TARGET_PAT.search(t)
    if tm:
        target = _clean_target(tm.group(1))
        tstart, tend = tm.start(), tm.end()
    if not target:
        return {"ok": False, "why": "没看出要发给谁"}

    # ② 内容：优先括号/引号
    content = ""
    for m in _QUOTED.finditer(t):
        g = next((x for x in m.groups() if x is not None), "")
        if g and g.strip():
            content = g.strip()
            break

    # ③ 没括号 -> 取"发给"之后剩下的（冒号/顿号后面的）
    if not content:
        rest = t[tend:].strip()
        rest = re.sub(r"^[：:，,、\s]+", "", rest).strip()
        if rest:
            content = rest

    # ④ 还是没有 -> 取"这段话/这句话"和"发给"之间的
    if not content and tstart > 0:
        mid = t[:tstart]
        m2 = re.search(r"(?:这段话|这句话|那段话|下面|以下)[：:]?\s*(.+)$", mid, re.S)
        if m2:
            content = m2.group(1).strip(" （）()「」\"'：:")

    if not content:
        return {"ok": False, "why": "没找到要发的内容", "target": target}

    # 内容里如果还残留"然后发给..."就截掉
    content = re.split(r"然后(?:再)?(?:发给|发到)", content)[0].strip()
    if not content:
        return {"ok": False, "why": "没找到要发的内容", "target": target}
    return {"ok": True, "target": target, "content": content}




# 「新建会话 / 新会话 / 新开一个会话」
_NEW_SESSION = re.compile(
    r"(?:新建|新开|开个新|新起|新弄)(?:一个)?(?:的)?会话"
    r"|(?:^|[\s，,、])(?:新会话|新对话)(?:$|[\s，,。！!])")
# 「发给 harness 会话：xxx」/「发给 harness 的 xxx 会话」/「发给 harness 的会话 xxx」
_SESSION_PAT = re.compile(
    r"(?:会话|对话|聊天|线程)\s*[：:]\s*([^，,。！!；;\n]{1,40})"
    r"|(?:的|那个|名(?:为|叫))\s*([^，,。！!；;\n]{1,40}?)\s*(?:会话|对话|聊天|线程)"
)


def parse_session(text: str) -> dict:
    """从指令里解析"要不要新建会话 / 发到哪个会话"。

    返回 {"new": bool, "name": str}
    认得：
      把「你好」发给 harness 新建会话
      把「你好」发给 harness 新会话
      把「你好」发给 harness 会话：简单的加法问题
      把「你好」发给 harness 的 简单的加法问题 会话
    """
    s = text or ""
    out = {"new": False, "name": ""}
    if _NEW_SESSION.search(s):
        out["new"] = True
        return out
    m = _SESSION_PAT.search(s)
    if m:
        name = next((x for x in m.groups() if x), "") or ""
        name = name.strip(" 　（）()「」\"'：:")
        # 把尾巴上的"新建会话"之类去掉
        name = re.sub(r"(?:新建|新开)(?:一个)?会话$", "", name).strip()
        if name:
            out["name"] = name
    return out

# ──────────────────────────────────────────────
# 窗口定位与投递
# ──────────────────────────────────────────────

def find_target_window(keyword: str):
    """按标题关键字找可见窗口，返回 (hwnd, title) 或 (None, "")。

    匹配规则：关键字里的每个词都在标题里出现（不分大小写）。
    """
    try:
        import win32gui
    except Exception:
        return None, ""
    words = [w for w in re.split(r"[\s_\-]+", (keyword or "").strip()) if w]
    if not words:
        return None, ""
    hits = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return
            title = win32gui.GetWindowText(h)
            if not title.strip():
                return
            r = win32gui.GetWindowRect(h)
            if r[2] - r[0] < 300 or r[3] - r[1] < 200:
                return
            low = title.lower()
            if all(w.lower() in low for w in words):
                hits.append((h, title))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None, ""
    if not hits:
        return None, ""
    return hits[0]


def find_input_box(hwnd):
    """在目标窗口里找文本输入框，返回 (rect, uia 控件) 或 (None, None)。

    优先找 Name 里带"发消息 / Message / 输入"的 Edit；
    找不到就取面积最大的 Edit。
    """
    try:
        import uiautomation as auto
    except Exception:
        return None, None
    try:
        win = auto.ControlFromHandle(hwnd)
        if win is None:
            return None, None
    except Exception:
        return None, None

    cands = []

    def walk(c, d=0):
        if d > 22 or len(cands) > 60:
            return
        try:
            for ch in c.GetChildren():
                try:
                    ct = ch.ControlTypeName or ""
                    if ct in ("EditControl", "DocumentControl"):
                        r = ch.BoundingRectangle
                        w = r.right - r.left
                        h = r.bottom - r.top
                        if w > 120 and h > 20:
                            cands.append((ch, r, (ch.Name or "")))
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not cands:
        return None, None

    # 优先名字里带关键词的
    for ctrl, r, nm in cands:
        if any(k in nm for k in ("发消息", "输入", "Message", "message", "Type")):
            return (r.left, r.top, r.right, r.bottom), ctrl
    # 否则取面积最大的
    best = max(cands, key=lambda x: (x[1].right - x[1].left) * (x[1].bottom - x[1].top))
    return (best[1].left, best[1].top, best[1].right, best[1].bottom), best[0]


def click_new_session(hwnd, settle: float = 1.2) -> tuple:
    """点侧边栏的「新建会话」。返回 (ok, why)。"""
    try:
        import uiautomation as auto
    except Exception:
        return False, "缺少 uiautomation"
    try:
        win = auto.ControlFromHandle(hwnd)
    except Exception as e:
        return False, "拿不到窗口：{}".format(type(e).__name__)

    # 找名字正好是"新建会话"、且尺寸像按钮的控件（会命中多个，取可见且最大的）
    cands = []

    def walk(c, d=0):
        if d > 22:
            return
        try:
            for ch in c.GetChildren():
                try:
                    nm = (ch.Name or "").strip()
                    if nm in ("新建会话", "新会话", "新对话"):
                        r = ch.BoundingRectangle
                        w = r.right - r.left
                        h = r.bottom - r.top
                        if w > 80 and 20 < h < 120:
                            cands.append((w * h, r, ch))
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not cands:
        return False, "找不到「新建会话」按钮"
    cands.sort(key=lambda x: -x[0])
    _, r, ctrl = cands[0]
    try:
        ctrl.Click()
    except Exception:
        try:
            import pyautogui
            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        except Exception as e:
            return False, "点击失败：{}".format(type(e).__name__)
    import time as _t
    _t.sleep(settle)
    return True, "已新建会话"


def click_session(hwnd, name: str, settle: float = 1.2) -> tuple:
    """在侧边栏找名字含 name 的会话并点开。返回 (ok, why, 实际标题)。"""
    try:
        import uiautomation as auto
    except Exception:
        return False, "缺少 uiautomation", ""
    name = (name or "").strip()
    if not name:
        return False, "没给会话名", ""
    try:
        win = auto.ControlFromHandle(hwnd)
    except Exception as e:
        return False, "拿不到窗口：{}".format(type(e).__name__), ""

    hits = []

    def walk(c, d=0):
        if d > 22:
            return
        try:
            for ch in c.GetChildren():
                try:
                    ct = ch.ControlTypeName or ""
                    if ct in ("TreeItemControl", "ListItemControl"):
                        nm = (ch.Name or "").strip()
                        # 名字里带"刚刚/1天/2天"这类时间尾巴，用包含匹配
                        if nm and name in nm:
                            r = ch.BoundingRectangle
                            if r.right - r.left > 80 and r.bottom - r.top > 20:
                                hits.append((nm, r, ch))
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not hits:
        return False, "侧边栏里没有叫「{}」的会话".format(name), ""
    # 取最短的那个名字（最贴近），再取最靠上的
    hits.sort(key=lambda x: (len(x[0]), x[1].top))
    nm, r, ctrl = hits[0]
    try:
        ctrl.Click()
    except Exception:
        try:
            import pyautogui
            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        except Exception as e:
            return False, "点击失败：{}".format(type(e).__name__), nm
    import time as _t
    _t.sleep(settle)
    return True, "已切到会话", nm


def _set_clipboard(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def send_to_app(keyword: str, text: str, enter: bool = True,
                settle: float = 0.6, new_session: bool = False,
                session: str = "") -> dict:
    """把 text 发到标题含 keyword 的那个应用。

    new_session=True  先点「新建会话」再发
    session="名字"    先切到那个会话再发

    返回 {"ok": bool, "why": str, "title": str, "input": rect|None, "session": str}
    """
    if not (text or "").strip():
        return {"ok": False, "why": "内容为空", "title": "", "input": None}

    hwnd, title = find_target_window(keyword)
    if not hwnd:
        return {"ok": False, "why": f"找不到名字里带「{keyword}」的窗口",
                "title": "", "input": None}

    # ---- 先把会话切对，再找输入框 ----
    # （会话一切界面就变了，输入框位置也可能变）
    chosen = ""
    if new_session:
        ok_ns, why_ns = click_new_session(hwnd)
        if not ok_ns:
            return {"ok": False, "why": why_ns, "title": title,
                    "input": None, "session": ""}
        chosen = "(新建)"
    elif session:
        ok_ss, why_ss, got = click_session(hwnd, session)
        if not ok_ss:
            return {"ok": False, "why": why_ss, "title": title,
                    "input": None, "session": ""}
        chosen = got
        time.sleep(0.4)

    input_rect, input_ctrl = find_input_box(hwnd)
    if not input_rect:
        return {"ok": False, "why": "找到了窗口但没找到输入框",
                "title": title, "input": None, "session": chosen}

    try:
        import win32con
        import win32gui

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.6)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(settle)
    except Exception as e:
        return {"ok": False, "why": f"窗口置前失败：{type(e).__name__}",
                "title": title, "input": input_rect}

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        l, tp, r, b = input_rect
        pyautogui.click((l + r) // 2, (tp + b) // 2)
        time.sleep(0.35)
    except Exception as e:
        return {"ok": False, "why": f"点输入框失败：{type(e).__name__}",
                "title": title, "input": input_rect}

    if not _set_clipboard(text):
        return {"ok": False, "why": "剪贴板写入失败",
                "title": title, "input": input_rect}

    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.7)
        if enter:
            pyautogui.press("enter")
            time.sleep(0.5)
    except Exception as e:
        return {"ok": False, "why": f"粘贴/回车失败：{type(e).__name__}",
                "title": title, "input": input_rect}

    return {"ok": True, "why": "已发出", "title": title,
            "input": input_rect, "session": chosen}
