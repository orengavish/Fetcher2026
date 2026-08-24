"""
check_fetch_priority.py
Standalone assert-based self-check for the bug 3/11 fix in trader/fetch_priority.py
(no test framework in this repo). Run: python check_fetch_priority.py

Checks:
  1. The NEW path -- fetch_priority.get_priority_dates(), which loads CC2026's
     lib/db.py by file path and queries verified_trades in the real live
     galao.db -- returns a non-empty list[str] of YYYY-MM-DD dates (read-only).
  2. The OLD path -- cfg.paths.db -- is untouched: still points at its
     original stale Galgo2026 value, proving we didn't repoint it.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trader import fetch_priority
from lib.config_loader import get_config

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_new_path():
    dates = fetch_priority.get_priority_dates()
    assert isinstance(dates, list), f"expected list, got {type(dates)}"
    assert len(dates) > 0, "expected non-empty date list from live CC2026 galao.db"
    assert all(isinstance(d, str) and DATE_RE.match(d) for d in dates), \
        f"expected all entries to be YYYY-MM-DD strings, got: {dates[:5]}"
    print(f"[PASS] new path: get_priority_dates() -> {len(dates)} dates, "
          f"e.g. {dates[:3]}")


def check_old_path_untouched():
    cfg = get_config()
    db_path = str(Path(cfg.paths.db))
    expected = str(Path(r"C:\Projects\Galgo2026\june\data\galao.db"))
    assert db_path == expected, (
        f"cfg.paths.db changed! expected {expected!r}, got {db_path!r} -- "
        f"trader/config.yaml:70 must NOT be repointed (see bugs 3/11)"
    )
    print(f"[PASS] old path: cfg.paths.db still == {db_path!r} (untouched)")


if __name__ == "__main__":
    check_old_path_untouched()
    check_new_path()
    print("\nALL CHECKS PASSED")
