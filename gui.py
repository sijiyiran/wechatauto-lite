#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WeChat 自动回复 —— 桌面控制台

设计原则：**界面只是外壳，机器人本体是独立的守护进程。**
所以关掉这个窗口，群里的机器人照样在跑 —— 想真停就用界面上的「停止」按钮。

界面分两块：
  · 主面板（首页）：总开关 + 运行状态 + 实时日志
  · 设置：把 config.json 里常用的项做成表单，不用手改 JSON

外观参考 Linear（linear.app）的设计语言：
  · 近黑画布 + 炭色卡片 + 发丝线描边
  · 单一品牌色（淡紫 #5e6ad2）只用在主操作上，不做装饰
  · 状态用彩色圆点和语义色，信息密度高但不吵

跑法：
  python gui.py
打包：
  pyinstaller --noconsole --name WeChatAutoReply gui.py
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

APP_NAME = "WeChat 自动回复"
# 自动更新（GitHub Release）
_UPD = None
try:
    import updater as _UPD
except Exception:
    _UPD = None

VERSION = "2.0"

# ─────────────────────────────────────────────────────────────── 色板 ──
# 取自 awesome-design-md / linear.app DESIGN.md，按桌面小窗微调过对比度。
C = {
    "canvas":      "#0e0f13",   # 画布
    "surface":     "#16171c",   # 卡片
    "surface-2":   "#1e2027",   # 输入框 / 悬停
    "surface-3":   "#25272f",
    "hairline":    "#2b2d35",   # 发丝线
    "hairline-2":  "#3a3d47",
    "ink":         "#f7f8f8",   # 主文字
    "ink-muted":   "#c4c8d2",
    "ink-subtle":  "#8a8f9c",   # 次要文字
    "accent":      "#5e6ad2",   # Linear 紫 —— 只给主按钮
    "accent-hi":   "#7580e8",
    "accent-lo":   "#4854b8",
    "ok":          "#4cc38a",
    "ok-bg":       "#12271e",
    "err":         "#e5484d",
    "err-bg":      "#2b1416",
    "warn":        "#f5a623",
    "warn-bg":     "#2b2214",
    "term-bg":     "#0b0c10",   # 日志终端
    "term-fg":     "#d6d9e0",
}
FONT = "Microsoft YaHei UI"
MONO = "Consolas"


# ───────────────────────────────────────────────────────────── 动效 ──
# 原则（taste-skill / impeccable）：
#   · 动效只负责"解释状态变化"——哪里变了、忙不忙、还活着；
#     不做无目的的装饰动画。
#   · 所有颜色中间值都从现有色板 *派生*（mix 混合），不引入新配色。
#   · config 里 ui.reduced_motion = true 时，全部动效退化为直接切换。

MOTION = True          # App 启动时按 config 覆盖
SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # 忙碌指示的 braille 旋转帧


def motion_ok() -> bool:
    return MOTION


def mix(a: str, b: str, t: float) -> str:
    """两个 #rrggbb 按 t 混合（t=0 -> a，t=1 -> b）。"""
    try:
        a1, a2, a3 = (int(a[i:i + 2], 16) for i in (1, 3, 5))
        b1, b2, b3 = (int(b[i:i + 2], 16) for i in (1, 3, 5))
        f = lambda x, y: max(0, min(255, round(x + (y - x) * t)))  # noqa: E731
        return "#{:02x}{:02x}{:02x}".format(f(a1, b1), f(a2, b2), f(a3, b3))
    except (ValueError, IndexError):
        return a


def ease_out(t: float) -> float:
    """cubic ease-out：起步快、收尾缓 —— 桌面软件该有的利落手感。"""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _run_tween(holder, apply, ms: int, steps: int = 10, delay: int = 0):
    """通用补间：apply(t) 在 ms 毫秒内让 t 从 0 走到 1。
    同一 holder（控件）同一时刻只保留一条动画，新动画直接顶掉旧的，
    避免两条循环往同一个属性上抢。"""
    prev = getattr(holder, "_anim_id", None)
    if prev:
        try:
            holder.after_cancel(prev)
        except Exception:
            pass
    gap = max(15, ms // steps)

    def step(i):
        try:
            apply(ease_out(i / steps))
        except Exception:
            return
        if i < steps:
            holder._anim_id = holder.after(gap, lambda: step(i + 1))
    if delay > 0:
        holder._anim_id = holder.after(delay, lambda: step(0))
    else:
        step(0)


def animate_color(widget, attr, a: str, b: str, ms: int = 280,
                  steps: int = 10, delay: int = 0):
    """把 widget 的颜色属性从 a 渐变到 b。"""
    if not MOTION or ms <= 0:
        try:
            widget.configure(**{attr: b})
        except Exception:
            pass
        return
    _run_tween(widget,
               lambda t: widget.configure(**{attr: mix(a, b, t)}),
               ms, steps, delay)


def animate_tag_bg(txt, tag, a: str, b: str, ms: int = 650, steps: int = 10):
    """Text 控件里某个 tag 的背景色渐变（新日志"点亮"用）。"""
    if not MOTION or ms <= 0:
        try:
            txt.tag_configure(tag, background=b)
        except Exception:
            pass
        return
    _run_tween(txt,
               lambda t: txt.tag_configure(tag, background=mix(a, b, t)),
               ms, steps)


def fade_window(win, ms: int = 320, steps: int = 10):
    """窗口淡入。一次性开场动效；wm 不支持 alpha 就直接显示。"""
    if not MOTION or ms <= 0:
        try:
            win.attributes("-alpha", 1.0)
        except Exception:
            pass
        return
    try:
        win.attributes("-alpha", 0.0)
    except Exception:
        return
    gap = max(15, ms // steps)

    def step(i):
        try:
            win.attributes("-alpha", max(0.02, ease_out(i / steps)))
        except Exception:
            return
        if i < steps:
            win.after(gap, lambda: step(i + 1))
        else:
            try:
                win.attributes("-alpha", 1.0)
            except Exception:
                pass
    win.after(30, lambda: step(1))


def _hover_cells(widgets, on: bool):
    """状态格的悬停反馈：底色亮一档（surface-2 -> surface-3）。"""
    bgs = C["surface-3"] if on else C["surface-2"]
    for w in widgets:
        try:
            w.configure(bg=bgs)
        except Exception:
            pass


# ────────────────────────────────────────────────── 路径（含打包后的兼容）──
def app_root() -> pathlib.Path:
    """找到程序根目录（brain.py / config.json 所在的地方）。

    两种运行方式都要能工作：
      · 直接跑 .py：就是脚本所在目录
      · PyInstaller 打包后：sys.frozen 为真。**正常情况下机器人本体
        （brain.py、config.json、prompts\\、stickers\\）由打包脚本复制到
        exe 旁边**，所以 exe 所在目录就是根目录 —— 这样用户看得见、改得动配置。
        但也兼容被塞进 _internal\\ 的情况（PyInstaller 6 的 --add-data 默认行为），
        免得改一次打包方式界面就找不到配置。
    """
    if getattr(sys, "frozen", False):
        exe_dir = pathlib.Path(sys.executable).resolve().parent
        for cand in (exe_dir, exe_dir / "brain",
                     exe_dir / "_internal", exe_dir / "_internal" / "brain"):
            try:
                if (cand / "config.json").exists() or (cand / "brain.py").exists():
                    return cand
            except OSError:
                continue
        return exe_dir
    return pathlib.Path(__file__).resolve().parent


ROOT = app_root()

CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "daemon.log"
SUPERVISOR_LOG = ROOT / "supervisor.log"
HEARTBEAT = ROOT / "heartbeat.txt"
STATE_PATH = ROOT / "state.json"
STICKERS_PATH = ROOT / "stickers.json"
ALERT_PATH = ROOT / "ALERT.txt"      # 守护器连续失败时写的报警文件
PYW = None          # 启动时探测


# ──────────────────────────────────────────────────────────── 配置读写 ──
def load_cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_cfg(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def dig(cfg: dict, path: str, default=None):
    """按 "a.b.c" 取值。"""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def bury(cfg: dict, path: str, value) -> None:
    """按 "a.b.c" 写值，中间层不存在就建。"""
    parts = path.split(".")
    cur = cfg
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


# ──────────────────────────────────────────────────────── 进程与状态 ──
def _cim(names=("python.exe", "pythonw.exe", "Weixin.exe")):
    """拿进程列表。用 wmic/CIM；失败返回空表（界面不至于因此崩）。"""
    out = []
    try:
        q = " or ".join(f"Name='{n}'" for n in names)
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"{q}\" | "
             f"Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = (ps.stdout or "").strip()
        if not txt:
            return []
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        for d in data:
            out.append({
                "pid": d.get("ProcessId"),
                "name": d.get("Name") or "",
                "cmd": d.get("CommandLine") or "",
            })
    except Exception:
        pass
    return out


def read_sent_times() -> list:
    """读 state.json 里记录的「每次发送的时间戳」。

    界面上要显示「过去 10 分钟已经回了几条」，这个数据只有 brain 进程
    在写（record_send 往 state["sent"] 追加）。GUI 每次刷新重读一次，
    文件很小，开销可以忽略。
    """
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    out = []
    for x in (s.get("sent") or []):
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def snapshot() -> dict:
    """采集一次运行状态（界面每秒调一次，必须快且不抛异常）。"""
    st = {
        "wechat": False, "wechat_pid": "",
        "supervisor": False, "brain": False, "brain_pid": "",
        "heartbeat_age": None, "log_age": None,
    }
    for p in _cim():
        n, cmd = p["name"].lower(), p["cmd"]
        if n == "weixin.exe":
            if "group-brain" not in cmd:      # 我们自己起的也算，但优先算微信本体
                st["wechat"] = True
                st["wechat_pid"] = p["pid"]
        if "group-brain" in cmd and "supervisor.py" in cmd:
            st["supervisor"] = True
        if "group-brain" in cmd and "brain.py" in cmd:
            st["brain"] = True
            st["brain_pid"] = p["pid"]
    if not st["wechat"]:                      # 备用判断
        for p in _cim(("Weixin.exe",)):
            st["wechat"] = True
            st["wechat_pid"] = p["pid"]
    for key, path in (("heartbeat_age", HEARTBEAT), ("log_age", LOG_PATH)):
        try:
            st[key] = time.time() - path.stat().st_mtime
        except OSError:
            st[key] = None
    return st


def tail(path: pathlib.Path, n: int = 200) -> list[str]:
    """读文件末尾 n 行。文件很大时只读尾部，不整份载入。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as f:
            if size > 256 * 1024:
                f.seek(-256 * 1024, os.SEEK_END)
                f.readline()                  # 丢掉半行
            data = f.read()
        text = data.decode("utf-8", "replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


# ──────────────────────────────────────── 模型预设 ──
# 普通人不知道 base_url 该填什么，选一个预设就自动填好。
# 想用别的模型选「自定义」再手填。
MODEL_PRESETS = {
    "DeepSeek（默认）": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "provider": "deepseek",
        "note": "便宜、中文好。看图要另配 deepseek-flash。",
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "note": "gpt-4o-mini 便宜且能看图。",
    },
    "通义千问（阿里云）": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "provider": "openai_compat",
        "note": "兼容 OpenAI 接口。qwen-vl-plus 能看图。",
    },
    "Kimi（月之暗面）": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "provider": "openai_compat",
        "note": "长上下文。",
    },
    "智谱 GLM": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "provider": "openai_compat",
        "note": "glm-4-flash 免费额度大。",
    },
    "自定义（自己填）": {
        "base_url": "",
        "model": "",
        "provider": "custom",
        "note": "只要兼容 OpenAI 的 /chat/completions 接口就能用。",
    },
}


def edition_info(cfg: dict) -> tuple:
    """返回 (标识文字, 颜色, 说明)。

    为什么要区分：开发版和便携版共用同一份 gui.py，
    但两者能力完全不同（开发版有朋友圈/加好友/转述，便携版阉割了）。
    标题栏如果不标清楚，用户根本不知道自己在用哪个，
    会疑惑「为什么我的设置里没有朋友圈」。

    取值来自 config 的 ui.edition：
      dev      -> 开发版（橙色）
      portable -> 便携版（灰色）
      其它/缺省 -> 按 ui.extra_tabs 推断
    """
    ui = cfg.get("ui") or {}
    ed = str(ui.get("edition") or "").strip().lower()
    if not ed:
        ed = "dev" if bool(ui.get("extra_tabs", True)) else "portable"
    if ed == "dev":
        return ("开发版", "#f5a623",
                "完整功能：朋友圈、好友申请、转述、主动搭话都可用")
    if ed == "portable":
        return ("便携版", "#8a8f9c",
                "精简版：只回群聊和私聊，其余功能已关闭")
    return (ed, "#8a8f9c", "")


def detect_current_wxid() -> tuple:
    """读出当前登录的微信 wxid。返回 (wxid, 昵称, 错误说明)。"""
    try:
        from wechatauto import WeChatDB
    except Exception as e:
        return "", "", "读不到微信库（{}）".format(type(e).__name__)
    try:
        db = WeChatDB()
        info = db.get_self_info() or {}
        wxid = (info.get("username") or "").strip()
        nick = (info.get("nick_name") or "").strip()
        if not wxid:
            return "", "", "微信库没返回当前账号"
        return wxid, nick, ""
    except Exception as e:
        return "", "", "{}: {}".format(type(e).__name__, str(e)[:70])


def _kill_own_processes() -> int:
    """把这个目录下的 supervisor.py / brain.py 全杀掉，返回杀了几个。

    为什么不用 psutil：便携版要尽量减少第三方依赖，标准库 + WMI 够用。
    为什么按本目录匹配：换盘符（U 盘是 E:\\ 或 F:\\）也能正确命中，
    写死路径的话在别人电脑上就失效了。
    """
    root = str(ROOT).replace("'", "''")
    ps = (
        "$pat = '" + root + "\\*'; "          # 注意：目录 + 反斜杠 + 通配
        "$n = 0; "
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like $pat -and "
        "($_.CommandLine -like '*brain.py*' -or $_.CommandLine -like '*supervisor.py*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; "
        "Write-Output $n"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


STICKERS_JSON = ROOT / "stickers.json"


def load_stickers() -> dict:
    """读表情包登记表。结构：
    {"s01": {"file": "stickers/s01.png", "desc": "说明", "when": "何时用",
             "disabled": true, "min_interval_s": 1800}}
    """
    try:
        if not STICKERS_JSON.exists():
            return {}
        d = json.loads(STICKERS_JSON.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_stickers(d: dict) -> None:
    """写表情包登记表（无 BOM）。"""
    STICKERS_JSON.write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def list_to_text(cur) -> str:
    """把配置里的列表显示成人类能编辑的一行：wxid_a, wxid_b

    **不能直接 str(list)**：那样 Entry 里会显示 [\'wxid_a\', \'wxid_b\']，
    用户不动它点保存，save() 再按逗号拆开，就变成 ["[\'wxid_a\', \'wxid_b\']"]，
    每存一次多嵌套一层。实测把 safety.allowed_accounts 写成了三层字符串，
    机器人当场拒绝工作（2026-10-28）。
    """
    if isinstance(cur, (list, tuple)):
        return ", ".join(str(x).strip() for x in cur if str(x).strip())
    return "" if cur is None else str(cur)


def text_to_list(raw: str) -> list:
    """把一行文本还原成列表，顺便剥掉历史遗留的方括号/引号外壳。"""
    s = (raw or "").strip()
    # 反复剥 [ ... ] 外壳（兼容被套了几层的旧配置）
    for _ in range(10):
        if len(s) >= 2 and s[0] in "[(" and s[-1] in "])":
            s = s[1:-1].strip()
        else:
            break
    parts = []
    for x in s.split(","):
        # 去掉每项外面可能残留的引号和方括号
        y = x.strip().strip("'\"").strip().strip("[]").strip().strip("'\"")
        if y:
            parts.append(y)
    # 去重保序
    seen = set()
    out = []
    for y in parts:
        if y not in seen:
            seen.add(y)
            out.append(y)
    return out


def run_script(name: str) -> tuple[bool, str]:
    """跑 group-brain 里的 .cmd（启动/停止）。"""
    cmd = ROOT / name
    if not cmd.exists():
        return False, f"找不到 {cmd}"
    try:
        r = subprocess.run(["cmd", "/c", str(cmd)], capture_output=True,
                           text=True, timeout=90, cwd=str(ROOT),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "执行超时"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ────────────────────────────────────────────────────────────── 主面板 ──
class App(tk.Tk):
    def __init__(self, skip_start_check: bool = False,
                 defer_anim: bool = False):
        super().__init__()
        # **立刻藏起来。** Tk 窗口创建后默认可见，而 _build_ui() 要 1~2 秒，
        # 不藏的话用户会先看到一个空的主窗口，再看到启动动画，最后又一次主窗口。
        self.withdraw()
        self._skip_start_check = skip_start_check
        # defer_anim=True 时不在构造阶段排开场动画 ——
        # 否则窗口会在启动动画之前先闪一下（fade_window 的 after 回调拦不住）。
        self._defer_anim = defer_anim
        # **启动动画期间主窗口必须保持隐藏。**
        # 踩过（2026-11-08）：_ensure_on_screen 会在 after(300) 时判定
        # "窗口不在屏幕内" -> deiconify()，把 withdraw 的主窗口又拉出来，
        # 于是启动时先闪一下 GUI、再出动画窗口、最后又出 GUI。
        self._splash_active = defer_anim
        # 先读配置 —— 下面 edition_info / 标题 / 外观都要用它
        self.cfg = load_cfg()
        _ed_txt, _ed_color, _ed_note = edition_info(self.cfg)
        self.title(f"{APP_NAME}  v{VERSION}  [{_ed_txt}]")
        self._edition_txt = _ed_txt
        self._edition_color = _ed_color
        self._edition_note = _ed_note
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.configure(bg=C["canvas"])
        # 应用图标：程序目录下的 app.ico（多尺寸 16~256）。
        # 找不到就静默跳过 —— 图标不该成为启动失败的理由。
        try:
            _ico = ROOT / "app.ico"
            if _ico.exists():
                self.iconbitmap(str(_ico))
        except Exception:
            pass

        self._log_pos = 0          # 已经读过的日志字节位置（增量读取用）
        self._busy = False
        self._cards = []           # 卡片按构建顺序记下来（开场动效用）
        # 动效总开关：config ui.reduced_motion=true 时全部动效退化为直接切换
        global MOTION
        try:
            MOTION = not bool((self.cfg.get("ui") or {}).get("reduced_motion", False))
        except Exception:
            MOTION = True

        self._build_style()
        self._build_ui()
        # **点右上角 X 也要把后台停掉。**
        # 便携版是"插U盘就能用、拔U盘就走"的形态，窗口一关后台还在跑
        # 会很奇怪（U盘拔了进程还在），所以这里接管关闭事件。
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._closing = False
        # 开场动效：窗口淡入；卡片边框按顺序亮一下，把视线引到总开关。
        # 只在开场做一次，属于"有目的的引导"，不是常驻装饰。
        if not getattr(self, "_defer_anim", False):
            # 不走启动动画时，构建完就显示（并播开场动效）
            self.deiconify()
            fade_window(self, 340)
            self.after(140, self._entrance)
        # 总开关状态灯开始呼吸（常驻但极克制，2.4s 一个周期）
        self._breathe_switch()
        self._render_switch()      # 启动时立刻按配置显示，别用硬编码的初值
        self._tick()
        # **启动后 300ms 检查一次窗口是不是跑到屏幕外了。**
        # 踩过（2026-10-28）：Tk 在某些时序下（被最小化、多显示器切换、
        # 上次异常退出）会把窗口放在 (-32000,-32000) —— 那是 Windows
        # 最小化窗口的标准坐标。用户看到的是"窗口没了"。
        self.after(300, self._ensure_on_screen)
        # 启动 3 秒后后台查一次更新（不阻塞界面）
        # 用启动动画时，检查已经在 splash 里做过，这里就不再重复
        if not getattr(self, "_skip_start_check", False):
            self.after(3000, self._check_update_on_start)


    # ---- 启动检查更新 ----
    def _check_update_on_start(self):
        """启动后后台查一次更新，有新版本弹窗提醒。

        为什么要 try 包住所有东西：查更新要走网络，
        没网/仓库没配/GitHub 抽风都不能影响开机。
        """
        try:
            if _UPD is None:
                return
            cfg = load_cfg()
            if not dig(cfg, "update.check_on_start", True):
                return
            if not dig(cfg, "update.enabled", True):
                return
            edition = (dig(cfg, "ui.edition", "") or
                       dig(cfg, "update.edition", "") or "dev")
            repo = _UPD.repo_of(cfg, edition)
            if not repo:
                return      # 还没配置仓库，静默跳过

            def work():
                try:
                    res = _UPD.check_latest(cfg, edition)
                except Exception:
                    return
                if res:
                    self.after(0, lambda: self._on_start_update(res))

            import threading
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            pass

    def _on_start_update(self, res):
        """发现新版本时弹窗：显示更新内容，让用户选是否更新。"""
        try:
            # 先拉 Release 里的更新说明
            notes = ""
            try:
                rel = _UPD._get_json_with_sources(
                    _UPD.API_TAG_SOURCES,
                    {"repo": res["repo"], "tag": res["tag"]})
                notes = (rel.get("body") or "").strip()
            except Exception:
                notes = "（取更新说明失败，可以稍后在设置里看）"

            def _go_update():
                # 打开设置页并落到「更新」tab
                self.open_settings(tab="更新")

            UpdateDialog(self, res, notes, on_update=_go_update)
        except Exception:
            # 兜底：退回原来的简单弹窗
            try:
                if messagebox.askyesno(
                        APP_NAME,
                        "发现新版本 v{}\n\n现在去更新吗？".format(
                            res["version"])):
                    self.open_settings(tab="更新")
            except Exception:
                pass

    # ---- 外观：把 ttk 控件统一刷成暗色（Linear 风格） ----
    def _ensure_on_screen(self):
        """窗口要是被最小化或跑到屏幕外，就拉回屏幕中央。

        为什么需要：Tk 窗口被最小化时坐标是 (-32000,-32000)，
        用户看到的是"面板不见了"，但进程好好活着 —— 很容易误判成崩溃。
        """
        try:
            # **启动动画还在的时候别碰窗口。**
            # 此时主窗口是 withdraw 的，坐标/尺寸都不可信，
            # 下面的"不在屏幕内就 deiconify"会把它提前拉出来。
            if getattr(self, "_splash_active", False):
                self.after(300, self._ensure_on_screen)
                return
            if self.state() == "iconic":
                self.deiconify()
            self.update_idletasks()
            x, y = self.winfo_x(), self.winfo_y()
            w, h = self.winfo_width(), self.winfo_height()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            # 只要标题栏还露在屏幕里就算正常
            on_screen = (-50 < x < sw - 100) and (-20 < y < sh - 100)
            if on_screen and w > 200 and h > 150:
                return
            nw, nh = 1180, 760
            nx = max(20, (sw - nw) // 2)
            ny = max(20, (sh - nh) // 3)
            self.geometry("{}x{}+{}+{}".format(nw, nh, nx, ny))
            self.deiconify()
            self.lift()
        except Exception:
            pass

    def _entrance(self):
        """开场：各卡片边框按构建顺序从品牌色淡回发丝线。
        一次性的视线引导（总开关 → 运行控制 → 状态 → 日志），不重复出现。"""
        if not motion_ok():
            return
        for i, card in enumerate(getattr(self, "_cards", [])):
            animate_color(card, "highlightbackground",
                          C["accent-hi"], C["hairline"], ms=520, delay=i * 90)

    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")          # clam 才允许完全自定颜色
        except Exception:
            pass

        st.configure(".", background=C["canvas"], foreground=C["ink"],
                     fieldbackground=C["surface-2"], bordercolor=C["hairline"],
                     lightcolor=C["surface"], darkcolor=C["canvas"],
                     troughcolor=C["surface"], selectbackground=C["accent-lo"],
                     selectforeground="#ffffff",
                     font=(FONT, 10))
        st.configure("TFrame", background=C["canvas"])
        st.configure("Card.TFrame", background=C["surface"])
        st.configure("TLabel", background=C["canvas"], foreground=C["ink"])
        st.configure("Card.TLabel", background=C["surface"], foreground=C["ink"])
        st.configure("Subtle.TLabel", background=C["surface"],
                     foreground=C["ink-subtle"], font=(FONT, 8))
        st.configure("H1.TLabel", font=(FONT, 13, "bold"))
        st.configure("Val.TLabel", font=(FONT, 10))

        # —— 按钮：默认 = 幽灵灰；Accent = 紫底白字；Danger = 红字 ——
        st.configure("TButton", background=C["surface-2"],
                     foreground=C["ink-muted"], borderwidth=0, focusthickness=0,
                     focuscolor=C["surface-2"], relief="flat",
                     font=(FONT, 10), padding=(12, 6))
        st.map("TButton",
               background=[("pressed", C["surface-3"]), ("active", C["surface-3"])],
               foreground=[("disabled", C["ink-subtle"]), ("active", C["ink"])],
               bordercolor=[("!disabled", C["surface-2"])],
               lightcolor=[("!disabled", C["surface-2"])],
               darkcolor=[("!disabled", C["surface-2"])])

        st.configure("Ghost.TButton", background=C["canvas"],
                     foreground=C["ink-subtle"], padding=(10, 5),
                     font=(FONT, 10))
        st.map("Ghost.TButton",
               background=[("pressed", C["surface-2"]), ("active", C["surface-2"])],
               foreground=[("disabled", C["ink-subtle"]), ("active", C["ink"])],
               bordercolor=[("!disabled", C["canvas"])],
               lightcolor=[("!disabled", C["canvas"])],
               darkcolor=[("!disabled", C["canvas"])])

        st.configure("Accent.TButton", background=C["accent"],
                     foreground="#ffffff", font=(FONT, 11, "bold"),
                     padding=(20, 8), borderwidth=0, focusthickness=0)
        st.map("Accent.TButton",
               background=[("disabled", C["surface-3"]),
                           ("pressed", C["accent-lo"]), ("active", C["accent-hi"])],
               foreground=[("disabled", C["ink-subtle"])],
               bordercolor=[("!disabled", C["accent"])],
               lightcolor=[("!disabled", C["accent"])],
               darkcolor=[("!disabled", C["accent"])])

        # —— 输入类 ——
        st.configure("TEntry", fieldbackground=C["surface-2"],
                     foreground=C["ink"], insertcolor=C["ink"],
                     bordercolor=C["hairline"], lightcolor=C["surface-2"],
                     darkcolor=C["surface-2"], padding=(8, 5))
        st.map("TEntry",
               bordercolor=[("focus", C["accent"])],
               lightcolor=[("focus", C["accent"])],
               fieldbackground=[("disabled", C["surface"])],
               foreground=[("disabled", C["ink-subtle"])])

        st.configure("TSpinbox", fieldbackground=C["surface-2"],
                     background=C["surface-2"], foreground=C["ink"],
                     insertcolor=C["ink"], arrowcolor=C["ink-subtle"],
                     bordercolor=C["hairline"], lightcolor=C["surface-2"],
                     darkcolor=C["surface-2"], padding=(8, 5))
        st.map("TSpinbox", bordercolor=[("focus", C["accent"])])

        st.configure("TCombobox", fieldbackground=C["surface-2"],
                     background=C["surface-2"], foreground=C["ink"],
                     arrowcolor=C["ink-subtle"], bordercolor=C["hairline"],
                     lightcolor=C["surface-2"], darkcolor=C["surface-2"],
                     padding=(8, 4))
        st.map("TCombobox", bordercolor=[("focus", C["accent"])],
               fieldbackground=[("readonly", C["surface-2"])],
               selectbackground=[("readonly", C["surface-2"])],
               selectforeground=[("readonly", C["ink"])])
        try:
            st.configure("ComboboxPopdownFrame", bordercolor=C["hairline"],
                         lightcolor=C["hairline"], darkcolor=C["hairline"])
        except Exception:
            pass

        # —— 复选框 ——
        st.configure("TCheckbutton", background=C["canvas"],
                     foreground=C["ink"], focuscolor=C["canvas"],
                     indicatorbackground=C["surface-2"], padding=(0, 2))
        st.map("TCheckbutton",
               background=[("active", C["canvas"])],
               foreground=[("disabled", C["ink-subtle"]), ("active", C["ink"])],
               indicatorbackground=[("selected", C["accent"]),
                                    ("pressed", C["surface-3"])],
               indicatorforeground=[("selected", "#ffffff")])

        # —— Notebook（设置窗口的页签）——
        st.configure("TNotebook", background=C["canvas"],
                     bordercolor=C["canvas"], tabmargins=(12, 10, 12, 0))
        st.configure("TNotebook.Tab", background=C["canvas"],
                     foreground=C["ink-subtle"], padding=(16, 8),
                     font=(FONT, 10), borderwidth=0, focuscolor=C["canvas"])
        st.map("TNotebook.Tab",
               background=[("selected", C["surface"])],
               foreground=[("selected", C["ink"])],
               lightcolor=[("selected", C["accent"])],
               darkcolor=[("selected", C["surface"])])

        # —— 滚动条 / 分隔线 ——
        st.configure("TScrollbar", background=C["surface-3"],
                     troughcolor=C["surface"], bordercolor=C["surface"],
                     arrowcolor=C["ink-subtle"], arrowsize=12)
        st.map("TScrollbar", background=[("active", C["hairline-2"])])
        st.configure("TSeparator", background=C["hairline"])
        st.configure("Vertical.TScrollbar", background=C["surface-3"],
                     troughcolor=C["surface"], bordercolor=C["surface"],
                     arrowcolor=C["ink-subtle"])

        st.configure("Mono.TLabel", font=(MONO, 9),
                     background=C["canvas"], foreground=C["ink-subtle"])

    # 小工具：一张带发丝线描边的卡片
    def _card(self, parent, **pack_kw) -> tk.Frame:
        card = tk.Frame(parent, bg=C["surface"],
                        highlightbackground=C["hairline"], highlightthickness=1)
        card.pack(**pack_kw)
        # 记住构建顺序，开场动效按这个顺序点亮边框
        self._cards.append(card)
        return card

    # 小工具：卡片顶部的小节标题（eyebrow）
    @staticmethod
    def _eyebrow(parent, text) -> tk.Label:
        return tk.Label(parent, text=text, bg=C["surface"],
                        fg=C["ink-subtle"], font=(FONT, 9), anchor="w")

    # 小工具：发丝线
    @staticmethod
    def _rule(parent) -> tk.Frame:
        return tk.Frame(parent, bg=C["hairline"], height=1, bd=0)

    # ---- 布局 ----
    def _build_ui(self):
        # header 固定不滚（标题 + 设置按钮要一直看得到）
        self._build_header()
        # **底部状态栏先 pack（side=bottom）** —— 固定在窗口底部，不跟着滚。
        # pack 顺序很关键：先 bottom 再 fill 中间，否则会被 canvas 覆盖。
        bar2 = tk.Frame(self, bg=C["canvas"])
        bar2.pack(side="bottom", fill="x", padx=20, pady=(0, 12))
        self.lbl_foot = tk.Label(bar2, text="就绪", fg=C["ink-subtle"],
                                 bg=C["canvas"], font=(FONT, 9))
        self.lbl_foot.pack(side="left")

        # 其余内容放进可滚动的 canvas。
        # 踩过（2026-11-02）：加「鱼的心」卡片后内容需要 1212px，
        # 窗口只有 760px —— 日志区直接被压成 1px，而且没法滚动，
        # 用户看到的就是「显示不全且无法滑动」。
        holder = tk.Frame(self, bg=C["canvas"])
        holder.pack(fill="both", expand=True)
        self._main_canvas = tk.Canvas(holder, bg=C["canvas"],
                                      highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(holder, orient="vertical",
                            command=self._main_canvas.yview)
        self._main_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._main_canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self._main_canvas, bg=C["canvas"])
        self._body_win = self._main_canvas.create_window(
            (0, 0), window=self.body, anchor="nw")
        # 内容高度变了就更新滚动范围；宽度跟着 canvas 走
        self.body.bind(
            "<Configure>",
            lambda e: self._main_canvas.configure(
                scrollregion=self._main_canvas.bbox("all")))
        self._main_canvas.bind(
            "<Configure>",
            lambda e: self._main_canvas.itemconfigure(
                self._body_win, width=e.width))
        # 鼠标滚轮：鼠标在日志框里时让它自己滚，别抢
        def _wheel(e):
            try:
                w = e.widget
                while w is not None:
                    if isinstance(w, (tk.Text, tk.Listbox)):
                        return
                    w = getattr(w, "master", None)
                delta = int(-1 * (e.delta / 120)) if e.delta else 0
                if delta:
                    self._main_canvas.yview_scroll(delta, "units")
            except Exception:
                pass
        self._main_canvas.bind_all("<MouseWheel>", _wheel)

        self._build_alert()
        self._build_hero()
        self._build_status()
        self._build_heart()
        self._build_log()

    def _build_header(self):
        pad = tk.Frame(self, bg=C["canvas"], height=12)
        pad.pack(fill="x")
        bar = tk.Frame(self, bg=C["canvas"])
        bar.pack(fill="x", padx=20)

        left = tk.Frame(bar, bg=C["canvas"])
        left.pack(side="left")
        tk.Label(left, text=APP_NAME, bg=C["canvas"], fg=C["ink"],
                 font=(FONT, 16, "bold")).pack(side="left")
        # 版本标识：开发版橙色、便携版灰色，一眼能分清
        tk.Label(left, text=f"v{VERSION}  ·  {self._edition_txt}",
                 bg=C["surface-2"], fg=self._edition_color,
                 font=(FONT, 8, "bold"),
                 padx=8, pady=3).pack(side="left", padx=(10, 0))

        right = tk.Frame(bar, bg=C["canvas"])
        right.pack(side="right")
        self.btn_refresh = ttk.Button(right, text="刷新", style="Ghost.TButton",
                                      command=self.refresh_now, cursor="hand2")
        self.btn_refresh.pack(side="right", padx=(8, 0))
        self.btn_settings = ttk.Button(right, text="⚙ 设置", style="TButton",
                                       command=self.open_settings,
                                       cursor="hand2")
        self.btn_settings.pack(side="right")

        self._rule(self).pack(fill="x", padx=20, pady=(12, 0))

    def _build_alert(self):
        # ── 报警横幅（守护器连续失败时才有）──
        self.alert_bar = tk.Frame(self.body, bg=C["err-bg"],
                                  highlightbackground=C["err"],
                                  highlightthickness=1)
        self.lbl_alert = tk.Label(
            self.alert_bar, background=C["err-bg"], foreground="#ffb4b6",
            font=(FONT, 10, "bold"), justify="left",
            anchor="w", padx=14, pady=9, wraplength=1000)
        self.lbl_alert.pack(fill="x")
        self.btn_alert = tk.Button(
            self.alert_bar, text="看详情 ▸", command=lambda: self.open_path(ALERT_PATH),
            background=C["err-bg"], foreground="#ffb4b6", activebackground=C["err"],
            activeforeground="#ffffff", relief="flat", bd=0, cursor="hand2",
            font=(FONT, 9), padx=12)
        self.btn_alert.pack(side="right", padx=10, pady=6)
        self._alert_shown = False

    def _build_hero(self):
        hero = tk.Frame(self.body, bg=C["canvas"])
        hero.pack(fill="x", padx=20, pady=(16, 0))
        # 报警横幅要插在这一行卡片之前 —— 记下这个锚点
        self._alert_anchor = hero

        # ── 总开关卡（主视觉）──
        sw_card = self._card(hero, side="left", fill="y", padx=(0, 10))
        self.sw_card = sw_card        # 记住它：切换开关时边框要做点击反馈
        sw = tk.Frame(sw_card, bg=C["surface"], padx=20, pady=16)
        sw.pack(fill="both", expand=True)

        self._eyebrow(sw, "自动回复总开关").pack(anchor="w")
        self.lbl_switch = tk.Label(sw, text="●  已开启", font=(FONT, 20, "bold"),
                                   bg=C["surface"], fg=C["ok"])
        self.lbl_switch.pack(anchor="w", pady=(8, 2))
        self.lbl_switch_hint = tk.Label(sw, text="机器人在群里正常说话",
                                        bg=C["surface"], fg=C["ink-subtle"],
                                        font=(FONT, 9))
        self.lbl_switch_hint.pack(anchor="w", pady=(0, 12))
        self.btn_switch = ttk.Button(sw, text="停  用", width=12,
                                     style="Accent.TButton",
                                     command=self.toggle_enabled,
                                     cursor="hand2")
        self.btn_switch.pack(anchor="w", fill="x")

        # ── 运行控制卡 ──
        ops_card = self._card(hero, side="left", fill="both", expand=True)
        ops = tk.Frame(ops_card, bg=C["surface"], padx=16, pady=12)
        ops.pack(fill="both", expand=True)

        self._eyebrow(ops, "运行控制").pack(anchor="w")
        r1 = tk.Frame(ops, bg=C["surface"])
        r1.pack(fill="x", pady=(8, 8))
        self.btn_start = ttk.Button(r1, text="▶  启动", style="TButton",
                                    command=lambda: self.do_run("start-daemon.cmd", "启动"),
                                    cursor="hand2")
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(r1, text="■  停止", style="TButton",
                                   command=lambda: self.do_run("stop-daemon.cmd", "停止"),
                                   cursor="hand2")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.btn_restart = ttk.Button(r1, text="↻  重启", style="TButton",
                                      command=self.do_restart, cursor="hand2")
        self.btn_restart.pack(side="left", fill="x", expand=True, padx=(8, 0))
        r2 = tk.Frame(ops, bg=C["surface"])
        r2.pack(fill="x")
        ttk.Button(r2, text="打开配置文件夹", style="TButton", cursor="hand2",
                   command=self.open_folder).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="查看最近决策", style="TButton", cursor="hand2",
                   command=self.show_decisions).pack(side="left", fill="x",
                                                     expand=True, padx=(8, 0))

    def _build_status(self):
        st_card = self._card(self.body, fill="x", padx=20, pady=(10, 0))
        head = tk.Frame(st_card, bg=C["surface"], padx=16, pady=8)
        head.pack(fill="x")
        self._eyebrow(head, "运行状态").pack(side="left")

        self.status_grid = tk.Frame(st_card, bg=C["surface"], padx=12, pady=10)
        self.status_grid.pack(fill="x")
        for c in range(3):
            self.status_grid.columnconfigure(c, weight=1, uniform="stat")
        self.status_labels: dict[str, tk.Label] = {}
        self.status_cells: dict[str, tk.Frame] = {}
        self._status_fg: dict[str, str] = {}   # 上一拍的值颜色（变化才有动效）
        rows = [
            ("wechat", "微信客户端"), ("supervisor", "守护器"), ("brain", "大脑进程"),
            ("groups", "启用的群"), ("mode", "接话模式"), ("cooldown", "冷却 / 静默窗口"),
            ("caps", "已回（10分/时·上限）"), ("model", "模型"), ("heartbeat", "最后心跳"),
        ]
        for i, (key, label) in enumerate(rows):
            r, c = divmod(i, 3)
            cell = tk.Frame(self.status_grid, bg=C["surface-2"],
                            highlightbackground=C["hairline"], highlightthickness=1)
            cell.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
            tk.Label(cell, text=label, bg=C["surface-2"], fg=C["ink-subtle"],
                     font=(FONT, 8), anchor="w").pack(fill="x",
                                                      padx=(10, 4), pady=(6, 0))
            v = tk.Label(cell, text="—", bg=C["surface-2"], fg=C["ink"],
                         font=(FONT, 10, "bold"), anchor="w")
            v.pack(fill="x", padx=(10, 6), pady=(2, 7))
            self.status_labels[key] = v
            self.status_cells[key] = cell
            # 微交互：鼠标扫过某格，底色亮一档 —— 帮助一格格读状态
            hov = [cell] + list(cell.winfo_children())
            for w in hov:
                w.bind("<Enter>", lambda _e, ws=hov: _hover_cells(ws, True))
                w.bind("<Leave>", lambda _e, ws=hov: _hover_cells(ws, False))

    # ---- 鱼的心（情绪引擎）----
    def _build_heart(self):
        """好感度 + 九维雷达图 + 念头。"""
        card = self._card(self.body, fill="x", padx=20, pady=(10, 0))
        head = tk.Frame(card, bg=C["surface"], padx=16, pady=8)
        head.pack(fill="x")
        self._eyebrow(head, "鱼的心").pack(side="left")
        self.heart_mode_lbl = tk.Label(
            head, text="平静", bg=C["surface"], fg=C["accent"],
            font=(FONT, 9, "bold"))
        self.heart_mode_lbl.pack(side="left", padx=(10, 0))

        body = tk.Frame(card, bg=C["surface"], padx=12, pady=8)
        body.pack(fill="x")

        # 左：好感度大数字
        left = tk.Frame(body, bg=C["surface"])
        left.pack(side="left", padx=(4, 16))
        tk.Label(left, text="好感度", bg=C["surface"], fg=C["ink-subtle"],
                 font=(FONT, 8)).pack(anchor="w")
        self.heart_favor_lbl = tk.Label(
            left, text="—", bg=C["surface"], fg=C["accent"],
            font=(FONT, 30, "bold"))
        self.heart_favor_lbl.pack(anchor="w")
        self.heart_sub_lbl = tk.Label(
            left, text="", bg=C["surface"], fg=C["ink-subtle"],
            font=(FONT, 8), justify="left")
        self.heart_sub_lbl.pack(anchor="w", pady=(2, 0))

        # 右：雷达图
        self.heart_canvas = tk.Canvas(
            body, width=236, height=236, bg=C["surface"],
            highlightthickness=0)
        self.heart_canvas.pack(side="left", padx=(0, 12))

        # 最右：念头 + 最近变化
        right = tk.Frame(body, bg=C["surface"])
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="心里惦记着", bg=C["surface"], fg=C["ink-subtle"],
                 font=(FONT, 8)).pack(anchor="w")
        self.heart_thought_lbl = tk.Label(
            right, text="（没惦记什么）", bg=C["surface"], fg=C["ink"],
            font=(FONT, 9), justify="left", anchor="w", wraplength=220)
        self.heart_thought_lbl.pack(anchor="w", pady=(2, 8))
        tk.Label(right, text="最近的变化", bg=C["surface"], fg=C["ink-subtle"],
                 font=(FONT, 8)).pack(anchor="w")
        self.heart_recent_lbl = tk.Label(
            right, text="—", bg=C["surface"], fg=C["ink"],
            font=(FONT, 9), justify="left", anchor="w", wraplength=220)
        self.heart_recent_lbl.pack(anchor="w", pady=(2, 0))

    def _draw_radar(self):
        """九维雷达图。每次刷新重画（Canvas 很小，开销可忽略）。"""
        cv = getattr(self, "heart_canvas", None)
        if cv is None:
            return
        try:
            import emotion as _E
        except Exception:
            return
        try:
            snap = _E.snapshot()
        except Exception:
            return
        cv.delete("all")
        dims = list(_E.DRIVES)
        cn = _E.DIM_CN
        cx, cy, R = 118, 112, 82
        import math
        # 背景网格：4 层九边形
        for layer in (0.25, 0.5, 0.75, 1.0):
            pts = []
            for i in range(len(dims)):
                ang = -math.pi / 2 + i * 2 * math.pi / len(dims)
                pts.extend([cx + R * layer * math.cos(ang),
                            cy + R * layer * math.sin(ang)])
            cv.create_polygon(pts, outline=C["hairline"], fill="",
                              width=1)
        # 轴线 + 标签
        for i, k in enumerate(dims):
            ang = -math.pi / 2 + i * 2 * math.pi / len(dims)
            ex = cx + R * math.cos(ang)
            ey = cy + R * math.sin(ang)
            cv.create_line(cx, cy, ex, ey, fill=C["hairline"])
            lx = cx + (R + 16) * math.cos(ang)
            ly = cy + (R + 16) * math.sin(ang)
            cv.create_text(lx, ly, text=cn.get(k, k), fill=C["ink-subtle"],
                           font=(FONT, 7))
        # 数据多边形
        pts = []
        for i, k in enumerate(dims):
            v = float(snap["drives"].get(k, 0)) / 100.0
            v = max(0.03, min(1.0, v))
            ang = -math.pi / 2 + i * 2 * math.pi / len(dims)
            pts.extend([cx + R * v * math.cos(ang),
                        cy + R * v * math.sin(ang)])
        cv.create_polygon(pts, fill=C["accent-hi"], outline=C["accent"],
                          width=2, stipple="gray25")
        # 顶点小圆
        for i in range(len(dims)):
            cv.create_oval(pts[i * 2] - 2, pts[i * 2 + 1] - 2,
                           pts[i * 2] + 2, pts[i * 2 + 1] + 2,
                           fill=C["accent"], outline="")

    def _render_heart(self):
        """刷新「鱼的心」卡片。**3 秒一次**就够 —— 每秒重画雷达图太浪费。"""
        if not hasattr(self, "heart_favor_lbl"):
            return
        _now = time.time()
        if _now - getattr(self, "_heart_last", 0) < 3:
            return
        self._heart_last = _now
        try:
            import emotion as _E
            snap = _E.snapshot()
        except Exception:
            return
        try:
            self.heart_favor_lbl.configure(text="{:.0f}".format(snap["favor"]))
            self.heart_mode_lbl.configure(text=snap["mode_cn"])
            self.heart_sub_lbl.configure(
                text="责怪 {:.0f}｜期待 {:.0f}\n哄的进度 {} 轮".format(
                    snap["blame"], snap["expectation"],
                    snap["comfort_streak"]))
            if snap["thoughts"]:
                self.heart_thought_lbl.configure(
                    text="\n".join("· {}（剩 {} 分钟）".format(
                        x["cn"], x["left_min"]) for x in snap["thoughts"][:3]))
            else:
                self.heart_thought_lbl.configure(text="（没惦记什么）")
            if snap["recent"]:
                self.heart_recent_lbl.configure(
                    text="\n".join("· {}（{}）".format(x["cn"], x["ago"])
                                    for x in snap["recent"][-4:]))
            else:
                self.heart_recent_lbl.configure(text="—")
            self._draw_radar()
        except Exception:
            pass

    def _build_log(self):
        # 在滚动容器里 expand 没有意义（父容器高度由内容决定），
        # 改成固定高度 —— 日志框本身有 height 参数控制行数。
        lf = self._card(self.body, fill="x", padx=20, pady=(10, 14))
        head = tk.Frame(lf, bg=C["surface"], padx=14, pady=8)
        head.pack(fill="x")
        self._eyebrow(head, "实时日志").pack(side="left")
        ttk.Button(head, text="打开日志文件",
                   style="Ghost.TButton", cursor="hand2",
                   command=lambda: self.open_path(LOG_PATH)).pack(side="right")
        ttk.Button(head, text="清空显示", style="Ghost.TButton", cursor="hand2",
                   command=self.clear_log).pack(side="right", padx=(0, 6))

        body = tk.Frame(lf, bg=C["surface"], padx=12, pady=10)
        body.pack(fill="both", expand=True)
        inner = tk.Frame(body, bg=C["term-bg"],
                         highlightbackground=C["hairline"], highlightthickness=1)
        inner.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(
            inner, height=16, wrap="none", font=(MONO, 9),
            background=C["term-bg"], foreground=C["term-fg"],
            insertbackground=C["term-fg"], relief="flat", bd=0,
            padx=10, pady=8, selectbackground=C["accent-lo"])
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        for tag, color in (("err", "#f2797d"), ("warn", "#e2b93d"),
                           ("ok", "#63d297"), ("info", "#7cb1f5")):
            self.log.tag_config(tag, foreground=color)

        # 底部状态栏不在这里建了 —— 它要固定在窗口底部（见 _build_ui），
        # 跟着内容滚会看不到，压在外面又会叠到卡片上。

    # ──────────────────────────────────────────────── 动作 ──
    def refresh_now(self):
        self.cfg = load_cfg()
        self._render_switch()
        self._render_status()
        self._render_log()

    def toggle_enabled(self):
        self.cfg = load_cfg()
        cur = bool(self.cfg.get("enabled", True))
        self.cfg["enabled"] = (not cur)
        try:
            save_cfg(self.cfg)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"写配置失败：{e}")
            return
        # config.json 是热加载的，守护进程下一轮就生效，不用重启
        self._render_switch()
        # 切换反馈：总开关卡片的边框从品牌色淡回发丝线。
        # 状态灯颜色本身的平滑过场由呼吸灯循环做（每帧向目标色靠近）。
        sw_card = getattr(self, "sw_card", None)
        if sw_card is not None:
            animate_color(sw_card, "highlightbackground",
                          C["accent-hi"], C["hairline"], 480)
        self.foot("已" + ("停用" if cur else "开启") +
                  "自动回复（守护进程下一轮生效，无需重启）")

    def do_run(self, script: str, verb: str):
        if self._busy:
            return
        self._busy = True
        self._spin(verb)          # 底部状态行转入忙碌指示（braille 旋转）
        self._set_buttons("disabled")

        def work():
            ok, out = run_script(script)
            self.after(0, lambda: self._after_run(ok, out, verb))
        threading.Thread(target=work, daemon=True).start()

    def _after_run(self, ok: bool, out: str, verb: str):
        self._busy = False
        self._spin_stop()
        self._set_buttons("normal")
        self.foot(f"{verb}{'完成' if ok else '失败'}",
                  color=C["ok"] if ok else C["err"])
        if out.strip():
            self.append_log(out.strip().splitlines())
        if not ok:
            messagebox.showwarning(APP_NAME, f"{verb}失败，看日志窗口。")

    def do_restart(self):
        def work():
            run_script("stop-daemon.cmd")
            time.sleep(1.5)
            ok, out = run_script("start-daemon.cmd")
            self.after(0, lambda: self._after_run(ok, out, "重启"))
        if self._busy:
            return
        self._busy = True
        self._spin("重启")
        self._set_buttons("disabled")
        threading.Thread(target=work, daemon=True).start()

    def _set_buttons(self, state):
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.configure(state=state)

    def open_settings(self, tab: str = ""):
        Settings(self, tab=tab)

    def open_folder(self):
        self.open_path(ROOT)

    def open_path(self, p: pathlib.Path):
        try:
            if p.is_dir():
                os.startfile(str(p))                      # noqa: S606
            elif p.exists():
                subprocess.Popen(["notepad.exe", str(p)])
            else:
                messagebox.showinfo(APP_NAME, f"还没有这个文件：\n{p}")
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def show_decisions(self):
        p = ROOT / "decisions.jsonl"
        lines = tail(p, 60)
        win = tk.Toplevel(self)
        win.title("最近决策")
        win.geometry("900x520")
        win.configure(bg=C["canvas"])
        bar = tk.Frame(win, bg=C["canvas"])
        bar.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(bar, text="最近决策（最新在上面）", bg=C["canvas"], fg=C["ink"],
                 font=(FONT, 12, "bold")).pack(side="left")
        inner = tk.Frame(win, bg=C["term-bg"],
                         highlightbackground=C["hairline"], highlightthickness=1)
        inner.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        txt = scrolledtext.ScrolledText(
            inner, wrap="word", font=(MONO, 9), background=C["term-bg"],
            foreground=C["term-fg"], insertbackground=C["term-fg"],
            relief="flat", bd=0, padx=10, pady=8,
            selectbackground=C["accent-lo"])
        txt.pack(fill="both", expand=True)
        for ln in reversed(lines):
            try:
                d = json.loads(ln)
                txt.insert("end", "[{}] {} | {} | {}\n".format(
                    (d.get("time") or "")[5:19], d.get("group"), d.get("action"),
                    str(d.get("reason"))[:80]))
                if d.get("draft"):
                    txt.insert("end", "      草稿：{}\n".format(
                        str(d["draft"]).replace("\n", " / ")[:100]))
            except Exception:
                txt.insert("end", ln + "\n")
        txt.configure(state="disabled")
        fade_window(win, 240)

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        # 只清窗口不清文件：把位置留在当前末尾，这样接下来只显示新增的行
        try:
            self._log_pos = LOG_PATH.stat().st_size
        except OSError:
            self._log_pos = 0

    def foot(self, text: str, color: str | None = None):
        """底部状态行。给了 color 就亮一下语义色再淡回次要色：
        『这步成了/砸了』要看得见，不只是读得到。"""
        self.lbl_foot.configure(text=text)
        if color:
            animate_color(self.lbl_foot, "fg", color, C["ink-subtle"],
                          1500, steps=12)

    def _spin(self, verb: str):
        """忙碌时底部状态行放一个细小的旋转指示。
        「正在忙」要一眼可见，且不打断别的信息；结束后由 _spin_stop 停掉。"""
        self._spin_stop()
        holder = {"i": 0}

        def step():
            f = SPIN_FRAMES[holder["i"] % len(SPIN_FRAMES)]
            try:
                self.lbl_foot.configure(text=f"{f}  正在{verb}…", fg=C["ink"])
            except Exception:
                return
            holder["i"] += 1
            self._spin_id = self.after(110, step)
        step()

    def _spin_stop(self):
        if getattr(self, "_spin_id", None):
            try:
                self.after_cancel(self._spin_id)
            except Exception:
                pass
            self._spin_id = None

    # ──────────────────────────────────────────────── 渲染 ──
    # ---- 关闭 ----
    def on_close(self):
        """关窗口 = 停掉所有后台。

        便携版要求：把 U 盘一拔、窗口一关，不该还有 python 进程在后台跑。
        所以这里先停 supervisor + brain（stop-daemon.cmd 已经按本目录匹配进程，
        换盘符/换机器也能正确识别），再退出。

        用标志位防重入：停止过程里有 messagebox，用户可能再点一次 X。
        """
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            self.foot("正在关闭后台…")
            self.update_idletasks()
        except Exception:
            pass
        try:
            # 先试着体面地停（stop-daemon.cmd 按本目录匹配进程）
            run_script("stop-daemon.cmd")
        except Exception:
            pass
        # 再兜一道：按本目录精确清一次，防止有进程没被 cmd 脚本匹配到
        try:
            _kill_own_processes()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _render_switch(self):
        # 状态灯的颜色统一由 _breathe_switch 管（平滑过场 + 呼吸），这里只换文字。
        # **检测 config.json 的 mtime**：用户可能从命令行/别的地方改了开关，
        # 面板要能跟着变。踩过（2026-11-02）：启动时不调这个函数，
        # 标签永远停在硬编码的「已开启」，让人以为机器人在跑，其实总开关是关的。
        try:
            mt = CONFIG_PATH.stat().st_mtime
            if getattr(self, "_cfg_mtime", 0) != mt:
                self.cfg = load_cfg()
                self._cfg_mtime = mt
        except Exception:
            pass
        on = bool(self.cfg.get("enabled", True))
        if on:
            self.lbl_switch.configure(text="●  已开启")
            self.lbl_switch_hint.configure(text="机器人在群里正常说话")
            self.btn_switch.configure(text="停  用")
        else:
            self.lbl_switch.configure(text="●  已停用")
            self.lbl_switch_hint.configure(text="它不会在群里说任何话")
            self.btn_switch.configure(text="开  启")

    def _breathe_switch(self):
        """总开关状态灯的呼吸（常驻环境动效，2.4s 一个周期、摆幅仅 16%）。

        为什么值得常驻：这个窗口大部分时间没人盯，一个极轻的呼吸 =
        「这东西是活的、开着的」一眼可感。做法是每帧向目标色 *靠近一点*
        （指数趋近），所以开/关切换时颜色自己就平滑过渡了，不需要单独
        写切换动画，也不会和刷新打架。ui.reduced_motion 时静止为纯色。
        """
        try:
            on = bool(self.cfg.get("enabled", True))
            base = C["ok"] if on else C["err"]
            if motion_ok():
                ph = (time.time() % 2.4) / 2.4
                wave = (1 - math.cos(ph * 2 * math.pi)) / 2      # 0..1 平滑往复
                cur = self.lbl_switch.cget("fg")
                target = mix(base, C["surface"], 0.10 + 0.16 * wave)
                self.lbl_switch.configure(fg=mix(cur, target, 0.28))
            else:
                self.lbl_switch.configure(fg=base)
        except Exception:
            pass
        self.after(100, self._breathe_switch)

    def _render_status(self):
        s = snapshot()
        cfg = self.cfg
        g = self.status_labels

        def setl(key, text, color=None):
            """状态值更新。
            只在颜色语义变化时做动效（运行中→没跑、活→无心跳）：
            值先暗后亮地"点亮"，单元格边框闪一下品牌色 —— 变化的地方自己跳出来。
            普通的文字跳动（比如心跳秒数 +1）不做任何动效，避免整屏常闪。"""
            w = g[key]
            w.configure(text=text)
            fg = color or C["ink"]
            prev = self._status_fg.get(key)
            if prev is None:
                w.configure(fg=fg)
            elif prev != fg:
                animate_color(w, "fg", mix(fg, C["surface-2"], 0.55), fg, 320)
                cell = self.status_cells.get(key)
                if cell is not None:
                    animate_color(cell, "highlightbackground",
                                  C["accent-hi"], C["hairline"], 620)
            self._status_fg[key] = fg

        setl("wechat", f"{'● 运行中' if s['wechat'] else '● 没开'}"
                       f"{'  ' + str(s['wechat_pid']) if s['wechat_pid'] else ''}",
             C["ok"] if s["wechat"] else C["err"])
        setl("supervisor", "● 在跑" if s["supervisor"] else "● 没跑",
             C["ok"] if s["supervisor"] else C["err"])
        hb = s["heartbeat_age"]
        if s["brain"] and hb is not None and hb < 90:
            setl("brain", f"● 活跃 {int(hb)}s 前", C["ok"])
        elif s["brain"]:
            setl("brain", f"● 有进程 {int(hb) if hb else '?'}s 无心跳", C["warn"])
        else:
            setl("brain", "● 没跑", C["err"])

        groups = [k for k, v in (cfg.get("groups") or {}).items()
                  if isinstance(v, dict) and v.get("enabled")]
        names = []
        for k in groups:
            v = cfg["groups"][k]
            names.append(v.get("alias") or ("全部群（*）" if k == "*" else k[:12]))
        setl("groups", f"{len(groups)} 个" + (f"（{'、'.join(names[:3])}）" if names else ""))

        mode = dig(cfg, "participation.mode", "?")
        mode_cn = {"addressed_or_relevant": "点名 + 相关就接",
                   "addressed_only": "只接点名", "active": "主动插话",
                   "silent": "只旁观"}.get(mode, mode)
        setl("mode", f"{mode_cn} / {dig(cfg, 'participation.talkativeness', '?')}")
        setl("cooldown", f"{dig(cfg, 'participation.cooldown_s')} 秒 / "
                         f"{dig(cfg, 'participation.quiet_window_s')} 秒")
        # **额度 = 已用 / 上限**，不是单纯的上限。
        # 原来这里只显示配置值（8 / 40），用户以为是"当前可用额度"，
        # 看着永远不变还以为坏了（2026-10-30 反馈）。
        _now = time.time()
        _sent = read_sent_times()
        _used10 = len([x for x in _sent if _now - x < 600])
        _used60 = len([x for x in _sent if _now - x < 3600])
        _cap10 = int(dig(cfg, 'participation.max_replies_per_10min', 8) or 8)
        _cap60 = int(dig(cfg, 'participation.max_replies_per_hour', 40) or 40)
        _left10 = max(0, _cap10 - _used10)
        # 快用完变黄、用完变红，比盯着数字更容易察觉
        if _left10 <= 0:
            _cap_color = C["err"]
        elif _left10 <= max(1, _cap10 // 4):
            _cap_color = C["warn"]
        else:
            _cap_color = None
        setl("caps", f"{_used10} / {_cap10}（时 {_used60} / {_cap60}）",
             _cap_color)
        setl("model", str(dig(cfg, "llm.model", "?")))
        setl("heartbeat", f"{int(hb)} 秒前" if hb is not None else "—")
        # 鱼的心：好感度 / 雷达图 / 念头（内部自带节流）
        try:
            self._render_heart()
        except Exception:
            pass

    def _render_log(self):
        """把日志文件**新增的部分**追加到窗口里。

        为什么按字节偏移而不是"比较行数"：
          原来写的是 `if len(lines) == self._log_lines: return`，但 `_log_lines`
          从头到尾没被赋值过，于是这个判断永远不成立 —— 每 1.5 秒就把同一段日志
          重新贴一遍，实测界面上同一段内容出现了三遍。
          而且 tail(400) 到了 400 行就饱和，靠行数也分不出"新增"和"没变"。
          改成记住读到的字节位置：文件变长了就读多出来的那截，被轮转截断了就重来。
        """
        try:
            size = LOG_PATH.stat().st_size
        except OSError:
            return
        # 文件被轮转/清空过（变小了）-> 从头来，并把窗口清掉避免新旧混在一起
        if size < self._log_pos:
            self._log_pos = 0
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
        if size == self._log_pos:
            return
        # 首次打开：只把最后一段拉进来，不要从文件开头读几个月的日志
        # 首次打开：只读最后一小段，不要从文件开头拉几个月的日志。
        # 64KB 在密集日志下能有 600+ 行、跨好几个小时 —— 用户一进来
        # 看到的是中午的记录，容易误以为"日志不动了"。
        # 24KB 大约 200 行，够看最近发生了什么。
        if self._log_pos == 0 and size > 24 * 1024:
            self._log_pos = size - 24 * 1024
        try:
            with LOG_PATH.open("rb") as f:
                f.seek(self._log_pos)
                data = f.read(256 * 1024)
            self._log_pos += len(data)
        except OSError:
            return
        text = data.decode("utf-8", "replace")
        if text:
            self.append_log(text.splitlines())
        # **强制滚到最底。**
        # 踩过（2026-10-28）：日志明明读到了最新内容（文件末尾是 18:26），
        # 窗口里却停在 12:06 —— 用户以为"日志不更新了"。
        # 原因是 see("end") 在插入几百行后不一定真把滚动条拉到底。
        # 这里再补一次 yview_moveto(1.0)，并且 after(50) 延后再来一次
        # （tkinter 插入大段文本后布局是异步完成的）。
        try:
            self.log.see("end")
            self.log.yview_moveto(1.0)
            self.log.after(60, lambda: self.log.yview_moveto(1.0))
        except Exception:
            pass

    def append_log(self, lines):
        if not lines:
            return
        self.log.configure(state="normal")
        first = self.log.index("end-1c")
        for ln in lines:
            tag = "info"
            if "⛔" in ln or "失败" in ln or "❌" in ln or "异常" in ln:
                tag = "err"
            elif "⚠️" in ln or "拦截" in ln or "冷却" in ln:
                tag = "warn"
            elif "✅" in ln or "已启用" in ln or "守护启动" in ln:
                tag = "ok"
            self.log.insert("end", ln + "\n", tag)
        # 本批新行统一打 "fresh" 标记：背景从品牌暗调淡回终端底色，
        # 眼睛能立刻定位"刚来的几行"。上一批的标记先撤掉，只亮最新的。
        self.log.tag_remove("fresh", "1.0", "end")
        self.log.tag_add("fresh", first, "end-1c")
        self.log.configure(state="disabled")
        if motion_ok():
            animate_tag_bg(self.log, "fresh",
                           mix(C["accent-lo"], C["term-bg"], 0.28),
                           C["term-bg"], 720)
        # **插入大段文本后必须先把布局刷完，再滚到底。**
        # 踩过（2026-10-28）：日志明明读到了 18:26 的内容，窗口却停在 12:06，
        # 用户以为"日志不更新"。原因是 see("end") 在批量 insert 之后
        # 布局还是异步的，滚动条停在插入前的位置。
        # 顺序：update_idletasks() -> see("end") -> yview_moveto(1.0)
        try:
            self.log.update_idletasks()
        except Exception:
            pass
        try:
            self.log.see("end")
            self.log.yview_moveto(1.0)
        except Exception:
            pass
        # tkinter 的文本控件在窗口被遮挡/最小化时布局更慢，再补一次
        try:
            self.log.after(80, self._scroll_log_bottom)
        except Exception:
            pass

    def _scroll_log_bottom(self):
        try:
            self.log.see("end")
            self.log.yview_moveto(1.0)
        except Exception:
            pass

    def _render_alert(self):
        """守护器连续失败时会写 ALERT.txt —— 在这里顶一条红横幅出来。

        为什么界面上也要有：弹窗可能在人不注意时被关掉，日志又没人主动翻。
        红横幅一直在，直到守护器恢复正常（它会自己删掉那个文件）。
        """
        try:
            txt = ALERT_PATH.read_text(encoding="utf-8")
        except OSError:
            txt = ""
        if not txt:
            if self._alert_shown:
                self.alert_bar.pack_forget()
                self._alert_shown = False
                self._alert_pulse(False)
            return
        # 只取前几行有用的：时间 / 连续失败 / 最近一次
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        pick = []
        for l in lines:
            if l.startswith(("时间", "连续失败", "最近一次")):
                pick.append(l)
        msg = "🚨 自动回复起不来了 —— " + " ｜ ".join(pick[:3])
        self.lbl_alert.configure(text=msg)
        if not self._alert_shown:
            # 放在标题栏下面、其它内容上面（与下面这些卡片同一个父级，可正确插入）
            self.alert_bar.pack(fill="x", padx=20, pady=(12, 0),
                                before=self._alert_anchor)
            self._alert_shown = True
            self._alert_pulse(True)

    def _alert_pulse(self, on: bool):
        """报警横幅的呼吸描边：出事期间边框在暗红底和警报红之间缓慢呼吸
        （1.6s 一个周期）。人在别的屏幕，余光也能注意到"问题还在"。
        横幅撤掉就停；不做任何弹跳/闪烁那种让人烦躁的强调。"""
        if getattr(self, "_alert_pulsing", None) == on:
            return
        self._alert_pulsing = on
        if not (on and motion_ok()):
            return

        def step():
            if not self._alert_shown:
                return
            ph = (time.time() % 1.6) / 1.6
            wave = (1 - math.cos(ph * 2 * math.pi)) / 2
            try:
                self.alert_bar.configure(
                    highlightbackground=mix(C["err-bg"], C["err"],
                                            0.30 + 0.70 * wave))
            except Exception:
                return
            self.after(110, step)
        step()

    # ---- 定时器 ----
    def _tick(self):
        try:
            self._render_alert()
            self._render_switch()      # 总开关状态要跟着 config 变
            if not self._busy:
                self._render_status()
                self._render_log()
                age = snapshot().get("log_age")
                self.foot("运行中" if age is not None and age < 120
                          else ("空闲" if age is not None else "尚未启动"))
        except Exception:
            pass
        self.after(1500, self._tick)


class ScrollPage(ttk.Frame):
    """可滚动的页面容器。

    加了"提示放字段下面"之后，字段占两行 —— 回复页 12 个字段就是 24 行，
    固定高度装不下。所以每个 Tab 都做成可滚动的，窗口再小也能翻到底。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=C["canvas"])
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, background=C["canvas"], padx=20, pady=16)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        # 滚轮只在鼠标进入这一页时生效，避免抢别的控件的滚轮
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self._bind_wheel(True))
            w.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfig(self._win, width=e.width)

    def _bind_wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass


# ────────────────────────────────────────────────────────────── 设置 ──

def _clean_notes(text: str) -> str:
    """把 Release 的 markdown 说明清理成好读的纯文本。

    Text 控件不渲染 markdown，直接显示 `## v1.4 / **加粗**` 很难看。
    """
    import re as _re
    s = (text or "").strip()
    if not s:
        return ""
    s = _re.sub(r"^#{1,6}\s*", "", s, flags=_re.M)
    s = s.replace("**", "").replace("`", "")
    s = _re.sub(r"^\s*[-*+]\s+", "· ", s, flags=_re.M)
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class UpdateDialog(tk.Toplevel):
    """发现新版本时的提示框：显示更新内容，让用户决定要不要更。"""

    def __init__(self, master, info: dict, notes: str, on_update=None):
        super().__init__(master)
        self.title("发现新版本")
        self.configure(bg=C["canvas"])
        self.resizable(False, False)
        # 不用 transient：master 若处于 withdraw 状态，transient 子窗口可能也不显示。
        # 改成显式 deiconify + lift，保证一定浮到最前面。
        self._on_update = on_update
        self._notes = _clean_notes(notes) or "（这个版本没有写更新说明）"

        W, H = 560, 460
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("{}x{}+{}+{}".format(
            W, H, (sw - W) // 2, (sh - H) // 2 - 60))

        # ── 顶部：版本 ──
        head = tk.Frame(self, bg=C["canvas"], padx=20, pady=16)
        head.pack(fill="x")
        tk.Label(head, text="发现新版本", bg=C["canvas"], fg=C["ink-subtle"],
                 font=(FONT, 9), anchor="w").pack(fill="x")
        row = tk.Frame(head, bg=C["canvas"])
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text="v" + VERSION, bg=C["canvas"], fg=C["ink-subtle"],
                 font=(FONT, 15)).pack(side="left")
        tk.Label(row, text="  →  ", bg=C["canvas"], fg=C["ink-subtle"],
                 font=(FONT, 15)).pack(side="left")
        tk.Label(row, text="v" + str(info.get("version", "?")),
                 bg=C["canvas"], fg=C["accent-hi"],
                 font=(FONT, 15, "bold")).pack(side="left")

        # ── 更新说明（可滚动） ──
        body = tk.Frame(self, bg=C["surface"],
                        highlightbackground=C["hairline"],
                        highlightthickness=1)
        body.pack(fill="both", expand=True, padx=20)
        tk.Label(body, text="更新了哪些功能", bg=C["surface"],
                 fg=C["ink-subtle"], font=(FONT, 9), anchor="w",
                 padx=14, pady=8).pack(fill="x")

        wrap = tk.Frame(body, bg=C["surface"])
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        sb = ttk.Scrollbar(wrap, orient="vertical")
        txt = tk.Text(wrap, wrap="word", font=(FONT, 9),
                      background=C["term-bg"], foreground=C["ink"],
                      relief="flat", bd=0, padx=12, pady=10,
                      insertbackground=C["ink"],
                      yscrollcommand=sb.set, height=14)
        sb.configure(command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", self._notes)
        txt.configure(state="disabled")

        # ── 按钮 ──
        bar = tk.Frame(self, bg=C["canvas"], padx=20, pady=14)
        bar.pack(fill="x")
        tk.Label(bar, text="更新只下载变化的文件，不动配置和记忆",
                 bg=C["canvas"], fg=C["ink-subtle"],
                 font=(FONT, 8)).pack(side="left")
        ttk.Button(bar, text="立即更新", style="Accent.TButton",
                   cursor="hand2", command=self._do_update).pack(side="right")
        ttk.Button(bar, text="稍后再说", style="TButton",
                   cursor="hand2", command=self.destroy).pack(
            side="right", padx=(0, 8))

        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(120, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:
            pass
        try:
            fade_window(self, 220)
        except Exception:
            pass

    def _do_update(self):
        self.destroy()
        if self._on_update:
            try:
                self._on_update()
            except Exception:
                pass


class Settings(tk.Toplevel):
    """把 config.json 里常用的项做成表单。

    只覆盖「日常会改的」—— 太细的（提示词权重、各种阈值）仍然手改 JSON，
    免得界面变成长长的参数表把人吓跑。
    """

    def __init__(self, master: App, tab: str = ""):
        super().__init__(master)
        self._initial_tab = tab
        self.title("设置")
        self.geometry("900x700")
        self.minsize(760, 560)
        self.configure(bg=C["canvas"])
        self.transient(master)
        self.cfg = load_cfg()
        self.vars: dict[str, tk.Variable] = {}
        self._rows: dict[int, int] = {}      # 每个页面各自的自动行号

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._tab_basic(nb)
        self._tab_reply(nb)
        self._tab_groups(nb)
        self._tab_schedule(nb)
        self._tab_abilities(nb)
        self._tab_persona(nb)
        # 表情包管理：**两个版本都要有**，它属于「怎么说话」的一部分，
        # 不是「额外功能」，所以不受 ui.extra_tabs 控制。
        self._tab_stickers(nb)
        # **扩展页：朋友圈 / 好友申请 / 转述。**
        # 便携版是「只回群聊和私聊」的精简版，config 里 ui.extra_tabs=false，
        # 这三页就不出现 —— 界面上完全看不到没有的功能。
        if self._on("ui.extra_tabs", True):
            self._tab_moments(nb)
            self._tab_friend_requests(nb)
            self._tab_relay(nb)
        # 更新页：两个版本都要有
        self._tab_update(nb)

        # 从「发现新版本」跳过来时，直接落到「更新」页
        try:
            if getattr(self, "_initial_tab", ""):
                for i in range(nb.index("end")):
                    if nb.tab(i, "text") == self._initial_tab:
                        nb.select(i)
                        break
        except Exception:
            pass

        bar = tk.Frame(self, bg=C["canvas"])
        bar.pack(fill="x", padx=16, pady=(4, 12))
        save = ttk.Button(bar, text="保存", style="Accent.TButton",
                          command=self.save, cursor="hand2")
        save.pack(side="right")
        ttk.Button(bar, text="取消", style="TButton", cursor="hand2",
                   command=self.destroy).pack(side="right", padx=(0, 10))
        tk.Label(bar, text="保存即生效（配置是热加载的，不用重启）",
                 bg=C["canvas"], fg=C["ink-subtle"], font=(FONT, 9)).pack(side="left")

        fade_window(self, 240)

    # ---- 小工具 ----
    def _next_row(self, parent) -> int:
        """下一个可用网格行（自动累加）。

        为什么不手写行号：`_row` 现在占两行（控件 + 下面的提示），
        手写的 row=7 分隔线就会**撞进字段中间** —— 实测「模型」下面插进来一条
        「提示：这一页改完点右下角保存」，然后「温度」才接着排，读起来全乱。
        统一走这个计数器就不会错位。
        """
        r = self._rows.get(id(parent), 0)
        self._rows[id(parent)] = r + 1
        return r * 2

    def _row(self, parent, r, label, kind="entry", path="", **kw):
        """一个字段：标签 + 控件一行，提示文字在**控件下面**。

        提示原来放在右边第三列，实测窗口稍窄就被切掉（连「保存」按钮都被挤出屏幕）。
        放下面就不受窗口宽度影响。`r` 参数保留只为调用处少改，实际用自动行号。
        """
        gr = self._next_row(parent)
        ttk.Label(parent, text=label, style="TLabel").grid(
            row=gr, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        cur = dig(self.cfg, path, kw.get("default"))
        if kind == "entry":
            # 列表用「逗号分隔」显示，不要 str(list)
            v = tk.StringVar(value=list_to_text(cur))
            w = ttk.Entry(parent, textvariable=v, width=52)
        elif kind == "check":
            v = tk.BooleanVar(value=bool(cur))
            w = ttk.Checkbutton(parent, variable=v, text="启用")
        elif kind == "combo":
            v = tk.StringVar(value=str(cur))
            w = ttk.Combobox(parent, textvariable=v, values=kw.get("values", []),
                             state="readonly", width=49)
        elif kind == "spin":
            v = tk.StringVar(value=list_to_text(cur))
            w = ttk.Spinbox(parent, textvariable=v, from_=kw.get("lo", 0),
                            to=kw.get("hi", 999), width=50)
        elif kind == "label":
            # 只读文字（版本号、更新源这类不需要编辑的）
            v = tk.StringVar(value=str(cur))
            w = tk.Label(parent, textvariable=v, background=C["canvas"],
                         foreground=C["ink"], font=(FONT, 9), anchor="w")
        else:
            raise ValueError(kind)
        w.grid(row=gr, column=1, sticky="w", pady=(8, 0))
        if kw.get("hint"):
            tk.Label(parent, text=kw["hint"], background=C["canvas"],
                     foreground=C["ink-subtle"], font=(FONT, 8), wraplength=620,
                     justify="left").grid(row=gr + 1, column=1, sticky="w",
                                          pady=(3, 2))
        self.vars[path] = v
        return v

    def _on(self, path: str, default: bool = True) -> bool:
        """读一个开关值，用来决定某个设置项要不要显示。

        为什么要有这个：精简版（便携版）关掉了晚安/早上好、余额提醒等
        额外功能，但设置页如果照样显示这些字段，用户会以为还能用、填了没反应。
        关掉的功能就该在界面上**完全不存在**。

        用配置判断而不是写死：同一份 gui.py，
          开发版 config 没有 night.talk_enabled  -> 默认 True -> 照常显示
          便携版 config night.talk_enabled=False -> 隐藏
        """
        cur = self.cfg
        for part in path.split("."):
            if not isinstance(cur, dict):
                return default
            if part not in cur:
                return default
            cur = cur[part]
        return bool(cur)

    def _sep(self, parent):
        r = self._next_row(parent)
        ttk.Separator(parent, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=14)

    def _note(self, parent, text):
        r = self._next_row(parent)
        tk.Label(parent, text=text, background=C["canvas"],
                 foreground=C["ink-subtle"], justify="left",
                 wraplength=700, font=(FONT, 8)).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _page(self, nb, title):
        page = ScrollPage(nb)
        nb.add(page, text=title)
        f = page.inner
        f.columnconfigure(1, weight=1)
        return f

    # ---- 各页 ----
    def _tab_basic(self, nb):
        f = self._page(nb, "基础")
        self._row(f, 0, "总开关（enabled）", "check", "enabled",
                  hint="关掉后它不在群里说任何话")
        self._row(f, 1, "演习模式（dry_run）", "check", "safety.dry_run",
                  hint="只判断不发送，用来试跑")
        self._row(f, 2, "API Key", "entry", "llm.api_key",
                  hint="必填。sk- 开头的那串。便携版没有别的来源，留空就跑不了")
        # ── 模型预设：选一个就自动填好接口地址和模型名 ──
        cur_url = str(dig(self.cfg, "llm.base_url", "") or "")
        preset_name = "自定义（自己填）"
        for pname, pv in MODEL_PRESETS.items():
            if pv["base_url"] and pv["base_url"].rstrip("/") == cur_url.rstrip("/"):
                preset_name = pname
                break
        self._combo_row(f, "模型预设", preset_name,
                        list(MODEL_PRESETS.keys()),
                        hint="选一个自动填下面的接口地址和模型；"
                             "想用别的选「自定义」")
        self._row(f, 3, "接口地址", "entry", "llm.base_url")
        self._row(f, 4, "模型", "entry", "llm.model")
        self._row(f, 5, "温度", "entry", "llm.temperature")
        self._row(f, 6, "单次最多字", "entry", "llm.max_tokens")
        self._sep(f)

        # ── 微信账号：普通人不知道 wxid，给个按钮自己识别 ──
        self._wxid_row(f)

        self._note(f, "提示：这一页改完点右下角「保存」，守护进程下一轮就生效。")
        tk.Label(f, text="提示：这一页改完点右下角「保存」，守护进程下一轮就生效。",
                 background=C["canvas"], foreground=C["ink-subtle"],
                 wraplength=700).grid(
            row=self._next_row(f), column=0, columnspan=3,
            sticky="w", pady=(16, 0))

    def _combo_row(self, parent, label, cur_value, values, path="", hint=""):
        """一个「下拉 + 选中后回调」的行。用于模型预设这种「选了就自动填」的字段。"""
        gr = self._next_row(parent)
        ttk.Label(parent, text=label).grid(row=gr, column=0, sticky="w",
                                           padx=(0, 10), pady=(6, 0))
        var = tk.StringVar(value=cur_value)
        cb = ttk.Combobox(parent, textvariable=var, values=values,
                          state="readonly", width=49)
        cb.grid(row=gr, column=1, sticky="w", pady=(6, 0))
        cb.bind("<<ComboboxSelected>>",
                lambda _e: self._on_preset(var.get()))
        if hint:
            ttk.Label(parent, text=hint, foreground=C["ink-subtle"],
                      font=(FONT, 8), wraplength=620,
                      justify="left").grid(row=gr + 1, column=1, sticky="w")
        return var

    def _on_preset(self, name):
        """选了预设 -> 把接口地址和模型名填进对应输入框。"""
        pv = MODEL_PRESETS.get(name)
        if not pv:
            return
        for path, key in (("llm.base_url", "base_url"), ("llm.model", "model")):
            var = self.vars.get(path)
            if var is None:
                continue
            try:
                var.set(pv[key] or "")
            except Exception:
                pass
        # 顺手把 provider 也记进去（后面余额查询要用它判断）
        self._provider_override = pv.get("provider") or "custom"
        if pv.get("note"):
            try:
                self.foot(pv["note"])
            except Exception:
                pass

    def _wxid_row(self, parent):
        """「只用这个微信账号」+ 一个自动识别按钮。

        为什么要有按钮：这个字段要填 wxid（wxid_xxx 那种），
        普通人根本不知道自己的微信 id 是什么，还得去翻数据库。
        点一下直接读当前登录的微信，填进来。
        """
        gr = self._next_row(parent)
        ttk.Label(parent, text="只用这个微信账号").grid(
            row=gr, column=0, sticky="w", padx=(0, 10), pady=(6, 0))

        holder = ttk.Frame(parent)
        holder.grid(row=gr, column=1, sticky="w", pady=(6, 0))

        cur = dig(self.cfg, "safety.allowed_accounts", [])
        var = tk.StringVar(value=list_to_text(cur))
        entry = ttk.Entry(holder, textvariable=var, width=38)
        entry.pack(side="left")

        btn = ttk.Button(holder, text="识别当前微信", style="TButton")
        btn.pack(side="left", padx=(8, 0))

        status = ttk.Label(parent, text="", foreground=C["ok"], font=(FONT, 8))
        status.grid(row=gr + 1, column=1, sticky="w")

        def _detect():
            btn.configure(state="disabled")
            status.configure(text="正在读…", foreground=C["ink-subtle"])
            try:
                wxid, nick, err = detect_current_wxid()
            except Exception as e:
                wxid, nick, err = "", "", "{}: {}".format(type(e).__name__, e)
            btn.configure(state="normal")
            if wxid:
                var.set(wxid)
                self._wxid_detected = wxid
                status.configure(
                    text="已识别：{}  点「保存」生效".format(nick or wxid),
                    foreground=C["ok"])
            else:
                status.configure(text="识别失败：{}".format(err),
                                 foreground=C["err"])
                try:
                    messagebox.showwarning(
                        APP_NAME,
                        "没能识别当前微信账号。\n\n{}".format(err))
                except Exception:
                    pass

        btn.configure(command=_detect)
        self.vars["safety.allowed_accounts"] = var
        ttk.Label(parent, text="逗号分隔；留空=不限制。不知道填什么就点右边的按钮。",
                  foreground=C["ink-subtle"], font=(FONT, 8),
                  wraplength=620, justify="left").grid(
            row=gr + 2, column=1, sticky="w")

    def _tab_reply(self, nb):
        f = self._page(nb, "回复")
        self._row(f, 0, "接话模式", "combo", "participation.mode",
                  values=["addressed_or_relevant", "addressed_only", "active", "silent"],
                  hint="点名+相关 / 只接点名 / 主动插话 / 只旁观")
        self._row(f, 1, "话痨程度", "combo", "participation.talkativeness",
                  values=["quiet", "normal", "chatty"])
        self._row(f, 2, "普通消息判断范围", "combo",
                  "participation.consider_plain_messages",
                  values=["off", "questions", "all"],
                  hint="off 只看点名 / questions 省 / all 每批都判断")
        self._row(f, 3, "冷却（秒）", "spin", "participation.cooldown_s",
                  lo=0, hi=600, hint="两条消息之间至少隔多久")
        self._row(f, 4, "静默窗口（秒）", "spin", "participation.quiet_window_s",
                  lo=0, hi=60, hint="群里安静几秒后才判断（防止抢答）")
        self._row(f, 5, "10 分钟上限", "spin",
                  "participation.max_replies_per_10min", lo=1, hi=99)
        self._row(f, 6, "每小时上限", "spin",
                  "participation.max_replies_per_hour", lo=1, hi=999)
        self._row(f, 7, "置信度阈值", "entry", "participation.min_confidence",
                  hint="低于它就不说（被点名不受限）")
        self._row(f, 8, "回看多少条历史", "spin", "participation.context_messages",
                  lo=5, hi=200, hint="越大越记得住上下文，也越费钱")
        self._row(f, 9, "被 @ 时穿透冷却", "check",
                  "participation.mention_bypasses_cooldown")
        self._row(f, 10, "别人被 @ 时不插嘴", "check",
                  "participation.skip_when_others_addressed")
        self._row(f, 11, "触发词", "entry", "participation.trigger_words",
                  hint="逗号分隔；提到这些词等于在叫它")

    def _tab_groups(self, nb):
        f = self._page(nb, "群")
        tk.Label(f, text="勾选要自动回复的群。没列出来的群默认不回复。",
                 background=C["canvas"], foreground=C["ink-subtle"]).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.group_vars: dict[str, tk.BooleanVar] = {}
        groups = self.cfg.get("groups") or {}
        r = 1
        for key, v in groups.items():
            if key == "*" or not isinstance(v, dict):
                continue
            var = tk.BooleanVar(value=bool(v.get("enabled")))
            ttk.Checkbutton(f, variable=var,
                            text=f"{v.get('alias') or key}   ({key})").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=3)
            self.group_vars[key] = var
            r += 1
        if r == 1:
            tk.Label(f, text="（配置里还没有群。先跑一次守护进程，它会自动发现群。）",
                     background=C["canvas"], foreground=C["ink-subtle"]).grid(
                row=1, column=0, sticky="w")
        tk.Label(f, text="没在这里列出的群：", background=C["canvas"],
                 foreground=C["ink-subtle"]).grid(
            row=r + 1, column=0, sticky="w", pady=(18, 4))
        self.var_star = tk.BooleanVar(value=bool((groups.get("*") or {}).get("enabled")))
        ttk.Checkbutton(f, variable=self.var_star,
                        text="默认回复所有群（* 通配，慎用）").grid(
            row=r + 2, column=0, sticky="w")

    def _tab_schedule(self, nb):
        f = self._page(nb, "作息")
        self._row(f, 0, "夜间模式", "check", "night.enabled",
                  hint="这段时间完全不回消息")
        self._row(f, 1, "入睡时间", "entry", "night.start", hint="格式 23:00")
        self._row(f, 2, "起床时间", "entry", "night.end", hint="格式 06:30")
        # 晚安 / 早上好是"主动发言"，精简版关掉后就不该出现在设置里。
        if self._on("night.talk_enabled", True):
            self._row(f, 3, "晚安时间", "entry", "night.goodnight_at",
                      hint="到点且群里有人说话才道晚安")
            self._row(f, 4, "早上好截止", "entry", "night.greeting_until",
                      hint="起床后到这个点之前，每群发一次早上好")
        self._sep(f)
        # 余额提醒也是主动私聊，关掉就不显示。
        if self._on("alert.enabled", True):
            self._row(f, 6, "余额不足提醒", "check", "alert.enabled")
            self._row(f, 7, "@ 谁", "entry", "alert.at")
            self._row(f, 8, "提醒内容", "entry", "alert.text")
            self._sep(f)
        self._row(f, 10, "反复纠缠就收场", "check", "tease.enabled")
        self._row(f, 11, "窗口（秒）", "spin", "tease.window_s", lo=30, hi=3600)
        self._row(f, 12, "逗满几次", "spin", "tease.count", lo=1, hi=20)
        self._row(f, 13, "冷处理（秒）", "spin", "tease.ignore_s", lo=30, hi=7200)
        self._row(f, 14, "收场方式", "combo", "tease.prefer",
                  values=["either", "concede", "cold"],
                  hint="自选 / 妥协 / 冷收场")
        self._row(f, 15, "豁免名单", "entry", "tease.exempt",
                  hint="逗号分隔；这些人逗它不会被冷处理")

    def _tab_abilities(self, nb):
        f = self._page(nb, "能力")
        self._row(f, 0, "发表情包", "check", "abilities.sticker.enabled")
        self._row(f, 1, "表情包每小时上限", "spin",
                  "abilities.sticker.max_per_hour", lo=0, hi=60)
        self._row(f, 2, "表情包冷却（秒）", "spin",
                  "abilities.sticker.cooldown_s", lo=0, hi=3600)
        self._row(f, 3, "发微信自带表情", "check", "abilities.emoji.enabled",
                  hint="关掉后回复里不会出现 [捂脸] 这类")
        self._sep(f)
        self._row(f, 5, "拍一拍", "check", "abilities.poke.enabled")
        self._row(f, 6, "拍一拍每小时上限", "spin",
                  "abilities.poke.max_per_hour", lo=0, hi=30)
        self._sep(f)
        self._row(f, 8, "要求屏幕可截图", "check", "safety.require_screen",
                  hint="锁屏时自动暂停（截图失败会卡死发送）")
        self._row(f, 9, "启动时清理数据库缓存", "check",
                  "safety.clean_db_cache_on_start",
                  hint="缓存堆积会导致启动死锁，建议保持开启")
        self._note(f, "表情包文件放在程序目录的 stickers/ 里，"
                      "并在 stickers.json 里给每张写 desc（说明什么时候用）。")

    def _tab_persona(self, nb):
        f = self._page(nb, "人设")
        self._row(f, 0, "名字", "entry", "persona.name")
        self._row(f, 1, "身份", "entry", "persona.identity")
        self._row(f, 2, "语气", "entry", "persona.tone")
        self._row(f, 3, "单条最多字", "spin", "persona.max_chars", lo=4, hi=200)

        # ── 打开人格文件 ──
        # 人格卡正文写了几千字（习惯、禁忌、说话方式、生日…），
        # 塞进表单不现实，只能直接编辑文件。给个按钮省得用户去翻目录。
        bar = tk.Frame(f, bg=C["canvas"])
        bar.grid(row=self._next_row(f), column=0, columnspan=3,
                 sticky="w", pady=(16, 0))
        ttk.Button(bar, text="打开人格文件（persona.md）",
                   style="Accent.TButton", cursor="hand2",
                   command=self._open_persona_file).pack(side="left")
        ttk.Button(bar, text="打开 prompts 文件夹", style="TButton",
                   cursor="hand2",
                   command=lambda: self.master.open_path(ROOT / "prompts")
                   ).pack(side="left", padx=(8, 0))

        self._note(f, "人格卡正文（prompts/persona.md）内容更多 —— 习惯、禁忌、"
                      "说话方式、生日都在里面。点上面的按钮打开改，"
                      "存盘即生效，不用重启。")

    def _open_persona_file(self):
        """打开 prompts/persona.md。"""
        p = ROOT / "prompts" / "persona.md"
        if not p.exists():
            # 目录在、文件没了 —— 顺手建个空的，免得用户一脸问号
            try:
                p.parent.mkdir(exist_ok=True)
                p.write_text("", encoding="utf-8")
            except Exception as e:
                messagebox.showerror(APP_NAME, "找不到也建不了：{}".format(e))
                return
        self.master.open_path(p)


    # ---- 更新 ----
    def _edition(self) -> str:
        """当前是哪个版本（决定去哪个仓库查更新）。"""
        return str(dig(self.cfg, "ui.edition", "") or
                   dig(self.cfg, "update.edition", "") or "dev")

    def _tab_update(self, nb):
        f = self._page(nb, "更新")

        self._row(f, 0, "当前版本", "label", "", default="v" + VERSION)
        repo = ""
        try:
            repo = _UPD.repo_of(self.cfg, self._edition()) if _UPD else ""
        except Exception:
            repo = ""
        self._row(f, 1, "更新源", "label", "",
                  default=repo or "（还没配置 GitHub 仓库）")
        self._row(f, 2, "版本类型", "label", "", default=self._edition())

        bar = tk.Frame(f, bg=C["canvas"])
        bar.grid(row=self._next_row(f), column=0, columnspan=3,
                 sticky="w", pady=(18, 0))
        self.btn_check_upd = ttk.Button(
            bar, text="检查更新", style="TButton", cursor="hand2",
            command=self._check_update)
        self.btn_check_upd.pack(side="left")
        self.btn_do_upd = ttk.Button(
            bar, text="立即更新", style="Accent.TButton", cursor="hand2",
            state="disabled", command=self._do_update)
        self.btn_do_upd.pack(side="left", padx=(10, 0))

        self.lbl_upd = tk.Label(
            f, text="点「检查更新」看看有没有新版本",
            bg=C["canvas"], fg=C["ink-subtle"], justify="left",
            wraplength=680, font=(FONT, 9), anchor="w")
        self.lbl_upd.grid(row=self._next_row(f), column=0, columnspan=3,
                          sticky="w", pady=(14, 0))

        self.prog_upd = ttk.Progressbar(f, mode="determinate",
                                        length=420, maximum=100)
        self.prog_upd.grid(row=self._next_row(f), column=0, columnspan=3,
                           sticky="w", pady=(8, 0))

        self.txt_upd_notes = tk.Text(
            f, height=7, width=72, wrap="word", font=(FONT, 9),
            background=C["surface-2"], foreground=C["ink"],
            relief="flat", bd=0, padx=10, pady=8)
        self.txt_upd_notes.grid(row=self._next_row(f), column=0, columnspan=3,
                                sticky="we", pady=(10, 0))
        self.txt_upd_notes.insert("1.0", "（这里显示新版本的更新说明）")
        self.txt_upd_notes.configure(state="disabled")

        self._note(
            f,
            "更新只下载变化的文件（按 sha256 对比），不会整个重下；"
            "本地多出来的程序文件会被删掉。\n"
            "config.json / 记忆 / 日志 / 人格卡这类私人内容都不会被动。"
            "更新完需要重启程序。")

        self._upd_result = None

    def _check_update(self):
        if _UPD is None:
            self._upd_set("更新模块没加载（updater.py 缺失）", err=True)
            return
        self.btn_check_upd.configure(state="disabled")
        self._upd_set("正在检查…")
        cfg = load_cfg()
        edition = self._edition()

        def work():
            try:
                res = _UPD.check_latest(cfg, edition)
                self.after(0, lambda: self._upd_checked(res, None))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self._upd_checked(None, msg))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _upd_checked(self, res, err):
        self.btn_check_upd.configure(state="normal")
        if err:
            self._upd_set("检查失败：" + err, err=True)
            return
        if not res:
            self._upd_set("已经是最新版本（v{}）".format(VERSION))
            self.btn_do_upd.configure(state="disabled")
            return
        self._upd_result = res
        self._upd_set("发现新版本：v{}  →  v{}".format(VERSION, res["version"]))
        self.btn_do_upd.configure(state="normal")
        body = "（取更新说明失败）"
        try:
            # 走 updater 的国内加速源，别直连 api.github.com
            rel = _UPD._get_json_with_sources(
                _UPD.API_TAG_SOURCES,
                {"repo": res["repo"], "tag": res["tag"]})
            body = (rel.get("body") or "").strip() or "（这次没有写说明）"
        except Exception:
            pass
        self.txt_upd_notes.configure(state="normal")
        self.txt_upd_notes.delete("1.0", "end")
        self.txt_upd_notes.insert("1.0", body)
        self.txt_upd_notes.configure(state="disabled")

    def _do_update(self):
        if not self._upd_result or _UPD is None:
            return
        res = self._upd_result
        if not messagebox.askyesno(
                APP_NAME,
                "确认更新到 v{}？\n\n只会下载变化的文件，"
                "更新完需要重启程序。".format(res["version"])):
            return
        self.btn_do_upd.configure(state="disabled")
        self.btn_check_upd.configure(state="disabled")
        self._upd_set("正在准备…")
        edition = self._edition()

        def work():
            try:
                manifest = _UPD.fetch_manifest(
                        res["manifest_url"], res["repo"],
                        res.get("commit") or res["tag"])
                plan = _UPD.plan(ROOT, manifest, edition)
                total = len(plan["to_download"])
                self.after(0, lambda: self._upd_set(
                    "需要下载 {} 个文件，删除 {} 个".format(
                        total, len(plan["to_delete"]))))
                self.after(0, lambda: self.prog_upd.configure(
                    maximum=max(1, total), value=0))

                def prog(i, n2, rel):
                    self.after(0, lambda: self.prog_upd.configure(value=i))
                    self.after(0, lambda: self._upd_set(
                        "下载中 {}/{}：{}".format(i, n2, rel)))

                stat = _UPD.apply_update(
                    ROOT, res["repo"],
                    res.get("commit") or res["tag"],
                    plan, progress=prog)
                self.after(0, lambda: self._upd_done(res, stat))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self._upd_set("更新失败：" + msg, err=True))
                self.after(0, lambda: self.btn_check_upd.configure(state="normal"))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _upd_done(self, res, stat):
        self.prog_upd.configure(value=self.prog_upd.cget("maximum"))
        ok = len(stat["downloaded"])
        if stat["failed"]:
            self._upd_set("部分失败：成功 {}，失败 {}".format(
                ok, len(stat["failed"])), err=True)
            return
        self._upd_set("更新完成：下载 {} 个，删除 {} 个。请重启程序。".format(
            ok, len(stat["deleted"])))
        try:
            cfg = load_cfg()
            cfg["version"] = res["version"]
            save_cfg(cfg)
        except Exception:
            pass
        messagebox.showinfo(
            APP_NAME,
            "已更新到 v{}\n\n下载 {} 个文件，删除 {} 个。\n\n"
            "请关闭程序后重新打开。".format(
                res["version"], ok, len(stat["deleted"])))

    def _upd_set(self, text, err=False):
        try:
            self.lbl_upd.configure(
                text=text, fg=(C["err"] if err else C["ink-subtle"]))
        except Exception:
            pass


    # ---- 表情包管理 ----
    def _tab_stickers(self, nb):
        """表情包管理页：增删、改说明、改用途、改频率。

        为什么要有：表情包到底是什么意思、什么时候该发，只有用户自己知道。
        之前只能改 stickers.json，普通人不会碰 JSON。
        """
        page = self._page(nb, "表情包")
        self._sticker_vars = {}          # sid -> dict(vars)
        self._sticker_frames = {}        # sid -> 卡片 Frame

        # ── 顶部说明 + 添加按钮 ──
        top = ttk.Frame(page)
        top.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(top, text="标记好「说明」和「什么时候发」，她才会在合适的时候用。"
                           "留空说明 = 不会被选中。",
                  foreground=C["ink-subtle"], font=(FONT, 8),
                  wraplength=520).pack(side="left")
        ttk.Button(top, text="＋ 从文件添加表情", style="Accent.TButton",
                   cursor="hand2",
                   command=self._add_sticker_file).pack(side="right")

        # ── 表情卡片容器 ──
        self._sticker_holder = ttk.Frame(page)
        self._sticker_holder.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._sticker_holder.columnconfigure(0, weight=1)

        self._render_sticker_cards()

    def _render_sticker_cards(self):
        """按 stickers.json 重建所有表情卡片。"""
        for w in list(self._sticker_holder.winfo_children()):
            w.destroy()
        self._sticker_vars.clear()
        self._sticker_frames.clear()

        reg = load_stickers()
        if not reg:
            ttk.Label(self._sticker_holder,
                      text="还没有表情包。点右上角「从文件添加表情」。",
                      foreground=C["ink-subtle"]).grid(row=0, column=0, sticky="w")
            return

        for i, sid in enumerate(sorted(reg)):
            v = reg[sid]
            self._sticker_card(self._sticker_holder, i, sid, v)

    def _sticker_card(self, parent, row, sid, v):
        """一张表情卡片：缩略图 + 说明 + 用途 + 频率 + 删除。"""
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        # ── 缩略图 ──
        thumb = tk.Label(card, bg=C["surface"], width=90, height=90)
        thumb.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 12))
        self._load_thumb(thumb, v.get("file") or "")

        # ── 第一行：编号 + 文件名 + 启用 + 删除 ──
        head = ttk.Frame(card, style="Card.TFrame")
        head.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(head, text=sid, background=C["surface"],
                  foreground=C["accent"], font=(FONT, 11, "bold")).pack(side="left")
        ttk.Label(head, text=(v.get("file") or "").split("/")[-1],
                  background=C["surface"], foreground=C["ink-subtle"],
                  font=(FONT, 8)).pack(side="left", padx=(8, 0))

        enabled_var = tk.BooleanVar(value=not bool(v.get("disabled")))
        ttk.Checkbutton(head, text="启用", variable=enabled_var).pack(side="right")
        ttk.Button(head, text="删除", style="Ghost.TButton", cursor="hand2",
                   command=lambda s=sid: self._delete_sticker(s)).pack(
            side="right", padx=(0, 8))

        # ── 说明 ──
        f2 = ttk.Frame(card, style="Card.TFrame")
        f2.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(f2, text="说明", width=8, background=C["surface"]).pack(side="left")
        desc_var = tk.StringVar(value=v.get("desc") or "")
        ttk.Entry(f2, textvariable=desc_var).pack(side="left", fill="x", expand=True)

        # ── 什么时候发 ──
        f3 = ttk.Frame(card, style="Card.TFrame")
        f3.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(f3, text="什么时候发", width=8, background=C["surface"]).pack(side="left")
        when_var = tk.StringVar(value=v.get("when") or "")
        ttk.Entry(f3, textvariable=when_var).pack(side="left", fill="x", expand=True)

        # ── 频率 ──
        f4 = ttk.Frame(card, style="Card.TFrame")
        f4.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(f4, text="最短间隔", width=8, background=C["surface"]).pack(side="left")
        mins = int(float(v.get("min_interval_s") or 0)) // 60
        interval_var = tk.StringVar(value=str(mins))
        ttk.Spinbox(f4, textvariable=interval_var, from_=0, to=1440,
                    width=8).pack(side="left")
        ttk.Label(f4, text="分钟（0 = 只受全局限制）",
                  foreground=C["ink-subtle"], font=(FONT, 8),
                  background=C["surface"]).pack(side="left", padx=(6, 0))

        self._sticker_vars[sid] = {
            "desc": desc_var, "when": when_var,
            "enabled": enabled_var, "interval": interval_var,
            "file": v.get("file") or "",
        }
        self._sticker_frames[sid] = card

    def _load_thumb(self, label, rel):
        """把图片缩略图放进 Label。GIF 取第一帧。"""
        try:
            from PIL import Image, ImageTk
        except Exception:
            label.configure(text="(无 PIL)", fg=C["ink-subtle"], font=(FONT, 7))
            return
        p = ROOT / rel if rel else None
        if not p or not p.exists():
            label.configure(text="(文件缺失)", fg=C["err"], font=(FONT, 7))
            return
        try:
            im = Image.open(str(p))
            im = im.convert("RGB")
            im.thumbnail((90, 90))
            photo = ImageTk.PhotoImage(im)
            label.configure(image=photo)
            label.image = photo      # 必须留住引用，否则被回收
        except Exception:
            label.configure(text="(读图失败)", fg=C["err"], font=(FONT, 7))

    def _add_sticker_file(self):
        """从文件添加表情：复制到 stickers/ 并登记。"""
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="选择表情包图片（可多选）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.webp"),
                       ("所有文件", "*.*")])
        if not paths:
            return
        reg = load_stickers()
        stick_dir = ROOT / "stickers"
        stick_dir.mkdir(exist_ok=True)
        added = 0
        for src in paths:
            sp = pathlib.Path(src)
            if not sp.exists():
                continue
            # 生成下一个编号
            n = 1
            while "s{:02d}".format(n) in reg:
                n += 1
            sid = "s{:02d}".format(n)
            dst_name = sid + sp.suffix.lower()
            try:
                shutil.copy2(str(sp), str(stick_dir / dst_name))
            except Exception as e:
                messagebox.showerror(APP_NAME, "复制失败：{}".format(e))
                return
            reg[sid] = {"file": "stickers/" + dst_name,
                        "desc": "", "when": ""}
            added += 1
        if added:
            save_stickers(reg)
            self._render_sticker_cards()
            self.foot("已添加 {} 张表情，填好「说明」和「什么时候发」再保存".format(added))

    def _delete_sticker(self, sid):
        """从登记表删掉（图片文件保留，以后能再加回来）。"""
        if not messagebox.askyesno(APP_NAME,
                                   "从列表里移除 {}？\n图片文件留在 stickers/ 里不会删。".format(sid)):
            return
        reg = load_stickers()
        reg.pop(sid, None)
        save_stickers(reg)
        self._render_sticker_cards()
        self.foot("已移除 {}".format(sid))

    # ---- 扩展页：朋友圈 ----
    def _tab_moments(self, nb):
        f = self._page(nb, "朋友圈")
        self._row(f, 0, "看朋友圈", "check", "moments.enabled",
                  hint="关掉后完全不看、不点赞、不评论")
        self._row(f, 1, "每天几点看", "entry", "moments.schedule",
                  hint="逗号分隔，如 06:30, 12:00, 21:00")
        self._row(f, 2, "每次最多往下翻几条", "spin", "moments.read_limit",
                  lo=5, hi=200)
        self._row(f, 3, "每次最多互动几条", "spin", "moments.max_per_run",
                  lo=0, hi=50)
        self._row(f, 4, "允许点赞", "check", "moments.allow_like")
        self._row(f, 5, "允许评论", "check", "moments.allow_comment")
        self._row(f, 6, "每次最多评论几条", "spin",
                  "moments.max_comments_per_run", lo=0, hi=10)
        self._row(f, 7, "看图片内容", "check", "moments.read_images",
                  hint="关掉只看文字，省钱但会漏掉图里的意思")
        self._sep(f)
        self._row(f, 9, "两次之间至少隔（秒）", "spin",
                  "moments.min_interval_s", lo=60, hi=86400)
        self._note(f, "想临时让她看一次：私聊里说「去看朋友圈」或「看看XX的朋友圈」。")

    # ---- 扩展页：好友申请 ----
    def _tab_friend_requests(self, nb):
        f = self._page(nb, "好友申请")
        self._row(f, 0, "自动处理好友申请", "check", "friend_requests.enabled",
                  hint="关掉后完全不管，申请会一直堆着")
        self._row(f, 1, "自动同意", "check", "friend_requests.auto_accept",
                  hint="关掉只看看不点同意")
        self._row(f, 2, "每天最多同意几个", "spin",
                  "friend_requests.max_per_day", lo=0, hi=100)
        self._row(f, 3, "半夜也处理", "check", "friend_requests.allow_night")
        self._row(f, 4, "屏蔽关键词", "entry", "friend_requests.block_keywords",
                  hint="逗号分隔；备注里带这些词的一律不同意")
        self._sep(f)
        self._note(f, "想手动查一次：私聊里说「看看有没有好友申请」。")

    # ---- 扩展页：转述 ----
    def _tab_relay(self, nb):
        f = self._page(nb, "转述")
        self._row(f, 0, "开启转述", "check", "relay.enabled",
                  hint="主人说「帮我把这段话发给 X」，她替你发到那个应用")
        self._row(f, 1, "只认这个微信", "entry", "relay.owner_wxid",
                  hint="必须是主人的 wxid；别人发同样的字不生效")
        self._row(f, 2, "发完按回车", "check", "relay.press_enter")
        self._row(f, 3, "等回复超时（秒）", "spin",
                  "relay.reply_timeout_s", lo=30, hi=3600)
        self._note(f, "转述会把目标应用窗口切到前台，期间别抢鼠标键盘。")

    def _save_stickers_from_ui(self):
        """把界面上的表情包设置写回 stickers.json。"""
        if not getattr(self, "_sticker_vars", None):
            return
        reg = load_stickers()
        for sid, vs in self._sticker_vars.items():
            if sid not in reg:
                continue
            item = reg[sid]
            item["desc"] = vs["desc"].get().strip()
            item["when"] = vs["when"].get().strip()
            if not vs["enabled"].get():
                item["disabled"] = True
            else:
                item.pop("disabled", None)
            try:
                mins = int(float(vs["interval"].get().strip() or 0))
            except ValueError:
                mins = 0
            if mins > 0:
                item["min_interval_s"] = mins * 60
            else:
                item.pop("min_interval_s", None)
        save_stickers(reg)

    # ---- 保存 ----
    def save(self):
        cfg = load_cfg()
        for path, var in self.vars.items():
            cur = dig(cfg, path)
            val = var.get()
            if isinstance(cur, bool) or isinstance(var, tk.BooleanVar):
                v = bool(val)
            elif isinstance(cur, int):
                try:
                    v = int(float(str(val).strip()))
                except ValueError:
                    v = cur
            elif isinstance(cur, float):
                try:
                    v = float(str(val).strip())
                except ValueError:
                    v = cur
            elif isinstance(cur, list):
                # 逗号分隔 -> 列表（会剥掉历史遗留的方括号/引号）
                v = text_to_list(str(val))
                if not v:
                    v = cur          # 清空时保持原值，别把必填项写成 []
            else:
                v = val
            bury(cfg, path, v)

        # 群勾选
        if hasattr(self, "group_vars"):
            for key, var in self.group_vars.items():
                if isinstance(cfg.get("groups", {}).get(key), dict):
                    cfg["groups"][key]["enabled"] = bool(var.get())
            if isinstance(cfg.get("groups", {}).get("*"), dict):
                cfg["groups"]["*"]["enabled"] = bool(getattr(self, "var_star").get())

        try:
            save_cfg(cfg)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败：{e}")
            return

        # **表情包单独存 stickers.json。**
        # 它不是 config 的一部分，但要和 config 一起保存 ——
        # 用户点一次「保存」就都生效，不用再跑命令行导入。
        try:
            self._save_stickers_from_ui()
        except Exception as e:
            messagebox.showwarning(APP_NAME, "表情包保存失败：{}".format(e))
        self.master.cfg = cfg
        self.master.refresh_now()
        self.master.foot("设置已保存 —— 守护进程下一轮自动生效")
        self.destroy()


def main():
    """启动流程：先显示开启动画（检查更新 + 资源完整性），完成后进主界面。"""
    app = App(skip_start_check=True, defer_anim=True)
    app.withdraw()

    try:
        import splash as _SPLASH

        def _on_splash_done():
            try:
                # 先放开标志，再显示主窗口，最后补一次屏幕检查
                app._splash_active = False
                app.deiconify()
                fade_window(app, 300)
                app.after(140, app._entrance)
                app.after(200, app._ensure_on_screen)
            except Exception:
                pass

        sp = _SPLASH.SplashScreen(
            app, checker=_SPLASH.make_checker(ROOT))
        sp._on_done = _on_splash_done
    except Exception as e:
        # 启动动画挂了不能连带启动失败
        try:
            print("[启动] 开启动画跳过：{}: {}".format(
                type(e).__name__, e), file=sys.stderr)
        except Exception:
            pass
        app.deiconify()

    app.mainloop()


if __name__ == "__main__":
    main()
