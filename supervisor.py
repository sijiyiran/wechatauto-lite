#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""群聊大脑守护器（进程外看门狗 + 自动重启）。

为什么必须是**进程外**的：
  wechatauto 的数据库层会死锁。卡住时主线程停在某个 C 调用里**握着 GIL**，
  于是 brain.py 里那个进程内看门狗线程**永远拿不到 GIL** 去执行检查 ——
  实测就是这么失效的：日志停住、CPU 为 0、心跳早该超时，进程却一直不退。
  只有另一个进程才能可靠地观察到并处理它。

工作方式：
  1. 启动 brain.py，盯着它的「心跳文件」heartbeat.txt（brain 每轮轮询都写）
  2. 心跳停超过 stale_s 秒 -> 判定卡死 -> taskkill /T /F 杀掉整棵进程树
  3. 回到 1 重启
"""
import datetime as dt
import os
import pathlib
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
BRAIN = BASE / "brain.py"
HEARTBEAT = BASE / "heartbeat.txt"
LOG = BASE / "supervisor.log"


# **把自己那个控制台窗口藏起来。**
# supervisor 是 start-daemon.ps1 用 WMI 拉起来的，WMI 没法指定隐藏窗口，
# 而 venv 的 pythonw.exe 又是假 pythonw（Subsystem=3），于是桌面多一个黑框。
# 这里进程一启动就自己把窗口藏掉，立刻生效。
def _hide_own_console() -> None:
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def find_pythonw() -> pathlib.Path | None:
    """找到能跑 brain.py 的 pythonw.exe。

    **这里原来写死了本机路径** `C:\\Users\\pc\\.dsh-wxauto\\venv311\\…`，
    换台电脑（比如给朋友用）就必然找不到。现在按顺序找，全都不在才算失败：

      1. 程序目录下的 runtime\\pythonw.exe   ← 打包分发的形态（运行时随程序走）
      2. 程序目录同级的 venv311\\Scripts\\pythonw.exe  ← 本机开发形态
      3. 程序目录下的 venv311\\Scripts\\pythonw.exe
      4. 正在跑这个脚本的解释器自己（sys.executable 的同目录 pythonw）
      5. PATH 里的 pythonw

    找不到会返回 None，调用方会写一条**说得清原因**的日志再退出 ——
    原来只会说"缺少 brain.py 或 pythonw.exe"，那种提示对朋友毫无帮助。
    """
    cands = [
        BASE / "runtime" / "pythonw.exe",
        BASE.parent / "venv311" / "Scripts" / "pythonw.exe",
        BASE / "venv311" / "Scripts" / "pythonw.exe",
    ]
    # 自己就是 python 跑起来的：同目录的 pythonw 一般也能用
    try:
        me = pathlib.Path(sys.executable)
        if me.name.lower().startswith("python"):
            cands.append(me.with_name("pythonw.exe"))
            cands.append(me)
    except Exception:
        pass
    for c in cands:
        try:
            if c and c.exists():
                return c
        except OSError:
            continue
    # 最后找 PATH
    import shutil as _sh
    for name in ("pythonw.exe", "pythonw", "python.exe", "python"):
        w = _sh.which(name)
        if w:
            return pathlib.Path(w)
    return None


PYTHONW = find_pythonw() or pathlib.Path("pythonw.exe")

# 心跳停这么久就判定卡死。调小 => 恢复更快，但太小时正常的慢操作
# （发一条消息要走 UIA + 落库确认，可能花 20~40 秒）会被误杀。
STALE_S = 240.0
# 启动阶段允许多久没写出第一份心跳。
# 正常几秒内就有第一份 —— 但**杀毒软件在后台全盘扫描时能拖到 100 秒以上**，
# 实测（2026-10-15 21:12）那一次从"清理缓存"到"打开数据库"隔了 102 秒。
# 原先定 120 秒太紧：它刚好在"马上要成功"的时候被杀掉重启，于是无限循环，
# 而日志里只有一行行"卡在启动阶段"，看不出真正原因是慢而不是死。
# 放到 300 秒：真卡的还是会被清掉，单纯的慢能扛过去。
# 另外心跳现在已经挪到 list_groups() 之前（见 brain.py），所以正常的启动
# 心跳会比以前出现得更早，这个宽限期基本只在极端情况才用得上。
STARTUP_GRACE_S = 300.0
CHECK_EVERY_S = 15.0


def say(msg: str) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─────────────────────────── 持续失败报警 ───────────────────────────
#
# 为什么需要（2026-10-12 实测，停了 4 小时 47 分钟）：
#   守护器一直在"努力恢复"，supervisor.log 里刷满了一行行
#   "强杀整棵进程树并重启"，但**没有一次是成功的**，而且没有任何东西告诉人。
#   用户直到晚上主动问"卡了"才发现——机器人已经 5 小时没说过一句话。
#   只会默默重试、重试到天荒地老也不喊人，等于没有监控。
#
# 分层设计（关键是**不能依赖已经坏掉的东西**）：
#   1. ALERT.txt          —— 总有；写清楚时间、连续失败次数、最近一次原因
#   2. Windows 弹窗        —— 人在电脑前就能立刻看到
#   3. supervisor.log 里一条醒目的分隔记录
#   群里 @ 用户**不做**：报警的典型场景就是大脑起不来，而发消息要靠大脑，
#   靠一个已经坏掉的东西去报警，本身就是错的。
FAIL_STREAK_ALERT = 3            # 连续失败几次开始报警
ALERT_REPEAT_S = 1800.0          # 报警后多久允许再报一次（别刷屏）
ALERT_FILE = BASE / "ALERT.txt"

_fail_streak = 0
_last_alert_at = 0.0


def _write_alert_file(reason: str, streak: int) -> None:
    body = f"""微信自动回复起不来了
================================================================
时间      : {dt.datetime.now():%Y-%m-%d %H:%M:%S}
连续失败  : {streak} 次
最近一次  : {reason}

守护器还在每 30 秒重试一次，但连续 {streak} 次都没成功 ——
再这么重试下去也不会自己好，需要人看一眼。

常见原因
----------------------------------------------------------------
  1. 微信客户端没在运行、或者没登录
     -> 打开微信，确认已经登录

  2. 有残留进程占着微信数据库
     -> 任务管理器里结束所有 python / pythonw 进程，然后重新启动本程序
     -> 守护器每次启动前也会自己清一遍，但手工清更彻底

  3. 微信版本不是 4.x
     -> 这个程序只支持微信桌面版 4.x

  4. 屏幕锁了 / 屏保了
     -> 会在日志里写"屏幕截图不可用"，不会走到这里

要看细节
----------------------------------------------------------------
  daemon.log      大脑自己写的（为什么判断、为什么没回）
  supervisor.log  守护器写的（每次重启的记录）

恢复之后
----------------------------------------------------------------
  它会自己发现好了 —— 成功跑起来一次，连续失败计数就清零，
  这个文件也会被删掉。不用手动做什么。
================================================================
"""
    try:
        ALERT_FILE.write_text(body, encoding="utf-8")
    except Exception as e:
        say(f"写报警文件失败：{type(e).__name__}: {e}")


def _popup_alert(reason: str, streak: int) -> None:
    """弹一个 Windows 提示框。

    单独起一个进程弹，**不阻塞守护器** —— 守护器得继续重试。
    用 PowerShell 的 WinForms MessageBox：系统自带，不用装任何东西。

    文案写进临时文件让 PowerShell 读，**不要拼进命令行**：
    拼进去的话换行会变成字面的 `\\n`（Python 的转义和 PowerShell 的引号规则
    对不上），而且 reason 里万一有引号就会把脚本拼坏。实测就踩过——框根本不弹。
    """
    title = "微信自动回复起不来了"
    text = (f"连续 {streak} 次启动失败。\n\n"
            f"最近一次：{reason}\n\n"
            f"守护器还在每 30 秒重试，但需要你看一眼。\n\n"
            f"详情见程序目录里的 ALERT.txt")
    tmp = BASE / "_alert_popup.txt"
    try:
        tmp.write_text(text, encoding="utf-8-sig")
    except Exception as e:
        say(f"写弹窗文案失败（不影响重试）：{type(e).__name__}: {e}")
        return
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        f"$t = Get-Content -Raw -Encoding UTF8 '{tmp}';"
        "[System.Windows.Forms.MessageBox]::Show("
        f"$t, '{title}',"
        "[System.Windows.Forms.MessageBoxButtons]::OK,"
        "[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True)
    except Exception as e:
        say(f"弹窗报警失败（不影响重试）：{type(e).__name__}: {e}")


def _bump_fail_streak(reason: str, saw_heartbeat: bool) -> tuple[int, bool]:
    """记一次失败。到阈值就报警（带冷却），返回 (当前连续失败次数, 这次有没有报)。"""
    global _fail_streak, _last_alert_at
    _fail_streak += 1
    if _fail_streak < FAIL_STREAK_ALERT:
        say(f"⚠️ 连续第 {_fail_streak} 次失败（{FAIL_STREAK_ALERT} 次就报警）：{reason}")
        return _fail_streak, False

    now = time.time()
    if _last_alert_at and now - _last_alert_at < ALERT_REPEAT_S:
        return _fail_streak, False
    _last_alert_at = now

    say("=" * 62)
    say(f"🚨 连续 {_fail_streak} 次启动失败 —— 这台机器上的自动回复已经是死的了")
    say(f"   最近一次：{reason}")
    say(f"   已写 {ALERT_FILE.name}，并弹出提示框。守护器继续每 30 秒重试。")
    say("=" * 62)
    _write_alert_file(reason, _fail_streak)
    _popup_alert(reason, _fail_streak)
    return _fail_streak, True


def _reset_fail_streak() -> int:
    """成功跑起来一次 —— 计数清零，并把报警痕迹清掉。"""
    global _fail_streak, _last_alert_at
    if _fail_streak >= FAIL_STREAK_ALERT:
        say(f"✅ 恢复正常（之前连续失败 {_fail_streak} 次），清掉报警")
    _fail_streak = 0
    _last_alert_at = 0.0
    try:
        ALERT_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


def _reap_leftovers(dry_run: bool = False) -> int:
    """把本目录下残留的 brain.py / supervisor.py 进程清掉（不动自己）。

    **只能在守护器进程内部调用。** 它排除了"自己"（os.getpid()），
    而从外部脚本调用时"自己"是那个脚本 —— 结果会把正在干活的守护器和大脑
    一起杀掉。实测踩过：我用一个测试脚本调它，直接把跑着的机器人清没了。
    要在外部检查请传 dry_run=True。

    为什么需要这一步：`taskkill /T` 是按**当时的父子关系**杀的。venv 的
    pythonw 只是个启动器，真正干活的解释器是它的子进程；一旦启动器先退出、
    子进程被重新挂到别处，/T 就漏掉它。

    实测后果非常严重（2026-10-12，停了 5 小时）：
      漏掉的旧大脑继续占着微信数据库 -> 新大脑启动时卡住 ->
      2 分钟后判定"卡在启动阶段"再杀一次 -> 又漏一个 …… 无限循环。
      表现是机器人一句话都不说，而日志里只有一行行"强杀并重启"。
    """
    try:
        import psutil
    except Exception:
        return 0
    me = os.getpid()
    here = str(BASE).lower()

    # 保护自己和**自己的父进程链**。
    # 踩过的坑：venv 的 pythonw.exe 只是个转发外壳，真正跑 supervisor.py 的是
    # 它启动的子进程。两者命令行里都含 supervisor.py 和本目录 —— 只排除自己
    # 的话，会把外壳一起杀掉，刚起来的守护器立刻又没了
    # （表现是 start-daemon.cmd 报 "supervisor not alive 6s after start"）。
    protected = {me}
    try:
        anc = psutil.Process(me)
        for _ in range(5):
            anc = anc.parent()
            if anc is None:
                break
            protected.add(anc.pid)
    except Exception:
        pass

    killed = 0
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.pid in protected:
                continue
            name = (p.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            # 必须是**本目录**的 brain.py / supervisor.py，别误杀开发中的别的东西
            if here in cmd and ("brain.py" in cmd or "supervisor.py" in cmd):
                if dry_run:
                    say(f"  [dry_run] 会杀 pid={p.pid}: {cmd[:80]}")
                else:
                    p.kill()
                killed += 1
        except Exception:
            continue
    return killed


def kill_tree(pid: int) -> None:
    """杀掉整棵进程树，**并确认它真的死了**。

    只发 taskkill 不回头确认是不够的 —— 见 _reap_leftovers 的注释，
    漏掉一个残留就足以让整个机器人卡死好几个小时。
    """
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    except Exception as e:
        say(f"taskkill 失败：{type(e).__name__}: {e}")
    # 给 taskkill 一点时间落地，然后清残留
    time.sleep(2)
    n = _reap_leftovers()
    if n:
        say(f"  ⚠️ taskkill 漏了 {n} 个残留进程，已补杀"
            f"（否则它们会一直占着微信数据库，把新大脑也拖死）")


def main() -> int:
    say(f"守护器启动 (pid={os.getpid()})，心跳阈值 {int(STALE_S)}s")
    # 写 PID 文件：保活脚本（keepalive.py）靠它判断"守护器还在不在"。
    # 为什么不用 psutil 扫进程：保活必须用**真正的 pythonw.exe** 才能不闪窗，
    # 而真的 pythonw 在基础安装里、看不到 venv 的包 —— 它只能依赖标准库。
    try:
        (BASE / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    # 失败时要把**缺什么、找过哪些位置**都说清楚 —— 给朋友用时，
    # 一句"缺少 pythonw.exe"他完全不知道下一步该干嘛。
    if not BRAIN.exists():
        say(f"缺少 brain.py（找的是 {BRAIN}）—— 程序目录不完整，退出")
        return 1
    if not PYTHONW.exists():
        say("找不到 pythonw.exe —— 无法启动大脑。找过这些位置：")
        for c in (BASE / "runtime" / "pythonw.exe",
                  BASE.parent / "venv311" / "Scripts" / "pythonw.exe",
                  BASE / "venv311" / "Scripts" / "pythonw.exe"):
            say(f"    {c}")
        say("  也没在 PATH 里找到。请装 Python 3.11，"
            "或把运行时放到程序目录的 runtime\\ 下。")
        return 1
    say(f"用这个解释器跑大脑：{PYTHONW}")

    while True:
        # 每次重启先清掉旧心跳，避免拿上一次的残留当成本次的心跳
        try:
            HEARTBEAT.unlink(missing_ok=True)
        except Exception:
            pass

        # **起新的之前先清残留。** 上一轮 kill_tree 万一漏了谁（启动器先退出、
        # 真正的解释器被重新挂到别处），它就会一直占着微信数据库，
        # 让这次的新大脑卡在启动阶段 —— 于是又漏一个，滚雪球。
        # 在这里兜一道，循环就断了。
        n = _reap_leftovers()
        if n:
            say(f"启动前清掉 {n} 个残留进程（上一次没杀干净的）")
            time.sleep(3)

        t0 = time.time()
        # **必须带 CREATE_NO_WINDOW。**
        # 踩过（2026-10-28）：venv 的 pythonw.exe 是**假** pythonw（PE Subsystem=3
        # 控制台程序），不带这个标志就会在桌面上弹一个黑框。用户开了三个"自动回复窗口"
        # 其中两个就是这么来的。
        proc = subprocess.Popen(
            [str(PYTHONW), str(BRAIN)], cwd=str(BASE),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True)
        say(f"已启动 brain.py (pid={proc.pid})")

        killed_for_stale = False
        saw_heartbeat = False          # 这一轮里大脑有没有成功写出过心跳
        fail_reason = ""
        while proc.poll() is None:
            time.sleep(CHECK_EVERY_S)
            try:
                hb = float(HEARTBEAT.read_text(encoding="utf-8").strip())
            except Exception:
                hb = None

            if hb is None:
                # 还没写出第一份心跳：可能只是启动慢，也可能是卡在启动阶段
                # （连「守护启动」都没打出来那种）。**不能无限等** ——
                # 踩过这个坑：早期版本在这里 `continue`，遇上启动阶段卡死就
                # 永远不杀不重启，整个自动回复永久假死（实测死了 3.5 分钟
                # 无人问津，而日志里连一行都没有）。
                up = time.time() - t0
                if up > STARTUP_GRACE_S:
                    fail_reason = (f"启动 {int(up)} 秒仍未写出心跳"
                                   f"（第一轮都没跑完，卡在启动阶段）")
                    say(f"⚠️ {fail_reason} —— 强杀整棵进程树并重启")
                    kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=20)
                    except Exception:
                        pass
                    killed_for_stale = True
                    break
                continue

            age = time.time() - hb
            saw_heartbeat = True          # 它成功跑起来过

            # **看到新鲜心跳就把报警清掉**，不要等这一轮结束。
            # 一轮可能连续跑好几天 —— 那样 ALERT.txt 和界面上的红横幅会一直挂着，
            # 变成"狼来了"：人开始无视它，真正出事时反而没人看。
            # 实测踩过：05:16 报警，05:43 就恢复正常了，可 06:24 那个文件还在。
            if _fail_streak:
                say(f"心跳恢复正常（{int(age)}s 前）")
                _reset_fail_streak()

            if age > STALE_S:
                fail_reason = (f"心跳停了 {int(age)} 秒"
                               f"（阈值 {int(STALE_S)}s，运行中卡死）")
                say(f"⚠️ {fail_reason} —— 强杀整棵进程树并重启")
                kill_tree(proc.pid)
                try:
                    proc.wait(timeout=20)
                except Exception:
                    pass
                killed_for_stale = True
                break

        ran = int(time.time() - t0)
        # ---- 这一轮算成功还是失败？----
        # 失败的定义：被杀（卡死或卡在启动），或者活着不到 30 秒就自己崩了。
        # 只要有一轮成功，计数清零 —— 偶发一次不是"持续失败"。
        failed = killed_for_stale or (ran < 30 and proc.returncode not in (0, None))
        if killed_for_stale and not fail_reason:
            fail_reason = f"运行 {ran} 秒后被杀"
        if not killed_for_stale:
            say(f"brain.py 自行退出（运行 {ran} 秒，exit={proc.returncode}）")
            if failed and not fail_reason:
                fail_reason = f"启动后 {ran} 秒就自己退出了（exit={proc.returncode}）"

        # 注意：成功的情况**不在这里**清零 —— 能跑到这里说明这一轮已经结束了，
        # 而"恢复正常"应该一看到新鲜心跳就立刻反映出来（见上面循环里那段）。
        # 这里只负责记失败。
        if failed:
            _bump_fail_streak(fail_reason, saw_heartbeat)

        time.sleep(30 if ran < 30 else 5)


# 进程一起来就把自己那个黑框藏掉。
# supervisor 是 start-daemon.ps1 用 WMI 拉起来的，WMI 不支持指定隐藏窗口，
# 而 venv 的 pythonw.exe 是假 pythonw（PE Subsystem=3 控制台程序），
# 于是桌面上会多一个黑框（用户 2026-10-28 反馈）。
_hide_own_console()


if __name__ == "__main__":
    sys.exit(main())
