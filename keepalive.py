# -*- coding: utf-8 -*-
"""保活：守护器没在跑就把它拉起来。由计划任务每 5 分钟调用一次。

**必须用「真正的」pythonw.exe 运行（基础安装里那个，PE Subsystem=2）。**

踩过的两个坑，都记在这儿免得再犯：

坑 1：第一版计划任务用 `powershell.exe -WindowStyle Hidden -File start-daemon.ps1`。
      Windows 会**先创建控制台窗口**，PowerShell 起来之后才去隐藏它，那一下会明显闪。
      用户的原话："过一会就会有弹窗然后马上关闭很烦"，每 5 分钟一次。

坑 2：第二版以为换成 `venv\\Scripts\\pythonw.exe` 就好了 —— **并不**。
      查 PE 头的 Subsystem 字段才发现：
          基础安装的 pythonw.exe   Subsystem=2  GUI，不创建控制台   <- 真的
          venv 的   pythonw.exe    Subsystem=3  控制台，照样闪窗    <- 假的（只是个转发外壳）
      venv 的 pythonw 和 python 一样大（45568 字节），本质就是同一个控制台程序。

因为要跑真 pythonw（看不见 venv 的 site-packages），
**这个脚本只能用标准库** —— 所以判断进程存活用的是 PID 文件 + ctypes，不是 psutil。
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import pathlib
import struct
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
SUPERVISOR = BASE / "supervisor.py"
PIDFILE = BASE / "supervisor.pid"
STATE = BASE / "keepalive-state.json"
ALERT = BASE / "ALERT.txt"
LOG = BASE / "keepalive.log"

# 一小时内被迫重启这么多次，基本可以断定是杀毒软件在反复杀它
RESTART_ALERT_THRESHOLD = 3
WINDOW_S = 3600.0
POPUP_COOLDOWN_S = 3600.0

# 启动子进程时用：不给它分配可见的控制台窗口
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _pe_subsystem(path: pathlib.Path) -> int | None:
    """读 PE 头里的 Subsystem 字段：2=GUI（无控制台），3=控制台（会闪窗）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(0x400)
        if head[:2] != b"MZ":
            return None
        e = struct.unpack_from("<I", head, 0x3C)[0]
        with open(path, "rb") as f:
            f.seek(e)
            d = f.read(0x100)
        if d[:4] != b"PE\0\0":
            return None
        return struct.unpack_from("<H", d, 24 + 68)[0]
    except Exception:
        return None


def find_real_pythonw() -> pathlib.Path | None:
    """找一个**真正的** pythonw.exe（Subsystem=2）。

    优先基础安装（venv 的 pyvenv.cfg 里 home= 指向它），因为
    venv\\Scripts\\pythonw.exe 是控制台程序，拿它跑计划任务照样闪窗。
    """
    cands: list[pathlib.Path] = []
    # 1) venv 的 pyvenv.cfg 里 home= 就是基础安装目录
    for cfg in (BASE / ".." / "venv311" / "pyvenv.cfg",
                BASE / "venv311" / "pyvenv.cfg"):
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().lower().startswith("home"):
                    home = line.split("=", 1)[1].strip()
                    cands.append(pathlib.Path(home) / "pythonw.exe")
        except Exception:
            pass
    # 2) uv 的默认安装位置
    import os
    cands.append(pathlib.Path(os.environ.get("APPDATA", "")) /
                 "uv" / "python" / "cpython-3.11-windows-x86_64-none" / "pythonw.exe")
    # 3) 打包运行时（bundle-runtime.ps1 复制的是基础安装，所以它是真的）
    cands.append(BASE / "runtime" / "pythonw.exe")
    # 4) 兜底：venv 的（会闪窗，但总比不干活强）
    cands.append(BASE.parent / "venv311" / "Scripts" / "pythonw.exe")
    cands.append(BASE / "venv311" / "Scripts" / "pythonw.exe")

    for c in cands:
        try:
            if c.exists() and _pe_subsystem(c) == 2:
                return c
        except Exception:
            continue
    return None


def find_any_python() -> pathlib.Path | None:
    """启动守护器用一个能看见 venv 包的解释器（subsystem 无所谓，
    因为我们用 CREATE_NO_WINDOW 启动，不会给它控制台窗口）。"""
    for c in (BASE.parent / "venv311" / "Scripts" / "pythonw.exe",
              BASE / "venv311" / "Scripts" / "pythonw.exe",
              BASE / "runtime" / "pythonw.exe"):
        if c.exists():
            return c
    import shutil
    p = shutil.which("pythonw.exe") or shutil.which("python.exe")
    return pathlib.Path(p) if p else None


def _pid_alive(pid: int) -> bool:
    """用 OpenProcess 判断进程还在不在（纯标准库）。"""
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259      # STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(h)


def supervisor_alive() -> bool:
    """守护器还活着吗？

    只信 PID 文件：进程在、且那个 pid 真的还活着。
    PID 会被系统复用，所以再加一道"进程名以 python 开头"的确认 —— 不然
    复用到别的程序上，就会误判成"守护器还在"，永远不去拉它。
    """
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if not _pid_alive(pid):
        return False
    # 确认这个 pid 确实是我们的 python 进程
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW).stdout
        return "python" in out.lower()
    except Exception:
        # 查不了就认 PID 存活，宁可少启动一次也不要重复启动
        return True


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except Exception:
        pass


def popup(title: str, text: str) -> None:
    """弹提示框。tkinter 是标准库，GUI 子系统下不会闪控制台。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showwarning(title, text)
        root.destroy()
    except Exception as e:
        log(f"弹窗失败：{type(e).__name__}: {e}")


def write_alert(restarts: int) -> None:
    body = f"""微信自动回复被反复杀掉
================================================================
时间      : {dt.datetime.now():%Y-%m-%d %H:%M:%S}
一小时内  : 守护器被迫重启 {restarts} 次

这说明有东西在**反复结束它的进程**。最常见的原因是杀毒软件 ——
本程序要读另一个进程的内存、写它的内存、解密它的数据库，
在杀毒软件眼里和键盘记录器一样。

最可能的凶手
----------------------------------------------------------------
  火绒 / 360 / Windows Defender

怎么办（只有这一条能根治）
----------------------------------------------------------------
  把整个程序目录加进杀毒软件的「信任区 / 排除项」：

      {BASE}

    火绒    : 防护中心 -> 信任区 -> 添加目录
    360     : 木马查杀 -> 信任区 -> 添加目录
    Defender: 设置 -> 病毒和威胁防护 -> 排除项 -> 添加文件夹

  加完最好重启一次电脑让排除项生效。

现状
----------------------------------------------------------------
  保活任务会继续每 5 分钟把它拉起来，所以不是"完全不能用"，
  而是"起来一会儿又被打断" —— 被杀的那一瞬间正在处理的群消息会丢。
  加白名单之前，这个文件不会消失。
================================================================
"""
    try:
        ALERT.write_text(body, encoding="utf-8")
    except Exception as e:
        log(f"写报警文件失败：{type(e).__name__}: {e}")


def main() -> int:
    if supervisor_alive():
        # 正常情况：一声不响地退出。计划任务每 5 分钟一次，绝大多数走这里。
        st = load_state()
        if st.get("restarts"):
            log("守护器已恢复正常，清掉重启计数")
            save_state({"restarts": [], "last_popup": 0.0})
            try:
                ALERT.unlink(missing_ok=True)
            except Exception:
                pass
        return 0

    # ---- 没在跑：拉起来 ----
    starter = find_any_python()
    if starter is None or not SUPERVISOR.exists():
        log(f"无法启动：starter={starter} supervisor={SUPERVISOR.exists()}")
        return 1

    try:
        # CREATE_NO_WINDOW：即使 starter 是控制台程序，也不会给它可见的窗口
        subprocess.Popen([str(starter), str(SUPERVISOR)], cwd=str(BASE),
                         close_fds=True,
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)
        log(f"守护器没在跑 -> 已拉起（{starter.name}）")
    except Exception as e:
        log(f"拉起失败：{type(e).__name__}: {e}")
        return 1

    # ---- 记一次重启，太频繁就报警 ----
    now = time.time()
    st = load_state()
    hist = [t for t in (st.get("restarts") or []) if now - t < WINDOW_S]
    hist.append(now)
    last_popup = float(st.get("last_popup") or 0.0)
    save_state({"restarts": hist, "last_popup": last_popup})

    if len(hist) >= RESTART_ALERT_THRESHOLD:
        log(f"⚠️ 一小时内已重启 {len(hist)} 次 —— 有东西在反复杀它")
        if now - last_popup > POPUP_COOLDOWN_S:
            save_state({"restarts": hist, "last_popup": now})
            write_alert(len(hist))
            popup("微信自动回复被反复杀掉",
                  f"一小时内它被迫重启了 {len(hist)} 次。\n\n"
                  f"这通常说明杀毒软件（火绒/360/Defender）在反复结束它的进程。\n\n"
                  f"请把下面这个目录加进杀毒软件的信任区：\n{BASE}\n\n"
                  f"详情见同目录的 ALERT.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
