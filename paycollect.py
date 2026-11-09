"""红包 / 转账的领取（UIA + OCR）。

wechatauto 没有现成接口，只能摸界面：
  1. open_chat 打开会话
  2. OCR 扫右侧消息区，找到「微信转账」/「红包」卡片
  3. wx_click 点开（坐标 = origin_x/ y + OCR 相对坐标）
  4. 弹窗里 OCR 找「确认收款」/「开」，点它

**安全第一。** 这个模块只负责"点"，判断能不能收在 paycard.py 里
（转账必须 receiver_username == 我的 wxid），调用方还要再过一遍配置白名单。
"""
import time

# 全角 → 半角（OCR 经常把 . : ￥ 识别成全角）
_FULL2HALF = str.maketrans(
    "０１２３４５６７８９．：，％（）－＿　",
    "0123456789.:,%()-_ ",
)


def _norm(text: str) -> str:
    """归一化 OCR 文本：全角转半角、去掉所有空白。"""
    return (text or "").translate(_FULL2HALF).replace(" ", "").replace("\u3000", "")


def _digits(text: str) -> str:
    """只留数字和小数点（用于金额比对）。"""
    out = []
    for ch in _norm(text):
        if ch.isdigit() or ch == ".":
            out.append(ch)
    return "".join(out)


def _amount_key(text: str) -> str:
    """把「￥39.93」「¥39．93」「39.93元」统一成 "39.93"。"""
    d = _digits(text)
    if "." in d:
        # 去掉末尾多余的点
        d = d.rstrip(".")
    return d


# 弹窗里可能的按钮文案
TRANSFER_BUTTONS = ("确认收款", "确认收钱", "收 款", "收款")
REDPACKET_BUTTONS = ("开", "领取", "拆开")

# 卡片在聊天区里的识别关键词
TRANSFER_KEYWORDS = ("微信转账", "收到转账", "已收款", "转账给")
REDPACKET_KEYWORDS = ("红包", "恭喜发财", "领取红包")


def _ocr_region(gui):
    """聊天消息区的相对区域。

    注意 gui.ocr 的参数是 (x, y, w, h) **不是** (left, top, right, bottom) ——
    踩过（2026-11-05）：传成 right/bottom 时窗口外部分被裁掉，只扫到几条。
    """
    left = int(getattr(gui, "right_pane_left", 0) or 0)
    top = 100
    rw = int(getattr(gui, "render_w", 0) or 0)
    rh = int(getattr(gui, "render_h", 0) or 0)
    return (left, top, max(1, rw - left), max(1, rh - top))


def _find(gui, region, keywords, want_amount=""):
    """在区域里 OCR 找卡片，返回 (屏幕坐标, 命中的原文)。

    优先按**金额**定位（比关键词可靠得多 —— 「微信转账」四个字 OCR 经常认不出）。
    """
    try:
        items = gui.ocr(region)
    except Exception:
        return None, ""
    if not items:
        return None, ""

    want_amt = _amount_key(want_amount)

    # 1) 先按金额找（最可靠）
    if want_amt:
        for text, x, y, w, h in items:
            if _amount_key(text) == want_amt:
                return (gui.origin_x + x + w // 2,
                        gui.origin_y + y + h // 2), text

    # 2) 再按关键词找（归一化后比较）
    for text, x, y, w, h in items:
        nt = _norm(text)
        for kw in keywords:
            if _norm(kw) in nt:
                return (gui.origin_x + x + w // 2,
                        gui.origin_y + y + h // 2), text
    return None, ""




# ══════════════════════ UIA 直点（微信 4.0 实测最稳）══════════════════════
def _uia():
    try:
        import uiautomation as auto
        return auto
    except Exception:
        return None


def _find_pay_window(timeout: float = 3.0):
    """等并返回转账/红包详情弹窗控件（UIA ClassName = mmui::PayDetailWindow）。

    **踩过（2026-11-09）**：一开始用 `win32gui.GetClassName()` 判断
    `"PayDetailWindow"`，永远找不到 —— 因为那是 **UIA 的 ClassName**，
    窗口类名实际是 `Qt51514QWindowIcon`。
    正确做法：枚举微信进程的每个顶层窗口，用 UIA ControlFromHandle 拿 ClassName。
    """
    auto = _uia()
    if auto is None:
        return None
    import time as _t
    deadline = _t.time() + max(0.5, timeout)
    while _t.time() < deadline:
        try:
            import win32gui
            import win32process

            # 先确认微信主窗口 pid
            wx_pids = set()
            try:
                from wechatauto import WeChatGUI
                wmain = getattr(WeChatGUI()._get_uia(), "_win", None)
                hwnd_main = getattr(getattr(wmain, "NativeWindowHandle", None),
                                    "__int__", lambda: None)
                for proc in ("Weixin.exe", "WeChat.exe"):
                    pass
            except Exception:
                pass

            cands = []

            def cb(h, _):
                try:
                    if not win32gui.IsWindowVisible(h):
                        return
                    r = win32gui.GetWindowRect(h)
                    w, hh = r[2] - r[0], r[3] - r[1]
                    # 弹窗尺寸通常 200~800
                    if 120 < w < 900 and 120 < hh < 900:
                        cands.append(h)
                except Exception:
                    pass

            win32gui.EnumWindows(cb, None)

            for h in cands:
                try:
                    c = auto.ControlFromHandle(h)
                    if not c or not c.Exists(0.1):
                        continue
                    cls = c.ClassName or ""
                    nm = c.Name or ""
                    if "PayDetailWindow" in cls or "PayWindow" in cls:
                        return c
                    # 兜底：名字是"微信"且子树里有「收款」按钮
                    if nm == "微信":
                        b = _find_button_in(c, ("收款", "确认收款"))
                        if b is not None:
                            return c
                except Exception:
                    continue
        except Exception:
            pass
        _t.sleep(0.3)
    return None


def _find_button_in(ctrl, names) -> object:
    """在控件子树里按名字找按钮。"""
    if ctrl is None:
        return None
    found = []

    def walk(c, d=0):
        if d > 8 or found:
            return
        try:
            for ch in c.GetChildren():
                try:
                    if ch.ControlTypeName == "ButtonControl":
                        nm = (ch.Name or "").strip()
                        for want in names:
                            if want and (nm == want or want in nm):
                                found.append(ch)
                                return
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(ctrl)
    return found[0] if found else None


# 卡片 Name 里出现这些字样 = 已经处理过，别再点
CARD_DONE_WORDS = ("已收款", "已被接收", "已接收", "已退还", "已过期", "已领取")


def _find_card_items(post_text: str = "", include_done: bool = False) -> list:
    """UIA 找聊天区里**还能收的**转账/红包条目，按出现顺序返回。

    **踩过（2026-11-09）**：只按金额匹配会命中之前已经收过的同金额卡片，
    点开发现没有「收款」按钮就报错。所以：
      · 跳过 Name 里带「已收款 / 已被接收 / 已退还 / 已过期」的
      · 同金额有多张时，优先**最后一张**（聊天记录里越靠后越新）
    """
    auto = _uia()
    if auto is None:
        return []
    try:
        from wechatauto import WeChatGUI
        win = getattr(WeChatGUI()._get_uia(), "_win", None)
        if win is None:
            return []
        target = []

        def walk(c, d=0):
            if d > 30:
                return
            try:
                for ch in c.GetChildren():
                    try:
                        ct = ch.ControlTypeName
                        nm = ch.Name or ""
                        if ct == "ListItemControl" and (
                                "转账" in nm or "红包" in nm):
                            if post_text and post_text not in nm:
                                pass
                            else:
                                done = any(w in nm for w in CARD_DONE_WORDS)
                                if include_done or not done:
                                    target.append(ch)
                    except Exception:
                        pass
                    walk(ch, d + 1)
            except Exception:
                pass

        walk(win)
        return target
    except Exception:
        return []


def _find_card_item(post_text: str = ""):
    """找一张**还能收的**卡片；多张时返回最后一张（最新）。"""
    items = _find_card_items(post_text, include_done=False)
    if not items:
        # 全收过了就返回 None —— 让调用方知道"没得收"，
        # 而不是去点一张已收款的卡片然后报错
        return None
    return items[-1]


def _phys_click(x: int, y: int) -> bool:
    """物理鼠标点击（SetCursorPos + mouse_event）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        import time as _t
        _t.sleep(0.25)
        u.mouse_event(0x0002, 0, 0, 0, 0)
        _t.sleep(0.08)
        u.mouse_event(0x0004, 0, 0, 0, 0)
        return True
    except Exception:
        return False


def _click_card_by_uia(post_text: str) -> bool:
    """用 UIA 定位卡片 + **物理点击左侧金额区**打开弹窗。

    **关键踩坑（2026-11-09）**：点卡片中心（rect 中点）打不开弹窗，
    必须点左侧的金额文字区 —— `rect.left + 120, rect.top + 50`。
    UIA 自带的 `.Click()` 也打不开，必须物理鼠标。
    """
    item = _find_card_item(post_text)
    if item is None:
        return False
    try:
        r = item.BoundingRectangle
        # 左侧金额区（图标 + "¥xx.xx" 那一块）
        x = int(r.left) + 120
        y = int(r.top) + 50
        if not _phys_click(x, y):
            return False
        return True
    except Exception:
        return False


def _click_button_multi(btn, dlg) -> str:
    """点收款按钮：多路重试，返回成功的方式名（空=都失败）。

    实测单一路径经常不灵，多试几次总有一个中。
    """
    import time as _t
    nm = (btn.Name or "").strip() or "收款"
    try:
        r = btn.BoundingRectangle
        cx, cy = int((r.left + r.right) / 2), int((r.top + r.bottom) / 2)
    except Exception:
        cx = cy = 0

    # ① UIA Click
    try:
        btn.Click(simulateMove=True)
        _t.sleep(1.8)
        try:
            if not dlg.Exists(0.4):
                return "UIA Click"
        except Exception:
            return "UIA Click"
    except Exception:
        pass

    # ② 物理点按钮中心
    if cx:
        try:
            _phys_click(cx, cy)
            _t.sleep(1.8)
            try:
                if not dlg.Exists(0.4):
                    return "物理点击"
            except Exception:
                return "物理点击"
        except Exception:
            pass

    # ③ sidecar 物理 / 逻辑
    try:
        import wxinput
        sc = wxinput.Sidecar()
        sc.start()
        try:
            for tag, (x, y) in (("sidecar物理", (cx, cy)),
                                ("sidecar逻辑", (int(cx / 1.25), int(cy / 1.25)))):
                if not x:
                    continue
                try:
                    sc.click(x, y, mode="physical")
                except Exception:
                    continue
                _t.sleep(1.8)
                try:
                    if not dlg.Exists(0.4):
                        return tag
                except Exception:
                    return tag
        finally:
            try:
                sc.close()
            except Exception:
                pass
    except Exception:
        pass

    return ""


def close_pay_window(dlg=None) -> bool:
    """关掉转账详情弹窗（收过的弹窗只有「关闭」按钮）。"""
    if dlg is None:
        dlg = _find_pay_window(timeout=1.0)
    if dlg is None:
        return True
    try:
        dlg.GetWindowPattern().Close()
        return True
    except Exception:
        pass
    try:
        b = _find_button_in(dlg, ("关闭",))
        if b is not None:
            b.Click(simulateMove=True)
            return True
    except Exception:
        pass
    return False


def collect_by_uia(post_text: str = "", button_names=("收款", "确认收款", "开"),
                   wait_open: float = 2.2, wait_claim: float = 2.0) -> tuple:
    """用 UIA 打开卡片并点按钮。返回 (ok, 说明)。

    2026-11-09 实测唯一稳定的路径（三笔转账全部收成功）。
    """
    import time as _t
    auto = _uia()
    if auto is None:
        return False, "缺少 uiautomation"

    # 0) 先关掉可能残留的旧弹窗（它会挡住卡片点击）
    old = _find_pay_window(timeout=0.8)
    if old is not None:
        try:
            if _find_button_in(old, ("收款",)) is None:
                close_pay_window(old)
                _t.sleep(0.6)
        except Exception:
            pass

    # 1) 点卡片左侧金额区
    if not _click_card_by_uia(post_text):
        # **区分"没有这张卡片"和"已经收过了"**（2026-11-09 修复）。
        # 之前一律返回失败 -> _try_collect_pay 记重试 -> 反复点已收的卡片。
        done = _find_card_items(post_text, include_done=True)
        if done:
            return True, "这笔已经收过了（界面显示已收款）"
        return False, "UIA 没点到卡片（界面上没有匹配的转账/红包条目）"
    _t.sleep(wait_open)

    # 2) 等弹窗
    dlg = _find_pay_window(timeout=3.0)
    if dlg is None:
        return False, "卡片点了但详情弹窗没出现"

    # 3) 找按钮
    btn = _find_button_in(dlg, button_names)
    if btn is None:
        btn = _find_button_in(dlg, ("收", "确认"))
    if btn is None:
        # 没有收款按钮 = 这张已经收过了（或不是给我的）
        # 关掉弹窗，换下一张同金额的卡片再试
        close_pay_window(dlg)
        _t.sleep(0.6)
        cands = _find_card_items(post_text, include_done=False)
        # 已经点过的那张不重复点
        if len(cands) > 1:
            try:
                r2 = cands[-2].BoundingRectangle
                if _phys_click(int(r2.left) + 120, int(r2.top) + 50):
                    _t.sleep(wait_open)
                    dlg2 = _find_pay_window(timeout=3.0)
                    if dlg2 is not None:
                        btn2 = _find_button_in(dlg2, button_names)
                        if btn2 is None:
                            btn2 = _find_button_in(dlg2, ("收", "确认"))
                        if btn2 is not None:
                            how = _click_button_multi(btn2, dlg2)
                            if how:
                                return True, "已收款（方式：{}）".format(how)
                            close_pay_window(dlg2)
            except Exception:
                pass
        return False, "弹窗里没有收款按钮（可能已经收过了）"

    # 4) 多路点击
    how = _click_button_multi(btn, dlg)
    _t.sleep(max(0.3, wait_claim - 1.8))

    ok = bool(how)
    if ok:
        return True, "已收款（方式：{}）".format(how)

    # 5) 失败 -> 关掉弹窗，免得下次挡路
    close_pay_window(dlg)
    return False, "点了「{}」但弹窗没关（可能没生效）".format((btn.Name or "").strip())


def collect(gui, chat_name: str, kind: str,
            buttons: tuple = (), want_text: str = "",
            wait_open: float = 1.6, wait_claim: float = 1.4) -> tuple:
    """点开一条红包/转账并领取。

    参数:
        gui: WeChatGUI 实例
        chat_name: 会话名（群名或昵称）
        kind: "transfer" | "redpacket"
        buttons: 额外要尝试的按钮文案
        want_text: 卡片上应出现的关键词（用于精确定位，比如金额）
    返回:
        (成功?, 说明)
    """
    kws = TRANSFER_KEYWORDS if kind == "transfer" else REDPACKET_KEYWORDS

    if not gui.ensure_visible():
        return False, "微信窗口不可见"

    gui.open_chat(chat_name)
    time.sleep(0.8)

    # **优先走 UIA 直点**（2026-11-09 实测：坐标点击在 125% DPI 下不可靠，
    # UIA 的 Button.Click() 最稳）。
    try:
        _hint = (want_text or "").strip().replace("￥", "").replace("¥", "")
        _ok_uia, _msg_uia = collect_by_uia(post_text=_hint)
        if _ok_uia:
            return True, _msg_uia
        # UIA 失败不直接返回 —— 继续走下面的 OCR 路径兜底
    except Exception:
        pass

    region = _ocr_region(gui)
    pos, hit_text = _find(gui, region, kws, want_amount=want_text)

    # 没找到就往下滚一点再找（卡片可能在下面）
    if pos is None:
        try:
            gui.wheel(-500)
            time.sleep(0.6)
            pos, hit_text = _find(gui, _ocr_region(gui), kws, want_amount=want_text)
        except Exception:
            pass

    if pos is None:
        return False, "聊天区里没找到{}卡片".format(
            "转账" if kind == "transfer" else "红包")


    gui.wx_click(pos[0], pos[1])
    time.sleep(wait_open)

    # 弹窗可能是子窗口，先把全屏都扫一遍
    button_words = tuple(buttons) + (
        TRANSFER_BUTTONS if kind == "transfer" else REDPACKET_BUTTONS)
    for attempt in range(3):
        found = None
        try:
            # 全窗口扫（弹窗一般居中覆盖）
            items = gui.ocr((0, 0, int(gui.render_w), int(gui.render_h)))
        except Exception:
            items = []
        for text, x, y, w, h in items:
            t = (text or "").strip()
            if not t:
                continue
            for bw in button_words:
                if t == bw or (bw in t and len(t) <= len(bw) + 3):
                    found = (gui.origin_x + x + w // 2,
                             gui.origin_y + y + h // 2, t)
                    break
            if found:
                break
        if found:
            gui.wx_click(found[0], found[1])
            time.sleep(wait_claim)
            return True, "已点击「{}」".format(found[2])
        time.sleep(0.8)

    return False, "点开了卡片，但没在弹窗里找到收款按钮"
