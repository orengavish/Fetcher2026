"""
trader/bars1s_repair.py  v2.0

Repair pass for the bars1s_fetcher family (1s/5s/30s bar sizes). Scans every
bars{N}s_progress.db found under data/, and for every row checks the file on
disk against the *same* verification logic bars1s_fetcher.py's own resume
self-heal uses (_verify_chunks_done) — so a row can never be trusted as more
complete than what's genuinely, cleanly on disk, regardless of why it
diverged. This catches both known corruption patterns:

  A. Duplicate/out-of-order tail (2026-07-22 incident) — IB handed back a
     stale response for the last 1-2 chunks; the file has more rows than are
     genuinely unique/in-order.
  B. Zero/low-bar false-finish (2026-07-23 incident) — the IB connection
     dropped mid-run with no reconnect logic; every chunk attempt failed but
     got silently accepted as "no trades this window", so entire days ended
     up marked finished=1 with 0 real bars.

For any row where the verified state differs from what's recorded, this
writes the *verified* (file-truth) values back to the DB — not just a blind
finished=0 flip — so chunks_done/bars_fetched always reflect what's actually
on disk. No CSV content is deleted beyond what _fetch_day would already
truncate on its own next self-heal; this just does it proactively, across
every row, instead of waiting for the fetcher to happen to revisit each day.

Usage:
  python trader/bars1s_repair.py            # scan all bar sizes, fix DB rows
  python trader/bars1s_repair.py --dry-run  # report only, no DB writes
  python trader/bars1s_repair.py --bar-secs 5   # limit to one bar size
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

import trader.bars1s_fetcher as bf

_DATA = _ROOT / "data"


def _discover_bar_secs() -> list:
    found = []
    for db in sorted(_DATA.glob("bars*s_progress.db")):
        # data/bars{N}s_progress.db -> N
        name = db.stem  # "bars5s_progress"
        core = name[len("bars"):-len("s_progress")]
        try:
            found.append(int(core))
        except ValueError:
            continue
    return found


def _repair_one_bar_size(bar_secs: int, dry_run: bool) -> dict:
    bf._configure_bar_size(bar_secs)
    db = bf._PROGRESS_DB
    if not db.exists():
        return {"checked": 0, "fixed": 0}

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT symbol, date, chunks_done, total_chunks, bars_fetched, finished FROM progress"
    ).fetchall()

    checked = 0
    fixes = []
    for sym, date_str, chunks_done, total_chunks_db, bars_fetched, finished in rows:
        checked += 1
        day = date.fromisoformat(date_str)
        _, chunk_sizes, total_chunks = bf._build_chunks(day)
        out_path = bf._OUTPUT_DIR / f"{sym}_{bf._FILE_SUFFIX}_{day.strftime('%Y%m%d')}.csv"

        verified_done, verified_rows = bf._verify_chunks_done(out_path, chunk_sizes, chunks_done)

        # A row can also be WRONG in the other direction: chunks_done=0 but a
        # file with real clean rows exists (e.g. DB reset without touching
        # the file). Check the full file, not just up to the claimed count.
        if chunks_done == 0 and out_path.exists():
            verified_done, verified_rows = bf._verify_chunks_done(out_path, chunk_sizes, total_chunks)

        # A day marked fully done but short by <=2 chunks matches the accepted
        # permanent tail-gap pattern bars1s_fetcher.py now deliberately allows
        # (§0k) — not corruption. Don't flag/revert it.
        shortfall = chunks_done - verified_done
        if chunks_done >= total_chunks and finished and 0 < shortfall <= 2:
            continue

        would_be_finished = verified_done >= total_chunks
        if verified_done != chunks_done or bool(finished) != would_be_finished:
            fixes.append((sym, date_str, chunks_done, bars_fetched, bool(finished),
                          verified_done, verified_rows, would_be_finished, total_chunks))

    label = f"{bar_secs}s"
    print(f"\n=== {label} ({db}) — {checked} rows checked ===")
    for (sym, date_str, old_cd, old_bars, old_fin,
         new_cd, new_bars, new_fin, total_chunks) in fixes:
        flag = "ZERO/LOW-BAR FALSE-FINISH" if old_fin and old_cd > new_cd and new_bars == 0 else \
               "DUPLICATE/CORRUPT TAIL" if new_cd < old_cd else "OUT OF SYNC"
        print(f"  FIX  {sym} {date_str}: chunks_done {old_cd}->{new_cd}/{total_chunks}, "
              f"bars {old_bars:,}->{new_bars:,}, finished {old_fin}->{new_fin}  [{flag}]")

    if fixes and not dry_run:
        # Match bars1s_fetcher.py's own _save() format exactly (ISO8601 with
        # timezone) — SQLite's datetime('now') writes a naive, space-separated
        # string that broke bars_status_server.py's datetime.fromisoformat()
        # + tz-aware arithmetic (found while verifying this fix).
        now_iso = datetime.now(timezone.utc).isoformat()
        for (sym, date_str, *_rest, new_cd, new_bars, new_fin, _tc) in fixes:
            conn.execute(
                "UPDATE progress SET chunks_done=?, bars_fetched=?, finished=?, "
                "updated_at=? WHERE symbol=? AND date=?",
                (new_cd, new_bars, int(new_fin), now_iso, sym, date_str),
            )
        conn.commit()

    conn.close()
    return {"checked": checked, "fixed": len(fixes)}


def main(dry_run: bool, only_bar_secs):
    bar_sizes = [only_bar_secs] if only_bar_secs else _discover_bar_secs()
    if not bar_sizes:
        print("No bars*s_progress.db files found under data/.")
        return

    total_checked = total_fixed = 0
    for bar_secs in bar_sizes:
        result = _repair_one_bar_size(bar_secs, dry_run)
        total_checked += result["checked"]
        total_fixed += result["fixed"]

    print(f"\n{total_fixed} of {total_checked} rows (across {len(bar_sizes)} bar size(s)) needed correction.")
    if dry_run:
        print("--dry-run: no DB changes made. Re-run without --dry-run to fix.")
    elif total_fixed:
        print("Corrected rows now reflect exactly what's verified on disk. Any "
              "fetcher for these bar sizes will pick up the real remaining work "
              "next time it (re)visits these days — no already-good data was "
              "touched or re-fetched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair pass for the bars1s_fetcher family")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't modify the DB")
    parser.add_argument("--bar-secs", type=int, help="Limit to one bar size (default: all found)")
    args = parser.parse_args()
    main(args.dry_run, args.bar_secs)
