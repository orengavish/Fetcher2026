"""
trader/bars_fetch_watchdog.py  v3.0

Watchdog for the bars1s_fetcher family (1s/5s/30s bar sizes). Runs GROUPS of
bar-size fetchers, not individual stages (§0l): 5s runs alone; 1s and 30s run
CONCURRENTLY with each other. Never all three at once — that caused a severe
IB pacing collapse (real throughput fell to ~0.19 chunks/min, ~88% of
requests wasted on stale-window/pacing-violation retries — see
BARS1S_STATUS.md §0d). 1s+30s together is a much lighter combination: 30s
only needs 3 requests/day/symbol, so it barely dents the shared pacing
budget 1s is using — that's why this specific pairing is safe where 3-way
concurrency wasn't. Each fetcher's own --pace-max is tuned so the pair
together still fits under IB's real ~60-req/10-min ceiling.

Which GROUP gets to run is a WEIGHTED priority schedule (§0i/§0j): 5s gets
~80% of wall-clock run time; {1s, 30s} get the remaining ~20% together. This
is deficit-based (see _pick_next_group): it tracks real time-spent-running
per group in data/bars_watchdog_schedule.json (persists across watchdog
restarts) and always hands the next turn to whichever INCOMPLETE group is
furthest below its target share. A group only gets reconsidered every
MIN_QUANTUM_S (not every 60s check) to avoid thrashing processes (each
restart costs a fresh IB connection + contract resolution).

Every 60s this watchdog:
  1. Kills any bars1s_fetcher.py process whose bar size isn't a member of the
     current group — enforces "only this group's members run" even if
     something gets launched by mistake.
  2. Decides the current group via the weighted scheduler above.
  3. For each (still-incomplete) member of that group whose process is dead,
     (re)starts it — bars1s_fetcher.py always resumes from its own progress
     DB, so this is safe regardless of which other members are also running.
  4. Cleans stale lock files (process dead but lock file left behind).
  5. Emails on repeated restart failure or a long stall (reuses send_email.py
     the same way fetch_watchdog.py does for the tick-data fetcher).

Usage:
  python trader/bars_fetch_watchdog.py          # runs forever
  python trader/bars_fetch_watchdog.py --once   # single check (for testing)
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

import sqlite3
import psutil

from lib.logger import get_logger

log = get_logger("bars_fetch_watchdog")

_FETCHER_SCRIPT = _ROOT / "trader" / "bars1s_fetcher.py"
_DATA = _ROOT / "data"
_SCHEDULE_FILE = _DATA / "bars_watchdog_schedule.json"
_EMAIL_SCRIPT = _ROOT / "send_email.py"
_EMAIL_TO = "gavish.oren@gmail.com"

CHECK_INTERVAL = 60
STALE_THRESHOLD = 20 * 60      # 20 min with no DB write on the active stage = worth alerting
RESTART_COOLDOWN = 120         # seconds between restart attempts of the same stage
MIN_QUANTUM_S = 15 * 60        # minimum time a stage runs before the scheduler reconsiders

# GROUPS (§0l): which bar-size fetchers run concurrently as a unit. 5s runs
# alone; 1s and 30s run together — safe because 30s barely uses any of the
# shared IB pacing budget (3 req/day/symbol), so it doesn't meaningfully
# compete with 1s's share. Never put 5s in the same group as the others;
# that recreates the 3-way collapse from §0d.
GROUPS = {
    "5s": ["5s"],
    "1s+30s": ["1s", "30s"],
}
# Target share of wall-clock run time per GROUP (not per stage). 5s is the
# priority dataset for tonight (~80%); {1s, 30s} share the remaining ~20%.
GROUP_WEIGHTS = {"5s": 0.8, "1s+30s": 0.2}
# Per-suffix weights derived for reporting (bars_overnight_report.py etc.)
# only — the scheduler itself always operates on GROUP_WEIGHTS/GROUPS.
WEIGHTS = {
    suffix: GROUP_WEIGHTS[group] / len(members)
    for group, members in GROUPS.items()
    for suffix in members
}

QUEUE = list(WEIGHTS)   # order no longer determines priority, just iteration
STAGE_CONFIG = {
    "5s":  {"extra_args": ["--bar-secs", "5", "--symbols", "MES,MNQ", "--days", "42"],
            "symbols": 2, "days": 42, "chunks_per_day": 24},
    # 1s+30s run concurrently now (§0l) — each gets a --pace-max share of the
    # shared IB budget rather than assuming the full 40 to itself. 30s only
    # needs 3 req/day/symbol so it gets a small slice; 1s gets the rest.
    "30s": {"extra_args": ["--bar-secs", "30", "--days", "252", "--pace-max", "10"],
            "symbols": 4, "days": 252, "chunks_per_day": 3},
    "1s":  {"extra_args": ["--days", "84", "--pace-max", "30"],
            "symbols": 4, "days": 84, "chunks_per_day": 48},
}

_last_restart_ts: dict = {}   # suffix -> epoch seconds
_consecutive_failures = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_email(subject: str, body: str):
    if not _EMAIL_SCRIPT.exists():
        log.warning("Email script not found: %s", _EMAIL_SCRIPT)
        return
    try:
        subprocess.run([sys.executable, str(_EMAIL_SCRIPT), subject, body], check=False, timeout=30)
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.warning("Email failed: %s", e)


def _running_fetchers() -> dict:
    """Return {suffix: [pids]} for every currently-running bars1s_fetcher.py
    process, keyed by the bar size it was launched with (parsed from its
    --bar-secs arg; defaults to '1s' if the flag is absent, matching the
    script's own default).

    Matches on cwd() rather than an absolute path inside the command line —
    the fetcher is commonly launched with a relative script path
    ("trader/bars1s_fetcher.py" from cwd=repo root), which never contains the
    project root as a substring of cmdline. Matching on cwd() instead is what
    actually identifies "a bars1s_fetcher.py process belonging to this repo"
    reliably (verified: string-matching cmdline against the root path missed
    a real running process entirely and nearly caused a duplicate launch).
    """
    our_root = str(_ROOT.resolve()).lower()
    found: dict = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd_list = proc.info.get("cmdline") or []
            if not cmd_list:
                continue
            cmdline = " ".join(cmd_list)
            if "bars1s_fetcher" not in cmdline or "bars_fetch_watchdog" in cmdline:
                continue
            # Exclude wrapper processes (e.g. git-bash's nohup.exe, which stays
            # alive as a supervising parent and has the same cmdline substring
            # and cwd as the real python.exe child it launched) — only a real
            # python interpreter counts as "a running fetcher".
            if "python" not in Path(cmd_list[0]).name.lower():
                continue
            try:
                proc_cwd = proc.cwd().lower()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if proc_cwd != our_root:
                continue
            bar_secs = "1"
            for i, tok in enumerate(cmd_list):
                if tok == "--bar-secs" and i + 1 < len(cmd_list):
                    bar_secs = cmd_list[i + 1]
            suffix = f"{bar_secs}s"
            found.setdefault(suffix, []).append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def _stage_progress(suffix: str):
    """Return (chunks_done_total, target_total_chunks, latest_updated_at) for a stage."""
    cfg = STAGE_CONFIG[suffix]
    target = cfg["chunks_per_day"] * cfg["days"] * cfg["symbols"]
    db = _DATA / f"bars{suffix}_progress.db"
    if not db.exists():
        return 0, target, None
    try:
        uri = f"file:{db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        row = conn.execute(
            "SELECT COALESCE(SUM(chunks_done),0), MAX(updated_at) FROM progress"
        ).fetchone()
        conn.close()
        return row[0] or 0, target, row[1]
    except Exception:
        return 0, target, None


def _recent_row_is_bogus(suffix: str):
    """Check the single most-recently-updated row for a stage: finished=1
    with bars_fetched=0 is never legitimate for a full-session liquid
    futures day — that exact pattern is the 2026-07-23 connection-loss
    incident (see BARS1S_STATUS.md §0f). bars1s_fetcher.py itself no longer
    produces this (it now crashes instead of silently accepting a dead
    connection as "no trades"), but this stays as a defense-in-depth check
    in case some other path ever slips through — the watchdog restarting the
    stuck process is enough; bars1s_fetcher.py's own no-skip full traversal
    (see run()) will self-heal the bad row once it revisits that day."""
    db = _DATA / f"bars{suffix}_progress.db"
    if not db.exists():
        return None
    try:
        uri = f"file:{db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        row = conn.execute(
            "SELECT symbol, date, bars_fetched, finished FROM progress "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[3] and (row[2] or 0) == 0:
            return f"{row[0]} {row[1]}"
    except Exception:
        pass
    return None


def _incomplete_stages() -> list:
    result = []
    for s in QUEUE:
        done, target, _ = _stage_progress(s)
        if done < target:
            result.append(s)
    return result


def _incomplete_groups() -> list:
    """A group is incomplete as long as ANY of its members still is."""
    incomplete_stages = set(_incomplete_stages())
    return [g for g, members in GROUPS.items() if incomplete_stages & set(members)]


def _active_members(group: str) -> list:
    """Members of a group that still need work — a group can be picked while
    one member is already fully done (e.g. 30s finishes before 1s); only the
    still-incomplete members should actually get a process."""
    incomplete_stages = set(_incomplete_stages())
    return [m for m in GROUPS[group] if m in incomplete_stages]


def _load_schedule() -> dict:
    try:
        return json.loads(_SCHEDULE_FILE.read_text())
    except Exception:
        return {"time_spent": {g: 0.0 for g in GROUPS}, "active_group": None, "active_since": None}


def _save_schedule(sched: dict):
    try:
        _SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SCHEDULE_FILE.write_text(json.dumps(sched))
    except Exception as e:
        log.warning("Could not save schedule state: %s", e)


def _pick_next_group(sched: dict, incomplete: list) -> str:
    """Deficit-weighted pick: whichever incomplete group's actual time-share
    is furthest below its target GROUP_WEIGHTS share gets the next turn.
    Falls back to the highest-weight incomplete group if there's no history
    yet."""
    time_spent = sched.get("time_spent", {})
    total = sum(time_spent.get(g, 0.0) for g in incomplete)
    if total <= 0:
        return max(incomplete, key=lambda g: GROUP_WEIGHTS.get(g, 0))
    deficits = {
        g: GROUP_WEIGHTS.get(g, 0) - (time_spent.get(g, 0.0) / total)
        for g in incomplete
    }
    return max(deficits, key=deficits.get)


def _current_group() -> str:
    """Weighted-priority pick (see module docstring / _pick_next_group): 5s
    gets ~80% of run time, {1s, 30s} together get the rest, tracked via time
    actually spent running (persisted in _SCHEDULE_FILE so restarts of the
    watchdog itself don't reset the ratio). Only reconsiders every
    MIN_QUANTUM_S — otherwise just keeps returning whichever group is
    already active, so processes aren't killed/restarted every 60s check."""
    incomplete = _incomplete_groups()
    if not incomplete:
        return list(GROUPS)[-1]   # everything done — nothing left to schedule

    sched = _load_schedule()
    now = time.time()
    active = sched.get("active_group")
    since = sched.get("active_since")

    if active in incomplete and since is not None and (now - since) < MIN_QUANTUM_S:
        return active   # still within this group's quantum — don't reconsider yet

    # Quantum expired (or no active group, or active group just completed):
    # bank the elapsed time against the outgoing group, then pick the next one.
    if active is not None and since is not None:
        sched.setdefault("time_spent", {})
        sched["time_spent"][active] = sched["time_spent"].get(active, 0.0) + (now - since)

    next_group = _pick_next_group(sched, incomplete)
    sched["active_group"] = next_group
    sched["active_since"] = now
    _save_schedule(sched)
    return next_group


def _clean_stale_lock(suffix: str) -> bool:
    lock = _DATA / f"bars{suffix}_fetcher.lock"
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
        if not psutil.pid_exists(pid):
            lock.unlink(missing_ok=True)
            log.info("Cleaned stale lock for %s (dead pid=%d)", suffix, pid)
            return True
    except Exception:
        lock.unlink(missing_ok=True)
        log.info("Cleaned unreadable lock for %s", suffix)
        return True
    return False


def _start_stage(suffix: str) -> bool:
    global _last_restart_ts
    now = time.time()
    last = _last_restart_ts.get(suffix, 0)
    if now - last < RESTART_COOLDOWN:
        log.info("Restart cooldown active for %s (%ds remaining)", suffix, RESTART_COOLDOWN - (now - last))
        return False

    _clean_stale_lock(suffix)
    args = [sys.executable, str(_FETCHER_SCRIPT)] + STAGE_CONFIG[suffix]["extra_args"]
    out_log = open(_DATA / f"bars{suffix}_run.log", "a")
    err_log = open(_DATA / f"bars{suffix}_run_err.log", "a")
    try:
        subprocess.Popen(args, cwd=str(_ROOT), stdout=out_log, stderr=err_log)
        _last_restart_ts[suffix] = now
        log.info("Started stage %s: %s", suffix, " ".join(args))
        return True
    except Exception as e:
        log.error("Failed to start stage %s: %s", suffix, e)
        return False


# ── Main health check ─────────────────────────────────────────────────────────

def check_and_heal():
    """Returns (actions, group, active_members) — exposed so callers don't
    need a second _current_group() call, which would risk double-advancing
    the quantum-based scheduler within the same cycle."""
    global _consecutive_failures
    actions = []

    running = _running_fetchers()
    group = _current_group()
    active_members = _active_members(group)   # only still-incomplete members get a process

    # 1. Kill any process whose bar size isn't an active member of the
    #    current group — enforces "only this group's members run" (see
    #    module docstring; this is what keeps 5s from ever running
    #    alongside 1s/30s, the combination that caused the §0d collapse).
    for suffix, pids in running.items():
        if suffix not in active_members:
            for pid in pids:
                try:
                    psutil.Process(pid).terminate()
                    actions.append(f"KILLED_NOT_IN_GROUP:{suffix}:pid={pid}")
                    log.warning("Killed %s fetcher pid=%d — current group is %s (members: %s)",
                                suffix, pid, group, active_members)
                except Exception as e:
                    log.warning("Could not kill pid=%d: %s", pid, e)

    # 2. For each active member: dedupe (keep lowest PID) and start if missing.
    current_pids_by_member = {}
    for suffix in active_members:
        pids = sorted(running.get(suffix, []))
        if len(pids) > 1:
            for dup_pid in pids[1:]:
                try:
                    psutil.Process(dup_pid).terminate()
                    actions.append(f"KILLED_DUP:{suffix}:pid={dup_pid}")
                except Exception:
                    pass
            pids = pids[:1]
        current_pids_by_member[suffix] = pids

        if not pids:
            ok = _start_stage(suffix)
            actions.append(f"STARTED:{suffix}:{'ok' if ok else 'failed_or_cooldown'}")
            if not ok:
                _consecutive_failures += 1
                if _consecutive_failures >= 3:
                    _send_email(
                        "\U0001F6A8 bars_fetch_watchdog: repeated restart failure",
                        f"Stage {suffix} has failed to (re)start {_consecutive_failures}x.\n"
                        f"Check data/bars{suffix}_run_err.log manually.\n\n"
                        f"Time: {datetime.now(timezone.utc).isoformat()}"
                    )
                    _consecutive_failures = 0

    # 3. Stall + bogus-progress checks, per active member.
    for suffix in active_members:
        current_pids = current_pids_by_member[suffix]
        done, target, updated_at = _stage_progress(suffix)
        if updated_at:
            ts = datetime.fromisoformat(updated_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > STALE_THRESHOLD and current_pids:
                actions.append(f"STALE:{suffix}:{age:.0f}s")
                log.warning("Stage %s stale: last DB write %.0fs ago (process alive, may be pacing-limited)",
                            suffix, age)
                if age > STALE_THRESHOLD * 3:
                    _send_email(
                        "⚠️ bars_fetch_watchdog: stage stalled",
                        f"Stage {suffix} ({done}/{target} chunks) has had no progress DB write for "
                        f"{age/60:.1f} minutes, but its process is still running (pid={current_pids[0]}).\n"
                        f"This can be normal under heavy IB pacing pressure — check data/bars{suffix}_run.log.\n\n"
                        f"Time: {datetime.now(timezone.utc).isoformat()}"
                    )

        # Defense-in-depth: the exact zero-bar false-finish pattern from the
        # 2026-07-23 incident (see _recent_row_is_bogus docstring). Shouldn't
        # happen anymore now that bars1s_fetcher.py fails fast instead — but
        # if it ever does, kill the stuck/misbehaving process so a fresh
        # restart's full traversal (no finished=1 skip) self-heals the row.
        bogus = _recent_row_is_bogus(suffix)
        if bogus and current_pids:
            for pid in current_pids:
                try:
                    psutil.Process(pid).terminate()
                    actions.append(f"KILLED_BOGUS_PROGRESS:{suffix}:{bogus}:pid={pid}")
                    log.warning("Stage %s: most recent row (%s) is finished with 0 bars — "
                                "killing pid=%d to force a clean restart", suffix, bogus, pid)
                except Exception:
                    pass

    # 4. Clean any stale locks for stages that aren't currently running.
    for suffix in QUEUE:
        if suffix not in running or not running[suffix]:
            if _clean_stale_lock(suffix):
                actions.append(f"STALE_LOCK_CLEANED:{suffix}")

    if not actions:
        _consecutive_failures = 0

    return actions, group, active_members


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    args = parser.parse_args()

    log.info("bars_fetch_watchdog started (interval=%ds, queue=%s)", CHECK_INTERVAL, QUEUE)
    print(f"[BARS_WATCHDOG] started — queue={QUEUE}, checking every {CHECK_INTERVAL}s", flush=True)

    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            actions, group, active_members = check_and_heal()
            if actions:
                for a in actions:
                    log.info("action: %s", a)
                    print(f"[BARS_WATCHDOG] {ts} {a}", flush=True)
            else:
                print(f"[BARS_WATCHDOG] {ts} ok — group={group} running={active_members}", flush=True)
        except Exception as e:
            log.error("Watchdog cycle error: %s", e)
            print(f"[BARS_WATCHDOG] {ts} ERROR: {e}", flush=True)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
