"""
trader/bars1s_repair.py  v1.0

One-time repair pass for the duplicate-tail bug fixed in bars1s_fetcher.py
(see JULY_1S_DATA_HANDOFF.md / BARS1S_STATUS.md for the writeup).

Scans every data/bars1s/*.csv file already on disk. For any file with a
corrupted tail (out-of-order/duplicate timestamps — i.e. fewer "clean" rows
than the file actually contains), it flips that (symbol, date) row's
`finished` flag back to 0 in the progress DB. It does NOT touch the CSV
files or chunks_done itself — bars1s_fetcher.py's `_fetch_day` now self-heals
(truncates the corrupt tail and re-fetches it) the next time it visits that
day, since it no longer trusts chunks_done blindly.

Usage:
  python trader/bars1s_repair.py           # scan + report + fix DB flags
  python trader/bars1s_repair.py --dry-run # scan + report only, no DB writes
"""

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from trader.bars1s_fetcher import _scan_clean_prefix, _OUTPUT_DIR, _PROGRESS_DB


def _total_rows(path: Path) -> int:
    with open(path, "r", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)   # minus header


def main(dry_run: bool):
    conn = sqlite3.connect(str(_PROGRESS_DB))
    files = sorted(_OUTPUT_DIR.glob("*_1s_*.csv"))
    print(f"Scanning {len(files)} files in {_OUTPUT_DIR} ...\n")

    corrupted = []
    for f in files:
        sym, _, datepart = f.stem.split("_")
        date_str = f"{datepart[0:4]}-{datepart[4:6]}-{datepart[6:8]}"
        total = _total_rows(f)
        clean = _scan_clean_prefix(f)
        if clean < total:
            corrupted.append((sym, date_str, f, clean, total))
            print(f"  CORRUPT  {sym} {date_str}: {clean:,} clean / {total:,} rows "
                  f"({total - clean:,} bogus rows in tail)")

    print(f"\n{len(corrupted)} of {len(files)} files have a corrupted tail.")

    if not corrupted:
        conn.close()
        return

    if dry_run:
        print("\n--dry-run: no DB changes made. Re-run without --dry-run to fix.")
        conn.close()
        return

    for sym, date_str, f, clean, total in corrupted:
        conn.execute(
            "UPDATE progress SET finished=0 WHERE symbol=? AND date=?",
            (sym, date_str),
        )
    conn.commit()
    conn.close()
    print(f"\nFlagged {len(corrupted)} (symbol, date) pairs as finished=0.")
    print("Restart bars1s_fetcher.py — it will revisit these, self-heal the")
    print("truncated tail (via the fix in _fetch_day), and fetch only the")
    print("genuinely missing chunks. No already-verified-good data is re-fetched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair pass for the bars1s duplicate-tail bug")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't modify the DB")
    args = parser.parse_args()
    main(args.dry_run)
