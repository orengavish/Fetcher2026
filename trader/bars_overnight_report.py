"""
trader/bars_overnight_report.py  v1.0

Morning-after benchmark report for an unattended bars_fetch_watchdog.py run.
Compares current state against the baseline snapshot taken when the run
started (data/bars_overnight_baseline.json), and summarizes:

  - Real bars/chunks added per bar size since the baseline
  - Actual time-share per stage vs its target WEIGHTS (from
    bars_watchdog_schedule.json) — did the priority split actually hold?
  - How many (symbol, date, chunk) combos got skipped after repeated
    failures (data/bars{N}s_chunk_failures.json) — the tail-chunk pattern
    and any others
  - Watchdog restart count (crash frequency) per stage, from the log
  - A plain health verdict

This is the "benchmark for next night" the 2026-07-23 stabilization pass
(BARS1S_STATUS.md §0j) was built to support — run it each morning to see
what actually happened and tune WEIGHTS / MIN_QUANTUM_S / thresholds for the
next night accordingly.

Usage:
  python trader/bars_overnight_report.py
"""

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from trader.bars_fetch_watchdog import STAGE_CONFIG, WEIGHTS  # noqa: E402

_DATA = _ROOT / "data"
_BASELINE_FILE = _DATA / "bars_overnight_baseline.json"
_SCHEDULE_FILE = _DATA / "bars_watchdog_schedule.json"


def _stage_stats(suffix: str):
    cfg = STAGE_CONFIG[suffix]
    target = cfg["chunks_per_day"] * cfg["days"] * cfg["symbols"]
    db = _DATA / f"bars{suffix}_progress.db"
    if not db.exists():
        return {"bars_total": 0, "chunks_done_total": 0, "pairs_finished": 0, "target_total_chunks": target}
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT COALESCE(SUM(bars_fetched),0), COALESCE(SUM(chunks_done),0), "
        "COALESCE(SUM(finished),0) FROM progress"
    ).fetchone()
    conn.close()
    return {"bars_total": row[0], "chunks_done_total": row[1], "pairs_finished": row[2],
             "target_total_chunks": target}


def _crash_summary(suffix: str):
    f = _DATA / f"bars{suffix}_chunk_failures.json"
    try:
        counts = json.loads(f.read_text())
    except Exception:
        return {"distinct_stuck_chunks": 0, "total_failures": 0, "top": []}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return {
        "distinct_stuck_chunks": len(counts),
        "total_failures": sum(counts.values()),
        "top": top,
    }


def _restart_count(suffix: str) -> int:
    """Count 'Started stage {suffix}:' lines in the watchdog log — a proxy
    for how many times this stage's process was (re)launched overnight."""
    log = _DATA / "bars_fetch_watchdog_run.log"
    if not log.exists():
        return 0
    pattern = re.compile(rf"Started stage {re.escape(suffix)}:")
    try:
        text = log.read_text(errors="ignore")
    except Exception:
        return 0
    return len(pattern.findall(text))


def main():
    if not _BASELINE_FILE.exists():
        print(f"No baseline found at {_BASELINE_FILE} — can't compute deltas, "
              f"showing absolute current state only.\n")
        baseline = {}
    else:
        baseline = json.loads(_BASELINE_FILE.read_text())
        print(f"Baseline snapshot: {baseline.get('_snapshot_at', '?')}")

    now = datetime.now(timezone.utc)
    print(f"Report generated : {now.isoformat()}\n")

    schedule = {}
    if _SCHEDULE_FILE.exists():
        try:
            schedule = json.loads(_SCHEDULE_FILE.read_text())
        except Exception:
            pass
    time_spent = schedule.get("time_spent", {})
    total_time = sum(time_spent.values()) or 1

    print(f"{'Stage':<6}{'Bars added':>14}{'Chunks now':>14}{'Pairs done':>12}"
          f"{'Time %':>9}{'Target %':>10}{'Restarts':>10}{'Stuck chunks':>14}")
    print("-" * 92)

    for suffix in STAGE_CONFIG:
        cur = _stage_stats(suffix)
        base = baseline.get(suffix, {})
        bars_added = cur["bars_total"] - base.get("bars_total", 0)
        chunks_added = cur["chunks_done_total"] - base.get("chunks_done_total", 0)
        pairs_added = cur["pairs_finished"] - base.get("pairs_finished", 0)
        actual_pct = 100 * time_spent.get(suffix, 0) / total_time
        target_pct = 100 * WEIGHTS.get(suffix, 0)
        restarts = _restart_count(suffix)
        crashes = _crash_summary(suffix)

        print(f"{suffix:<6}{bars_added:>14,}{chunks_added:>+14,}{pairs_added:>+12,}"
              f"{actual_pct:>8.1f}%{target_pct:>9.1f}%{restarts:>10}{crashes['distinct_stuck_chunks']:>14}")

    print()
    for suffix in STAGE_CONFIG:
        crashes = _crash_summary(suffix)
        if crashes["top"]:
            print(f"{suffix} — most-failed chunks (key = symbol|date|chunk_index, count):")
            for key, count in crashes["top"]:
                flag = ""
                m = re.match(r"^(.+)\|(.+)\|(\d+)$", key)
                if m:
                    sym, date_str, idx = m.groups()
                    cfg = STAGE_CONFIG[suffix]
                    total_chunks_est = cfg["chunks_per_day"]
                    if int(idx) >= total_chunks_est - 2:
                        flag = "  [tail chunk — known IB recent-session quirk]"
                print(f"    {key}: {count} failures{flag}")
            print()

    print("Verdict:")
    for suffix in STAGE_CONFIG:
        actual_pct = 100 * time_spent.get(suffix, 0) / total_time
        target_pct = 100 * WEIGHTS.get(suffix, 0)
        drift = actual_pct - target_pct
        note = "on target" if abs(drift) < 10 else ("got MORE time than planned" if drift > 0 else "got LESS time than planned")
        print(f"  {suffix}: {note} ({actual_pct:.1f}% actual vs {target_pct:.1f}% target)")

    print("\nTuning ideas for tomorrow night — adjust in trader/bars_fetch_watchdog.py:")
    print("  - WEIGHTS: change the 80/10/10 split if a stage needs more/less priority")
    print("  - MIN_QUANTUM_S: shorter = more responsive rebalancing, more process churn")
    print("  - _MAX_CHUNK_CRASHES / is_tail_chunk logic in bars1s_fetcher.py: if new stuck")
    print("    chunk patterns show up outside the last-2 tail position, consider widening")
    print("    which positions get the fast-skip treatment")


if __name__ == "__main__":
    main()
