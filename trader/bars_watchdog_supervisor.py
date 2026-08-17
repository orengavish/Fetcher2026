"""
trader/bars_watchdog_supervisor.py  v1.0

Tiny supervisor that keeps bars_fetch_watchdog.py itself running 24/7. If the
watchdog process ever dies (crash, killed, machine hiccup), this relaunches
it after a short delay. bars_fetch_watchdog.py keeps the bar fetchers up;
this keeps the watchdog up — the "who watches the watchmen" layer requested
in BARS1S_STATUS.md §0i.

This is the one process to point at for true 24/7 durability — e.g. a
Windows Task Scheduler entry set to run at login / on a schedule, so the
whole chain (supervisor -> watchdog -> fetcher) survives a reboot too.

Usage:
  python trader/bars_watchdog_supervisor.py
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_WATCHDOG = _ROOT / "trader" / "bars_fetch_watchdog.py"
_DATA = _ROOT / "data"
_LOG = _DATA / "bars_fetch_watchdog_run.log"
_ERR = _DATA / "bars_fetch_watchdog_run_err.log"
_LOCK_FILE = _DATA / "bars_watchdog_supervisor.lock"

RESTART_DELAY_S = 10


def _acquire_lock() -> bool:
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text().strip())
            import psutil
            try:
                proc = psutil.Process(pid)
                if proc.status() not in ("zombie", "dead") and \
                   "bars_watchdog_supervisor" in " ".join(proc.cmdline()):
                    print(f"[SUPERVISOR] another instance is already running (pid={pid}) — exiting")
                    return False
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                pass
            _LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            _LOCK_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def main():
    if not _acquire_lock():
        sys.exit(1)

    print(f"[SUPERVISOR] watching bars_fetch_watchdog.py — restart delay {RESTART_DELAY_S}s", flush=True)
    try:
        while True:
            ts = datetime.now(timezone.utc).isoformat()
            print(f"[SUPERVISOR] {ts} starting bars_fetch_watchdog.py", flush=True)
            out = open(_LOG, "a")
            err = open(_ERR, "a")
            proc = subprocess.Popen(
                [sys.executable, str(_WATCHDOG)], cwd=str(_ROOT), stdout=out, stderr=err,
            )
            code = proc.wait()
            ts = datetime.now(timezone.utc).isoformat()
            print(f"[SUPERVISOR] {ts} bars_fetch_watchdog.py exited (code={code}) — "
                  f"restarting in {RESTART_DELAY_S}s", flush=True)
            time.sleep(RESTART_DELAY_S)
    finally:
        _LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
