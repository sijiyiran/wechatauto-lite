# -*- coding: utf-8 -*-
"""微信 <-> DSH 双向桥接。

用户需求（2026-10-19）：
  「我需要她发完后给我发好了，等他回我之类的，harness 回复完成后她再总结
    发给我，途中如果 harness 抛出选择让她发给我让我选，我选完她再帮我在
    harness 里点，点完告诉我点好了」

流程状态机：
  idle     空闲
    ↓ 主人说「把「内容」发给 harness」
  waiting  已发出去，等 DSH 生成结束
    ↓ 生成结束
    ├─ 有选项按钮 → choice  把问题+选项发给主人，等他选
    └─ 无选项     → 读回复 → 总结发给主人 → idle
  choice   等主人选
    ↓ 主人回了「1」/「上1」/选项文字
  clicking 在 DSH 里点那个按钮 → 回「点好了」→ idle

界面实测（1920x1080，窗口最大化）：
  · 助手消息行首 x≈730，用户消息行首 x≈989
  · 生成中：有「停止生成」按钮 + 「深度求索中」文本
  · 完成后：「停止生成」消失，「已完成，用时 X」出现
  · 选项按钮：主区域 x>350 的 Button，排除掉窗控/侧栏/状态栏那批固定名字
"""
from __future__ import annotations

import re
import time

import relay as RL

# 这些按钮是界面本身的功能键，不是给用户的选项
_CHROME_BTN = (
    # 窗口控制 / 侧边栏 / 顶栏
    "最小化", "最大化", "恢复", "关闭", "新建会话", "新会话", "收起侧边栏",
    "插件", "搜索会话", "视图选项", "添加工作区", "展开其余", "账号菜单",
    "新版本", "更多操作", "打开右侧边栏", "添加文件",
    "访问模式", "选择模型", "子智能体", "智能体团队", "后台任务",
    "打开", "更多打开方式", "跳转", "加载并跳转",
    # 状态栏
    "轮", "步", "tok", "上下文", "缓存", "深度求索", "请输入",
    # 生成状态
    "停止生成", "已完成", "正在运行", "正在生成",
    # **工具执行标记 —— 最容易误判成"选项"的那批**（实测 2026-10-19）
    "执行了命令", "已写入文件", "已读取", "已运行", "已创建", "已修改",
    "已删除", "已搜索", "已分析", "已保存", "正在执行", "执行中",
    "已编辑", "已应用", "已更新", "已打开", "已加载",
    # 代码块 / 消息工具栏
    "复制", "复制代码", "自动换行", "展开", "收起", "运行", "预览",
    "重新生成", "继续", "停止", "采纳", "拒绝", "撤销", "重试",
)


def _is_chrome(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    return any(c in n for c in _CHROME_BTN)


def _collect(hwnd, depth: int = 26):
    """把窗口里的 Text/Button 都收集出来：[(top, left, ctrlType, name, ctrl)]。"""
    import uiautomation as auto
    try:
        win = auto.ControlFromHandle(hwnd)
    except Exception:
        return []
    out = []

    def walk(c, d=0):
        if d > depth:
            return
        try:
            for ch in c.GetChildren():
                try:
                    ct = ch.ControlTypeName or ""
                    nm = (ch.Name or "").strip()
                    if nm and ct in ("TextControl", "ButtonControl"):
                        r = ch.BoundingRectangle
                        out.append((r.top, r.left, ct, nm, ch))
                except Exception:
                    pass
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win)
    return out


# 从助手的文字里认出"它在让我选"。
# 实测这个界面里选项**经常只是文字**（没有按钮）：
#   "1. 先修 bug  2. 先加功能"
#   "你要哪个？A. xxx  B. yyy"
#   "- 方案一：xxx  - 方案二：yyy"
_NUM_OPT = re.compile(
    r"(?:^|\n)\s*(?:[1-9][.、)）]|[A-Da-d][.、)）]|[-*]\s|方案[一二三四五]|选项[一二三四五])"
    r"\s*(.{1,60}?)(?=\n|$)", re.M)
_ASK_WORDS = ("选", "哪个", "哪一", "要哪", "确认", "继续吗", "可以吗",
              "要不要", "是否", "how", "which")


def looks_like_choice(text: str) -> bool:
    """这段文字像不像"在让用户做选择"。"""
    t = (text or "").strip()
    if not t:
        return False
    opts = _NUM_OPT.findall(t)
    if len(opts) >= 2:
        return True
    # 只有一个问句 + "选/哪个"这类词也算
    if any(w in t for w in _ASK_WORDS) and ("？" in t or "?" in t):
        return True
    return False


def extract_options(text: str) -> list:
    """把文字里的选项抽出来（去掉序号），返回文字列表。"""
    out = []
    for m in _NUM_OPT.finditer(text or ""):
        s = (m.group(1) or "").strip(" 　:：-")
        if s and len(s) <= 60:
            out.append(s)
    return out[:8]

def is_generating(hwnd) -> bool:
    """DSH 还在生成吗。"""
    for top, left, ct, nm, _ in _collect(hwnd):
        if nm == "停止生成":
            return True
        if "深度求索中" in nm:
            return True
    return False


def read_lines(hwnd) -> list:
    """把主区域的消息按行还原成 [{"who": "user"|"assistant", "text": str}]。

    做法：同一 y（±4px）的文本碎片合并成一行；行首 x < 900 判为助手，
    否则判为用户。这些阈值是实测出来的（见文件头）。
    """
    items = [x for x in _collect(hwnd) if x[2] == "TextControl"
             and x[1] > 600 and 60 <= x[0] <= 1015]
    if not items:
        return []
    items.sort(key=lambda x: (x[0], x[1]))
    lines = []
    cur = []
    cur_y = None
    for top, left, ct, nm, _ in items:
        if cur_y is None or abs(top - cur_y) <= 4:
            cur.append((left, nm))
            cur_y = top if cur_y is None else cur_y
        else:
            lines.append((cur_y, cur))
            cur = [(left, nm)]
            cur_y = top
    if cur:
        lines.append((cur_y, cur))

    out = []
    for y, frags in lines:
        frags.sort(key=lambda x: x[0])
        text = "".join(f[1] for f in frags).strip()
        if not text:
            continue
        head_x = frags[0][0]
        who = "assistant" if head_x < 900 else "user"
        out.append({"who": who, "text": text})
    return out


def last_assistant_reply(hwnd, max_blocks: int = 30) -> str:
    """取最末尾那一整段助手回复（从最后一条用户消息之后算起）。"""
    lines = read_lines(hwnd)
    if not lines:
        return ""
    # 从后往前找：最后一条 user 之后的所有 assistant
    idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i]["who"] == "user":
            idx = i
            break
    block = lines[idx + 1:] if idx >= 0 else lines
    block = [b for b in block if b["who"] == "assistant"]
    return "\n".join(b["text"] for b in block[-max_blocks:]).strip()


def find_choice_buttons(hwnd) -> list:
    """找主区域里可能是"选项"的按钮，返回 [(name, (l,t,r,b))]。"""
    out = []
    for top, left, ct, nm, ctrl in _collect(hwnd):
        if ct != "ButtonControl":
            continue
        if left < 350:
            continue          # 侧边栏
        if top < 60 or top > 1000:
            continue          # 顶栏 / 状态栏
        if _is_chrome(nm):
            continue
        if len(nm) > 60:
            continue
        try:
            r = ctrl.BoundingRectangle
            if r.right - r.left < 30 or r.bottom - r.top < 16:
                continue
            out.append((nm, (r.left, r.top, r.right, r.bottom)))
        except Exception:
            continue
    # 去重（同一个按钮可能被枚举两次）
    seen = set()
    uniq = []
    for nm, rect in out:
        k = (nm, rect[0], rect[1])
        if k in seen:
            continue
        seen.add(k)
        uniq.append((nm, rect))
    return uniq


def click_choice(hwnd, choice: str) -> tuple:
    """按用户的选择在 DSH 里操作。返回 (ok, why, 说明)。

    两条路：
      ① 界面上有可点按钮 -> 点它
      ② 没有按钮（选项只是文字）-> 把用户的话打进输入框、回车
    """
    choice = (choice or "").strip()
    if not choice:
        return False, "没给选择", ""

    btns = find_choice_buttons(hwnd)
    if btns:
        # 序号：用户回 "1" / "上1" / "第2个"
        m = re.search(r"(\d+)", choice)
        if m and len(choice) <= 6:
            n = int(m.group(1))
            if 1 <= n <= len(btns):
                nm, rect = btns[n - 1]
                if _click_rect(rect):
                    return True, "按序号点了", "点了「{}」".format(nm)
        # 文字匹配
        for nm, rect in btns:
            if choice in nm or nm in choice:
                if _click_rect(rect):
                    return True, "按文字点了", "点了「{}」".format(nm)
        if len(choice) >= 2:
            for nm, rect in btns:
                if nm.startswith(choice[:2]):
                    if _click_rect(rect):
                        return True, "按前缀点了", "点了「{}」".format(nm)
        names = " / ".join(b[0] for b in btns[:6])
        # 按钮对不上 —— **不要放弃，退回打字**
        print("[桥] 按钮没匹配（现有：{}），改成打字发送".format(names))

    # ② 没有按钮 / 按钮对不上 -> 打字
    ok, why = type_to_app(hwnd, choice)
    if ok:
        return True, "打字发送", "回了「{}」".format(choice[:30])
    return False, why, ""


def type_to_app(hwnd, text: str) -> tuple:
    """把一句话打进目标应用的输入框并回车。返回 (ok, why)。"""
    input_rect, _ctrl = RL.find_input_box(hwnd)
    if not input_rect:
        return False, "找不到输入框"
    try:
        import win32con
        import win32gui
        import pyautogui
        import time as _t
        pyautogui.FAILSAFE = False
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            _t.sleep(0.5)
        win32gui.SetForegroundWindow(hwnd)
        _t.sleep(0.4)
        l, tp, r, b = input_rect
        pyautogui.click((l + r) // 2, (tp + b) // 2)
        _t.sleep(0.3)
        if not RL._set_clipboard(text):
            return False, "剪贴板写入失败"
        pyautogui.hotkey("ctrl", "v")
        _t.sleep(0.5)
        pyautogui.press("enter")
        _t.sleep(0.4)
        return True, "已发送"
    except Exception as e:
        return False, "打字失败：{}".format(type(e).__name__)


def _click_rect(rect) -> bool:
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        l, t, r, b = rect
        pyautogui.click((l + r) // 2, (t + b) // 2)
        time.sleep(0.8)
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────
# 状态机
# ──────────────────────────────────────────────

class Bridge:
    """一次对话的桥接状态。"""

    IDLE = "idle"
    WAITING = "waiting"
    CHOICE = "choice"

    def __init__(self, cfg: dict, log_fn, say_fn):
        self.cfg = cfg or {}
        self.log = log_fn
        self.say = say_fn           # 往微信发消息
        self.state = self.IDLE
        self.hwnd = None
        self.started = 0.0
        self.deadline = 0.0
        self.last_reply = ""
        self.options = []
        self.question = ""
        self.polls = 0

    # ---------- 发起 ----------
    def start(self, target: str, content: str,
              new_session: bool = False, session: str = "") -> dict:
        r = RL.send_to_app(target, content, new_session=new_session,
                           session=session)
        if not r.get("ok"):
            return r
        hwnd, _ = RL.find_target_window(target)
        self.hwnd = hwnd
        self.started = time.time()
        self.deadline = self.started + float(self.cfg.get("reply_timeout_s", 600))
        self.state = self.WAITING
        self.polls = 0
        self.last_reply = ""
        self.options = []
        self.log("[桥] 已发到 DSH，开始等它回复")
        self.say("发好了，等它回")
        return r

    # ---------- 每次主循环调 ----------
    def poll(self) -> None:
        if self.state == self.IDLE:
            return
        now = time.time()
        if now > self.deadline:
            self.log("[桥] 等太久了，放弃")
            self.say("它太久没回 我先不等了")
            self.state = self.IDLE
            return
        if not self.hwnd:
            self.state = self.IDLE
            return

        self.polls += 1
        if self.polls % 4:          # 每 4 轮看一次，省 CPU
            return

        try:
            if is_generating(self.hwnd):
                return
        except Exception:
            return

        # 生成完了，稍等一下让界面稳定
        time.sleep(0.8)

        if self.state == self.WAITING:
            # 看看有没有抛出选项
            btns = []
            try:
                btns = find_choice_buttons(self.hwnd)
            except Exception:
                pass
            reply = ""
            try:
                reply = last_assistant_reply(self.hwnd)
            except Exception:
                pass

            # 判定"在让我选"：**按钮 or 文字** 都算。
            # 单个按钮不算（那通常是工具标记或"继续"键）；文字要有 2 个以上选项，
            # 或者一个问句 + "选/哪个"这类词。
            text_opts = extract_options(reply) if reply else []
            is_choice = (len(btns) >= 2) or looks_like_choice(reply)
            if is_choice:
                self.question = (reply or "")[-600:]
                self.state = self.CHOICE
                opts_to_show = ([b[0] for b in btns] if len(btns) >= 2
                                else text_opts)
                self.log("[桥] DSH 抛出选择（按钮 {} 个，文字选项 {} 个）".format(
                    len(btns), len(text_opts)))
                lines = ["它问你："]
                if self.question:
                    lines.append(self.question)
                if opts_to_show:
                    lines.append("")
                    for i, s in enumerate(opts_to_show[:8], 1):
                        lines.append("{}. {}".format(i, s))
                lines.append("")
                lines.append("回我序号或名字就行")
                self.say("\n".join(lines))
                return

            # 没有选项 -> 总结回复
            if reply:
                self.last_reply = reply
                self.log("[桥] DSH 回复了 {} 字".format(len(reply)))
                self.say(self._summarize(reply))
            else:
                self.say("它回完了 但我没读到内容")
            self.state = self.IDLE
            return

        if self.state == self.CHOICE:
            # 等用户选，这里不做动作（由 on_user_choice 处理）
            return

    # ---------- 用户选了 ----------
    def on_user_choice(self, text: str) -> bool:
        """用户回了一句，如果是在选选项就点它。返回是否处理了。"""
        if self.state != self.CHOICE:
            return False
        ok, why, desc = click_choice(self.hwnd, text)
        if ok:
            self.log("[桥] 已处理选择：{} ({})".format(desc, why))
            self.say("点好了，{}".format(desc))
            self.state = self.IDLE
        else:
            self.log("[桥] 处理选择失败：{}".format(why))
            self.say("没点上：" + why)
        return True

    # ---------- 总结 ----------
    def _summarize(self, reply: str) -> str:
        """把 DSH 的回复压缩成一小段发给微信。"""
        max_len = int(self.cfg.get("summary_max_chars", 300))
        try:
            import brain as _B
            user = ("下面这段是另一个 AI 助手刚给你的回复。"
                    "对方在微信上问了你一件事，你把问题转给那个助手了，现在它回完了。\n\n"
                    "【那个助手的回复】\n" + reply[:3000] + "\n\n"
                    "【你的任务】用你自己的语气，把它的结论转告对方。"
                    "抓重点，别逐字复述，{} 字以内。直接输出要说的话。").format(max_len)
            txt = _B.llm_plain(self.cfg, _B.read_api_key(self.cfg),
                               "", user)
            txt = (txt or "").strip()
            if txt:
                return txt[:max_len]
        except Exception as e:
            self.log("[桥] 总结失败：{}".format(type(e).__name__))
        # 兜底：截断原文
        return reply[:max_len]
