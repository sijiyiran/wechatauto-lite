# -*- coding: utf-8 -*-
"""自动同意好友申请。

**这条链路全部靠 UIA，数据库里读不到**（实测：stranger 表 0 行、oplog 0 行、
contact 里也没有待验证的行）。申请只存在于「通讯录 → 新的朋友」这个界面里。

实测出来的完整流程（2026-10-18，微信 4.x）：

  1. 点导航栏第 2 个 tab「通讯录」
  2. 列表里的条目 cls = mmui::ContactsCellNewFriendView，
     **name 里带着状态词**：
        '💤我是群聊"反贪局专案组"的💤等待验证'     <- 待处理
        '四季亦然我是群聊“反贪局专案组”的四季亦然已添加'  <- 已通过
        '澜我是群聊"反贪局专案组"的澜已过期'          <- 已过期
     所以「待处理」= name 里含「等待验证」。
  3. 点那一条 -> 右边出现详情（头像 / 回复 / **前往验证** 按钮）
  4. 点「前往验证」（mmui::XOutlineButton）-> 弹出独立的「通过朋友验证」窗口
     里面有：备注、标签、朋友权限、**确定**、取消
  5. 点「确定」-> 通过

第 4 步那个窗口是独立的顶层窗口（title = 通过朋友验证），
不是主窗的子控件，所以要在桌面所有窗口里按标题找它。
"""
from __future__ import annotations

import pathlib
import time

CELL_CLS = "mmui::ContactsCellNewFriendView"
GROUP_CLS = "mmui::ContactsCellGroupView"
TAB_BAR_CLS = "mmui::MainTabBar"
TAB_ITEM_CLS = "mmui::XTabBarItem"
CONFIRM_TITLE = "通过朋友验证"
PENDING_WORD = "等待验证"
DONE_WORDS = ("已添加", "已过期")

# 默认拒掉的垃圾申请关键词。命中就跳过（不点同意，也不点拒绝 —— 保持原样）。
SPAM_WORDS = (
    "推广", "刷单", "兼职", "日结", "贷款", "博彩", "彩票", "投资", "理财",
    "加微信", "加我", "私聊", "客服", "返利", "优惠券", "代购", "引流",
    "招聘", "招人", "赚钱", "项目", "课程", "培训", "股票", "币圈",
)


def _find_by(root, pred, max_depth=20):
    """深度优先找一个满足 pred 的控件。"""
    if root is None:
        return None
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for c in children:
        try:
            if pred(c):
                return c
        except Exception:
            pass
        if max_depth > 0:
            got = _find_by(c, pred, max_depth - 1)
            if got is not None:
                return got
    return None


def _find_all(root, pred, out=None, max_depth=20):
    out = [] if out is None else out
    if root is None or max_depth < 0:
        return out
    try:
        children = root.GetChildren()
    except Exception:
        return out
    for c in children:
        try:
            if pred(c):
                out.append(c)
        except Exception:
            pass
        _find_all(c, pred, out, max_depth - 1)
    return out


def goto_contacts(gui) -> bool:
    """切到「通讯录」页。"""
    win = getattr(gui._get_uia(), "_win", None)
    bar = _find_by(win, lambda c: (c.ClassName or "") == TAB_BAR_CLS)
    if bar is None:
        return False
    tabs = _find_all(bar, lambda c: (c.ClassName or "") == TAB_ITEM_CLS, [], 4)
    if len(tabs) < 2:
        return False
    try:
        tabs[1].Click()
    except Exception:
        return False
    time.sleep(1.5)
    return True


# 触发词分强弱，因为"通过一下""验证一下"这种说法太宽 ——
# 实测"帮我通过一下门禁"也会命中，那样群里随口一句她就去翻通讯录，很莫名其妙。
STRONG_WORDS = (
    "加好友", "加个好友", "加你好友", "好友申请", "好友验证", "朋友验证",
    "加个微信", "通过好友", "同意好友",
    "检查好友", "看看好友", "看看申请", "处理申请", "好友列表",
)
# 弱词：必须**同时**出现好友语境才算
WEAK_WORDS = ("通过一下", "通过下", "同意一下", "验证一下", "验证下", "通过一下")
FRIEND_CTX = ("好友", "申请", "加你", "加我", "加个", "验证", "二维码")

# 否定/无关语境：出现这些就不当成"让她处理申请"
TRIGGER_NEG = ("验证码", "密码", "通过考试", "通过面试", "测试通过", "审核通过")


def should_check(text: str) -> bool:
    """这句话是不是在让她去处理好友申请。

    分强弱两级：
      · 强词（加好友/好友申请/…）单独出现就算
      · 弱词（通过一下/同意一下/…）**必须**同时有好友语境
        —— 否则"帮我通过一下门禁"也会误触发
    另外单独认一句手动指令（"检查好友申请"）。
    """
    t = (text or "").strip()
    if not t or len(t) > 200:
        return False
    if any(n in t for n in TRIGGER_NEG):
        return False
    if any(w in t for w in STRONG_WORDS):
        return True
    if any(w in t for w in WEAK_WORDS) and any(c in t for c in FRIEND_CTX):
        return True
    return False


FLAG_NAME = "_check-friends.flag"


def request_check(reason: str = "") -> None:
    """放一个标记文件，让守护进程下一轮去查好友申请。"""
    try:
        (pathlib.Path(__file__).resolve().parent / FLAG_NAME).write_text(
            reason or "手动触发", encoding="utf-8")
    except Exception:
        pass


def take_flag() -> str:
    """取走标记（取到就删）。返回里面的原因，没有标记返回空串。"""
    p = pathlib.Path(__file__).resolve().parent / FLAG_NAME
    try:
        if not p.exists():
            return ""
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
        p.unlink(missing_ok=True)
        return txt or "手动触发"
    except Exception:
        return ""


def _tab_items(gui):
    """拿导航栏的 4 个 tab 控件。"""
    win = getattr(gui._get_uia(), "_win", None)
    bar = _find_by(win, lambda c: (c.ClassName or "") == TAB_BAR_CLS)
    if bar is None:
        return []
    return _find_all(bar, lambda c: (c.ClassName or "") == TAB_ITEM_CLS, [], 4)


def _badge_rect(tab):
    """找某个 tab 上那个小红点控件的屏幕矩形。"""
    def w2(c, d=0):
        if d > 4:
            return None
        try:
            kids = c.GetChildren()
        except Exception:
            return None
        for ch in kids:
            try:
                if (ch.ClassName or "") == "mmui::XBadge":
                    return ch.BoundingRectangle
            except Exception:
                pass
            r = w2(ch, d + 1)
            if r:
                return r
        return None

    return w2(tab)


def has_badge(gui, threshold: int = 6) -> bool:
    """「通讯录」tab 上有没有小红点 —— 有就说明有新的好友申请。

    **这是让"只在有申请时才看"成立的关键。**
    数据库里读不到申请（实测 stranger / oplog / contact 全是空），所以只能看界面；
    但每次都切到通讯录页会跟用户抢微信、还把界面留在通讯录上，很烦。

    折中办法：**只截 tab 上那 21×21 的小红点区域，数红色像素**。
    不切页、不动焦点、约 50 毫秒。没有红点就直接返回，连通讯录页都不进。

    2026-10-18 实测基线：没有申请时 4 个 tab 的红点像素都是 0。
    """
    try:
        tabs = _tab_items(gui)
    except Exception:
        return False
    target = None
    for t in tabs:
        try:
            if (t.Name or "").strip() == "通讯录":
                target = t
                break
        except Exception:
            continue
    if target is None and len(tabs) >= 2:
        target = tabs[1]
    if target is None:
        return False
    rect = _badge_rect(target)
    if rect is None:
        return False
    try:
        from PIL import ImageGrab
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0 or w > 80 or h > 80:
            return False
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                             all_screens=True).convert("RGB")
        n = 0
        for r, g, b in img.getdata():
            # 微信红点大约 (250,60,60)，阈值放宽一点，别被抗锯齿漏掉
            if r > 170 and g < 120 and b < 120:
                n += 1
                if n >= threshold:
                    return True
        return False
    except Exception:
        return False


def goto_chat_tab(gui) -> bool:
    """切回「微信」页。

    看完申请**一定要切回来** —— 否则界面会一直停在通讯录，
    用户看到的就是"她过一会就来翻我通讯录"。
    """
    try:
        tabs = _tab_items(gui)
    except Exception:
        return False
    if not tabs:
        return False
    for t in tabs:
        try:
            if (t.Name or "").strip() == "微信":
                t.Click()
                time.sleep(0.8)
                return True
        except Exception:
            continue
    try:
        tabs[0].Click()
        time.sleep(0.8)
        return True
    except Exception:
        return False


# 「通讯录」页里那个进入申请列表的入口。实测（2026-10-19）：
#   '新的朋友'  mmui::ContactsCellGroupView  (117, 208, 417, 253)
# **必须点它才能看到申请** —— 之前 goto_contacts 只切到通讯录页就去找
# ContactsCellNewFriendView，结果什么都找不到，于是"有待验证的她说没有"。
NEW_FRIEND_ENTRY_CLS = "mmui::ContactsCellGroupView"
NEW_FRIEND_NAMES = ("新的朋友", "新朋友", "New Friends", "Friend Requests")


def goto_new_friends(gui, settle: float = 1.5) -> bool:
    """切到「通讯录 → 新的朋友」页面。返回是否成功。

    这是好友申请检查的关键一步 —— 申请不在通讯录主页，在「新的朋友」里面。
    """
    if not goto_contacts(gui):
        return False
    win = getattr(gui._get_uia(), "_win", None)
    if win is None:
        return False

    hits = []

    def walk(c, d=0):
        if d > 22:
            return
        try:
            for ch in c.GetChildren():
                try:
                    cls = ch.ClassName or ""
                    nm = (ch.Name or "").strip()
                    if cls == NEW_FRIEND_ENTRY_CLS and nm in NEW_FRIEND_NAMES:
                        r = ch.BoundingRectangle
                        if r.right - r.left > 100:
                            hits.append((r.top, ch))
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    if not hits:
        return False
    hits.sort(key=lambda x: x[0])
    ctrl = hits[0][1]
    try:
        ctrl.Click()
    except Exception:
        try:
            r = ctrl.BoundingRectangle
            import pyautogui
            pyautogui.FAILSAFE = False
            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        except Exception:
            return False
    time.sleep(settle)
    return True

def list_pending(gui) -> list[dict]:
    """列出所有好友申请条目。返回 [{name, status, ctrl}]。"""
    win = getattr(gui._get_uia(), "_win", None)
    cells = _find_all(win, lambda c: (c.ClassName or "") == CELL_CLS, [], 20)
    out = []
    for c in cells:
        try:
            nm = (c.Name or "").strip()
        except Exception:
            continue
        if not nm:
            continue
        status = "pending" if PENDING_WORD in nm else (
            "done" if any(w in nm for w in DONE_WORDS) else "unknown")
        # 名字里混着昵称+招呼语+状态词，这里只做展示，不强行拆
        out.append({"name": nm, "status": status, "ctrl": c})
    return out


def is_spam(text: str, extra: tuple = ()) -> bool:
    t = (text or "").lower()
    return any(w.lower() in t for w in tuple(SPAM_WORDS) + tuple(extra))


def _click_btn_in_window(hwnd, want_name: str) -> bool:
    """在指定窗口里点一个名字叫 want_name 的按钮。

    **不能只靠 UIA 的 Invoke()**：实测微信这个「通过朋友验证」窗口是 Qt 画的，
    Invoke() 和 Click() 都返回成功但**界面上什么都没发生**（点「确定」没反应、
    点「取消」也关不掉），于是流程卡在"确认窗里点不到确定"。
    改成：用 UIA 只**读位置**，再用 SendInput 按屏幕坐标点一下 —— 和用户
    自己用鼠标点是同一条路径，实测有效。
    """
    import comtypes.client
    from comtypes.gen import UIAutomationClient as UIA

    try:
        uia = comtypes.client.CreateObject(UIA.CUIAutomation)
        root = uia.ElementFromHandle(hwnd)
    except Exception:
        return False

    target = None
    try:
        coll = root.FindAll(
            UIA.TreeScope_Descendants,
            uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                       UIA.UIA_ButtonControlTypeId))
        for i in range(coll.Length):
            el = coll.GetElement(i)
            try:
                if (el.CurrentName or "").strip() == want_name:
                    target = el
                    break
            except Exception:
                continue
    except Exception:
        return False
    if target is None:
        return False

    # 先试 Invoke（有的版本认），再试坐标
    try:
        target.Invoke()
        time.sleep(0.6)
        return True
    except Exception:
        pass

    try:
        r = target.CurrentBoundingRectangle
        x = int((r.left + r.right) / 2)
        y = int((r.top + r.bottom) / 2)
    except Exception:
        return False
    try:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        from wechatauto.guia import WinInput
        WinInput().send_input_click(x, y)
        time.sleep(0.8)
        return True
    except Exception:
        return False


def find_confirm_window():
    """找「通过朋友验证」那个独立窗口。返回 hwnd 或 0。"""
    import win32gui

    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == CONFIRM_TITLE:
            hits.append(h)

    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else 0


def cancel_confirm() -> bool:
    """关掉可能残留的验证窗口（点取消）。"""
    hwnd = find_confirm_window()
    if not hwnd:
        return False
    return _click_btn_in_window(hwnd, "取消")


def find_button_anywhere(want_names, timeout: float = 8.0):
    """在**所有可见窗口**里找那个按钮，返回 (btn, 说明)。

    为什么要这样：accept_one 原来只 _find_by(win, ...)，其中 win 是
    gui._get_uia()._win（主窗口）。但好友申请详情页有时候是**另一个顶层窗口**
    （或主窗口的对象在点开后被 Qt 重建了），于是明明界面上有「前往验证」，
    代码却找不到 —— 2026-10-19 实测：
        [好友申请] 发现 1 条待验证
        失败 详情页里没有「前往验证」/「接受」按钮
    用户现场看到按钮就在那儿。

    现在：主窗口找不到就枚举**所有顶层窗口**挨个找，还找不到再试一次主窗口。
    """
    import time as _t

    def probe(root):
        for nm in want_names:
            got = _find_by(root, lambda c, n=nm: (c.Name or "").strip() == n)
            if got is not None:
                return got
        return None

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        # ① 主窗口
        try:
            from wechatauto import WeChatGUI
            w = getattr(WeChatGUI()._get_uia(), "_win", None)
            got = probe(w)
            if got is not None:
                return got, "主窗口"
        except Exception:
            pass

        # ② 所有可见顶层窗口
        try:
            import win32gui
            import uiautomation as auto
            hwnds = []

            def cb(h, _):
                try:
                    if win32gui.IsWindowVisible(h):
                        ttl = win32gui.GetWindowText(h)
                        if ttl.strip():
                            hwnds.append((h, ttl))
                except Exception:
                    pass

            win32gui.EnumWindows(cb, None)
            for h, ttl in hwnds:
                try:
                    root = auto.ControlFromHandle(h)
                    if root is None:
                        continue
                    got = probe(root)
                    if got is not None:
                        return got, "窗口「{}」".format(ttl[:24])
                except Exception:
                    continue
        except Exception:
            pass
        _t.sleep(0.6)
    return None, "没找到"

def accept_one(gui, entry: dict, wait_confirm: float = 3.0) -> tuple[bool, str]:
    """通过一条好友申请。entry 来自 list_pending()。"""
    nm = entry.get("name") or ""
    # 先清掉可能残留的验证窗，否则点不动
    if find_confirm_window():
        cancel_confirm()
        time.sleep(0.8)

    try:
        entry["ctrl"].Click()
    except Exception as e:
        return False, f"点不开申请条目（{type(e).__name__}）"
    time.sleep(2.0)

    win = getattr(gui._get_uia(), "_win", None)
    # 详情页是**异步渲染**的，点开条目后按钮可能还没出来。
    # 之前只固定等 2 秒找一次，偶发报「详情页里没有「前往验证」」——
    # 实测踩过一次（20:59）：日志说没按钮，我手动点开明明有。
    # 改成轮询等最多 6 秒，每次重新取一次窗口句柄。
    # **在所有窗口里找按钮** —— 详情页有时是独立窗口，只查主窗口会漏
    btn, _where = find_button_anywhere(
        ("前往验证", "接受", "通过"), timeout=8.0)
    if btn is None:
        # 兜底：再单独查一次主窗口（有些版本要等一会儿才渲染）
        win = getattr(gui._get_uia(), "_win", None)
        btn = _find_by(win, lambda c: (c.Name or "").strip() in
                       ("前往验证", "接受", "通过"))
        _where = "主窗口兜底"  # noqa: F841
    if btn is None:
        return False, "详情页里没有「前往验证」/「接受」按钮"

    try:
        btn.Click()
    except Exception as e:
        return False, f"点不动「前往验证」（{type(e).__name__}）"

    # 等确认窗出现
    hwnd = 0
    deadline = time.time() + wait_confirm
    while time.time() < deadline:
        hwnd = find_confirm_window()
        if hwnd:
            break
        time.sleep(0.3)
    if not hwnd:
        return False, "没等到「通过朋友验证」窗口"

    time.sleep(0.8)
    if not _click_btn_in_window(hwnd, "确定"):
        return False, "确认窗里点不到「确定」"
    time.sleep(1.5)

    # 收尾：万一窗口还开着
    if find_confirm_window():
        cancel_confirm()
    return True, f"已通过：{nm[:24]}"


