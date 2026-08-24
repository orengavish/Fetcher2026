"""
fetch_priority.py
Show which dates have verified trades but missing tick files — fetch these first.

Usage:
  python fetch_priority.py
  python fetch_priority.py --all    # also show dates with tick files already present
"""

import sys
import argparse
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config

# Bugs 3 & 11: cfg.paths.db is Fetcher2026's OWN progress-tracking DB location
# (fetch_progress.db is also derived from its parent elsewhere in this repo) --
# it is NOT the live trading DB and must stay untouched. The real, actively-written
# galao.db (verified_trades) lives in CriticalCorallations2026. This is intentionally
# the ONE place in Fetcher2026 allowed to hardcode CC2026's real DB path; every other
# cfg.paths.db consumer in this repo must keep using cfg.paths.db as-is.
_CC2026_LIB_DB   = Path(r"C:\Projects\CriticalCorallations2026\lib\db.py")
_CC2026_GALAO_DB = Path(r"C:\Projects\CriticalCorallations2026\trader\data\galao.db")


def _load_cc2026_db():
    """
    Load CC2026's lib/db.py by explicit file path under a unique module name.
    Fetcher2026 has its own lib/db.py at the same dotted path (lib.db) -- a plain
    `sys.path.insert(...) ; import lib.db` would silently return whichever lib.db
    is already cached in sys.modules (Fetcher2026's own) instead of CC2026's, with
    no error. spec_from_file_location + a distinct module name avoids that collision.
    """
    spec = importlib.util.spec_from_file_location("cc2026_lib_db", _CC2026_LIB_DB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_priority_dates() -> list:
    """Priority-ordered (most verified trades first) dates, read-only, from CC2026's live DB."""
    cc_db = _load_cc2026_db()
    with cc_db.get_db(_CC2026_GALAO_DB) as con:
        return cc_db.get_priority_dates(con)


def run(show_all: bool = False):
    cfg = get_config()
    try:
        history_dir = Path(cfg.paths.history)
        symbols     = list(cfg.symbols)
    except Exception:
        history_dir = Path("data/history")
        symbols     = []

    # files already on disk
    present = set()
    if history_dir.exists():
        for f in history_dir.glob("*.csv"):
            parts = f.stem.split("_")
            if len(parts) < 3: continue
            sym, ftype, dc = parts[0], parts[1], parts[2]
            if not dc.isdigit() or len(dc) != 8:
                continue  # skip live/rolling files
            if f.stat().st_size > 100:
                date_s = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                present.add((sym, date_s, ftype))

    def has_trades(sym, date_s):
        return (sym, date_s, "trades") in present

    def has_bidask(sym, date_s):
        return (sym, date_s, "bidask") in present

    dates = get_priority_dates()  # already priority-ordered by CC2026 (most trades first)

    if not dates:
        print("No verified trades in DB.")
        return

    # get_priority_dates() returns dates only (deduplicated across symbols), not
    # per-symbol/count/pnl breakdown -- so pairs are (date, tracked-symbol), one
    # per symbol in cfg.symbols, in the date's priority order. VT/TP/SL/PNL counts
    # are no longer available from this source (they lived in the old direct query).
    pairs   = [(d, sym) for d in dates for sym in symbols]
    tier1   = [(d, s) for d, s in pairs if has_trades(s, d) and not has_bidask(s, d)]
    tier2   = [(d, s) for d, s in pairs if not has_trades(s, d)]
    covered = [(d, s) for d, s in pairs if has_trades(s, d) and has_bidask(s, d)]

    def _print_rows(data):
        if not data:
            print("  (none)")
            return
        for d, s in data:
            t = "T" if has_trades(s, d) else "-"
            b = "B" if has_bidask(s, d) else "-"
            print(f"  {d}  {s:<5}  files:[{t}{b}]")

    print("\nFETCH PRIORITY — dates with verified trades, missing tick files")
    print("=" * 66)
    print(f"\n[TIER 1 — BID_ASK only, TRADES done] ({len(tier1)} pairs, date priority = trade count DESC)")
    _print_rows(tier1)
    print(f"\n[TIER 2 — full fetch needed]          ({len(tier2)} pairs, date priority = trade count DESC)")
    _print_rows(tier2)

    if show_all and covered:
        print(f"\n[COVERED — both TRADES+BID_ASK done]  ({len(covered)} pairs)")
        _print_rows(covered)

    print(f"\nTier 1: {len(tier1)}  |  Tier 2: {len(tier2)}  |  Covered: {len(covered)}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch priority list for verified trades")
    parser.add_argument("--all", action="store_true", help="Also show already-covered dates")
    args = parser.parse_args()
    run(show_all=args.all)
