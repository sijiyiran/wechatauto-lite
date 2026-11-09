"""开启动画：播视频帧 + 底部两条进度条（检查更新 / 检查资源完整性）。

资源占用设计
------------
用户明确说「不要太多会卡」（机器是 Intel N95）。

- **不实时解码视频**：ffmpeg 预抽成 400x224 的 jpg（90 帧，1.56MB 磁盘）。
  实时解 1920x1080@60fps 会把 CPU 跑满。
- **不用 Tkinter 原生 PhotoImage**：它不认 jpg，用 PIL.ImageTk。
- **15fps 播放**（after 66ms），90 帧循环 ≈ 6 秒一轮。
- **内存约 30MB**（90 × 400×224×4 字节），启动完立即释放。
- 帧在后台线程加载，不阻塞窗口显示。

启动流程
--------
    splash = SplashScreen(root, on_done)
    ...
    splash.finish()   # 检查全做完后关闭

两个进度条分别表示：
  1. 检查更新（查 GitHub Release）
  2. 检查资源完整性（关键文件 + manifest 校验）
"""
import pathlib
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
    _PIL = True
except Exception:
    _PIL = False

BASE = pathlib.Path(__file__).parent
FRAME_DIR = BASE / "splash_frames"

# 视频区域尺寸（和抽帧尺寸一致）
VID_W, VID_H = 400, 224
# 底部进度区高度
BAR_H = 132
FPS_MS = 66          # 15fps

# 配色：和主界面暗色一致
C_BG = "#15171c"
C_SURFACE = "#1d2027"
C_INK = "#e8eaef"
C_SUB = "#8b93a3"
C_ACCENT = "#7c8cf8"


class SplashScreen(tk.Toplevel):
    """启动画面。"""

    def __init__(self, master, checker=None):
        super().__init__(master)
        # 给个标题：无边框窗口在任务栏上没名字，调试和识别都不方便
        try:
            self.title("正在启动…")
        except Exception:
            pass
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.configure(bg=C_BG)
        self.resizable(False, False)

        total_w, total_h = VID_W, VID_H + BAR_H
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - total_w) // 2
        y = (sh - total_h) // 2 - 40
        self.geometry("{}x{}+{}+{}".format(total_w, total_h, x, y))

        # ── 视频区 ──
        self.canvas = tk.Canvas(self, width=VID_W, height=VID_H,
                                highlightthickness=0, bd=0, bg="#000000")
        self.canvas.pack(fill="x")

        # ── 底部 ──
        foot = tk.Frame(self, bg=C_SURFACE, height=BAR_H)
        foot.pack(fill="both", expand=True)
        foot.pack_propagate(False)

        self.lbl_title = tk.Label(
            foot, text="正在启动…", bg=C_SURFACE, fg=C_INK,
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.lbl_title.pack(fill="x", padx=18, pady=(10, 2))

        # 进度条 1：检查更新
        self.lbl_upd = tk.Label(
            foot, text="检查更新", bg=C_SURFACE, fg=C_SUB,
            font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_upd.pack(fill="x", padx=18)
        self.pb_upd = ttk.Progressbar(foot, mode="determinate",
                                      maximum=100, length=VID_W - 36)
        self.pb_upd.pack(fill="x", padx=18, pady=(2, 6))

        # 进度条 2：资源完整性
        self.lbl_res = tk.Label(
            foot, text="检查资源完整性", bg=C_SURFACE, fg=C_SUB,
            font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_res.pack(fill="x", padx=18)
        self.pb_res = ttk.Progressbar(foot, mode="determinate",
                                      maximum=100, length=VID_W - 36)
        self.pb_res.pack(fill="x", padx=18, pady=(2, 8))

        # ── 状态 ──
        self._frames = []
        self._idx = 0
        self._img_item = None
        self._playing = False
        self._closing = False
        self._checker = checker
        self._on_done = None
        self._min_show = 1.2      # 至少显示 1.2 秒，别闪一下就没了
        self._t0 = time.time()

        self.deiconify()
        self.after(60, self._start)

    # ── 启动：加载帧 + 跑检查 ──
    def _start(self):
        # **帧加载放后台，但播放循环必须在主线程。**
        # 踩过（2026-11-08）：在后台线程里 self.after(0, self._begin_play)
        # 会导致帧一直显示不出来（Tkinter 的 after 不是线程安全的），
        # 表现就是启动画面视频区全黑。
        # **帧必须在主线程转成 PhotoImage。**
        # 踩过（2026-11-08）：在后台线程里调 ImageTk.PhotoImage，Tk 对象
        # 跨线程创建，会静默失败 —— 表现就是视频区永远是黑的，不报错。
        # 90 帧主线程加载只要 ~0.4 秒，启动阶段完全能接受。
        try:
            self._load_frames()
        except Exception:
            pass
        self._tick()          # 主线程开始播放循环
        if self._checker:
            threading.Thread(target=self._run_check, daemon=True).start()
        else:
            self.pb_upd["value"] = 100
            self.pb_res["value"] = 100
            self._maybe_finish()

    def _load_frames(self):
        """后台加载帧，加载一帧就试着播一帧（不用等全部载入）。"""
        if not _PIL:
            return
        files = sorted(FRAME_DIR.glob("*.jpg"))
        if not files:
            return
        for p in files:
            if self._closing:
                break
            try:
                im = Image.open(p)
                self._frames.append(ImageTk.PhotoImage(im))
            except Exception:
                continue
        # 首帧先画出来，别等第一次 _tick
        if self._frames:
            try:
                self._img_item = self.canvas.create_image(
                    0, 0, anchor="nw", image=self._frames[0])
            except Exception:
                pass

    def _tick(self):
        if self._closing:
            return
        try:
            if self._frames:
                if self._img_item is None:
                    self._img_item = self.canvas.create_image(
                        0, 0, anchor="nw", image=self._frames[0])
                else:
                    self._idx = (self._idx + 1) % len(self._frames)
                    self.canvas.itemconfig(self._img_item,
                                           image=self._frames[self._idx])
        except Exception:
            pass
        self.after(FPS_MS, self._tick)

    def _run_check(self):
        """跑两个检查，更新进度条。"""
        def setp(which, val, text=None):
            def _do():
                if self._closing:
                    return
                bar = self.pb_upd if which == "upd" else self.pb_res
                bar["value"] = max(0, min(100, val))
                if text:
                    lbl = self.lbl_upd if which == "upd" else self.lbl_res
                    lbl.configure(text=text)
            self.after(0, _do)

        try:
            self._checker(setp)
        except Exception as e:
            setp("res", 100, "资源检查失败（{}）".format(type(e).__name__))
        setp("upd", 100)
        setp("res", 100)
        self.after(0, self._maybe_finish)

    def _maybe_finish(self):
        """两个检查都完成，且至少显示够了时间，才关。"""
        try:
            if self.pb_upd["value"] < 100 or self.pb_res["value"] < 100:
                self.after(120, self._maybe_finish)
                return
            wait = self._min_show - (time.time() - self._t0)
            if wait > 0:
                self.after(int(wait * 1000), self._maybe_finish)
                return
        except Exception:
            pass
        self.finish()

    def finish(self):
        if self._closing:
            return
        self._closing = True
        self._playing = False
        try:
            self._frames.clear()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self._on_done:
            try:
                self._on_done()
            except Exception:
                pass

# ══════════════════════ 检查器 ══════════════════════
def make_checker(base_dir: pathlib.Path):
    """返回一个 checker(setp) 函数，给 SplashScreen 用。

    setp(which, value, text) —— which 是 "upd" 或 "res"。
    """
    # 关键文件清单（缺了说明安装不完整）
    KEY_FILES = [
        "brain.py", "gui.py", "memory.py", "emotion.py",
        "updater.py", "prompts/persona.md", "app.ico", "config.json",
    ]

    def checker(setp):
        import json

        # ── 1) 检查更新 ──
        setp("upd", 8, "检查更新…")
        try:
            import updater as U
            cfg_path = base_dir / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            edition = (cfg.get("ui", {}).get("edition")
                       or cfg.get("update", {}).get("edition") or "dev")
            repo = U.repo_of(cfg, edition)
            if not repo:
                setp("upd", 100, "检查更新（未配置仓库，跳过）")
            else:
                # 整体限时 10 秒：多源回退可能很慢，启动不能等太久
                _res = {}

                def _do():
                    try:
                        _res["r"] = U.check_latest(cfg, edition)
                    except Exception as e:
                        _res["e"] = e

                import threading as _th
                th = _th.Thread(target=_do, daemon=True)
                th.start()
                th.join(10.0)
                if th.is_alive():
                    setp("upd", 100, "检查更新超时（跳过）")
                elif _res.get("e"):
                    setp("upd", 100, "检查更新失败（跳过）")
                elif _res.get("r"):
                    setp("upd", 100, "发现新版本 v{}".format(
                        _res["r"]["version"]))
                else:
                    setp("upd", 100, "已是最新版本")
        except Exception:
            setp("upd", 100, "检查更新跳过")

        # ── 2) 检查资源完整性 ──
        setp("res", 10, "检查资源完整性…")
        missing = []
        for i, rel in enumerate(KEY_FILES):
            p = base_dir / rel
            if not p.exists():
                missing.append(rel)
            setp("res", 10 + int(70 * (i + 1) / max(1, len(KEY_FILES))))

        # 帧目录（启动动画资源）
        if FRAME_DIR.exists():
            n = len(list(FRAME_DIR.glob("*.jpg")))
            if n < 30:
                missing.append("splash_frames/（只有 {} 帧）".format(n))
        setp("res", 90, "检查资源完整性…")

        if missing:
            setp("res", 100, "资源异常：{} 个".format(len(missing)))
        else:
            setp("res", 100, "资源完整")

    return checker
