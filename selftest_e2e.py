"""End-to-end live test of the brain pipeline against real group data.

Temporarily flips the group to addressed_or_relevant, rolls the watermark back
one message so the newest real message counts as "new", runs one tick, then
restores the config. dry_run stays true, so nothing is ever sent.
"""
import json, pathlib, subprocess, sys

BASE = pathlib.Path(r"C:\Users\pc\.dsh-wxauto\group-brain")
PY = r"C:\Users\pc\.dsh-wxauto\venv311\Scripts\python.exe"
UID = "47459029235@chatroom"
CFG = BASE / "config.json"
STATE = BASE / "state.json"

cfg = json.loads(CFG.read_text(encoding="utf-8"))
original = json.dumps(cfg["groups"][UID], ensure_ascii=False)

try:
    cfg["groups"][UID].setdefault("participation", {})["mode"] = "addressed_or_relevant"
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    st = json.loads(STATE.read_text(encoding="utf-8"))
    st["watermarks"][UID] = int(st["watermarks"][UID]) - 1
    st.pop("last_reply", None)          # 清掉冷却，确保这一步不被冷却拦住
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[test] mode=addressed_or_relevant, watermark -> {st['watermarks'][UID]}")

    r = subprocess.run([PY, str(BASE / "brain.py"), "--once", "--group", UID],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("--- brain stdout ---")
    for line in (r.stdout or "").splitlines():
        if "首次接管" not in line:
            print("   ", line)
    if r.stderr:
        tail = [l for l in r.stderr.splitlines() if "INFO" not in l][-6:]
        for line in tail:
            print("   [err]", line)
finally:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    cfg["groups"][UID] = json.loads(original)
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[test] config restored:", json.dumps(cfg["groups"][UID], ensure_ascii=False))

print("\n--- last decision record ---")
log = BASE / "decisions.jsonl"
if log.exists():
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    if lines:
        print(json.dumps(json.loads(lines[-1]), ensure_ascii=False, indent=2))
    else:
        print("(empty)")
else:
    print("(no decisions.jsonl)")
