"""
validate_fetch.py
Post-fetch file validator for Galao tick data.

Checks each fetched CSV for:
  - Correct schema (expected columns present)
  - Non-empty (at least 1 row)
  - Price sanity (no zero/negative prices, no jumps > 50 points)
  - Timestamp monotonicity (rows in time order)
  - Row count vs prior days (flag if < 20% of median — thin session)

Writes validation results back to fetch_log (updates status to 'corrupt' and
sets error_msg if issues found). Leaves status='ok' rows with valid files alone.

Usage:
  python validate_fetch.py                        # validate all unflagged fetch_log rows
  python validate_fetch.py --symbol MES --date 2026-04-25
  python validate_fetch.py --backfill             # validate all existing history files
  python validate_fetch.py --self-test

Self-test:
  python validate_fetch.py --self-test
"""

import sys
import csv
import argparse
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("validate_fetch")

_TRADES_COLS  = {"time_ct", "time_utc", "price", "size", "symbol"}
_BIDASK_COLS  = {"time_ct", "time_utc", "bid_p", "bid_s", "ask_p", "ask_s", "symbol"}
_MAX_PRICE_JUMP = 50.0   # points — flag if consecutive prices differ by more
_THIN_THRESHOLD = 0.20   # fraction of median — flag if row count < 20% of median


# ── Core validation ────────────────────────────────────────────────────────────

def validate_file(path: Path, file_type: str) -> tuple[bool, str | None]:
    """
    Validate a single CSV file.
    Returns (ok: bool, error_msg: str | None).
    """
    if not path.exists():
        return False, "file not found"

    expected_cols = _TRADES_COLS if file_type == "trades" else _BIDASK_COLS

    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return False, "empty file (no header)"
            missing = expected_cols - set(reader.fieldnames)
            if missing:
                return False, f"missing columns: {sorted(missing)}"
            rows = list(reader)
    except Exception as e:
        return False, f"read error: {e}"

    if not rows:
        return False, "empty file (no data rows)"

    issues = []

    if file_type == "trades":
        prices = []
        prev_ts = None
        for i, r in enumerate(rows):
            try:
                p = float(r["price"])
            except (ValueError, KeyError):
                issues.append(f"bad price at row {i+2}")
                continue
            if p <= 0:
                issues.append(f"zero/negative price at row {i+2}: {p}")
            prices.append(p)

            ts = r.get("time_utc", "")
            if prev_ts and ts < prev_ts:
                issues.append(f"timestamp not monotonic at row {i+2}")
                break   # one violation is enough to flag
            prev_ts = ts

        for i in range(1, len(prices)):
            jump = abs(prices[i] - prices[i-1])
            if jump > _MAX_PRICE_JUMP:
                issues.append(f"price jump {jump:.2f} pts at row {i+2}")
                break

    else:  # bidask
        prev_ts = None
        for i, r in enumerate(rows):
            try:
                bid = float(r["bid_p"])
                ask = float(r["ask_p"])
            except (ValueError, KeyError):
                issues.append(f"bad bid/ask at row {i+2}")
                continue
            if bid > ask:
                issues.append(f"inverted spread at row {i+2}: bid={bid} ask={ask}")
                break
            ts = r.get("time_utc", "")
            if prev_ts and ts < prev_ts:
                issues.append(f"timestamp not monotonic at row {i+2}")
                break
            prev_ts = ts

    if issues:
        return False, "; ".join(issues[:3])  # cap at 3 issues in error_msg
    return True, None


def _row_count(path: Path) -> int:
    """Count data rows (excluding header) in a CSV."""
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


def _median_row_count(symbol: str, file_type: str, output_dir: Path,
                      exclude_date: str, lookback: int = 20) -> int:
    """Compute median row count over the last N files for a symbol/type."""
    counts = []
    suffix = "trades" if file_type == "trades" else "bidask"
    files = sorted(output_dir.glob(f"{symbol}_{suffix}_????????.csv"), reverse=True)
    for f in files:
        date_part = f.stem.split("_")[-1]
        d_str = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:]}"
        if d_str == exclude_date:
            continue
        c = _row_count(f)
        if c > 0:
            counts.append(c)
        if len(counts) >= lookback:
            break
    if not counts:
        return 0
    counts.sort()
    mid = len(counts) // 2
    return counts[mid]


# ── Validate one entry ─────────────────────────────────────────────────────────

def validate_and_update(symbol: str, date_str: str, file_type: str,
                        output_dir: Path, db_path: Path) -> dict:
    """
    Validate the file for this symbol/date/type and update fetch_log.
    Returns a result dict with 'ok', 'row_count', 'error'.
    """
    date_compact = date_str.replace("-", "")
    suffix = "trades" if file_type == "trades" else "bidask"
    path = output_dir / f"{symbol}_{suffix}_{date_compact}.csv"

    ok, error = validate_file(path, file_type)

    row_count = _row_count(path)
    thin_warning = None

    if ok and row_count > 0:
        median = _median_row_count(symbol, file_type, output_dir, date_str)
        if median > 0 and row_count < median * _THIN_THRESHOLD:
            thin_warning = (f"thin session: {row_count:,} rows vs "
                            f"median {median:,} ({row_count/median:.0%})")

    final_error = error or thin_warning

    with get_db(db_path) as con:
        if not ok:
            con.execute(
                "UPDATE fetch_log SET status='corrupt', error_msg=?"
                " WHERE symbol=? AND date=? AND file_type=?"
                " AND status='ok'",
                (error, symbol, date_str, file_type)
            )
            log.warning(f"CORRUPT {symbol} {file_type} {date_str}: {error}")
        elif thin_warning:
            con.execute(
                "UPDATE fetch_log SET error_msg=?"
                " WHERE symbol=? AND date=? AND file_type=?"
                " AND status='ok' AND (error_msg IS NULL OR error_msg='')",
                (thin_warning, symbol, date_str, file_type)
            )
            log.warning(f"THIN {symbol} {file_type} {date_str}: {thin_warning}")
        else:
            log.info(f"OK {symbol} {file_type} {date_str}: {row_count:,} rows")

    return {"ok": ok, "row_count": row_count, "error": final_error}


# ── Run modes ──────────────────────────────────────────────────────────────────

def validate_all_pending(db_path: Path, output_dir: Path):
    """Validate all fetch_log rows with status='ok' that haven't been validated yet."""
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT DISTINCT symbol, date, file_type FROM fetch_log"
            " WHERE status='ok'"
        ).fetchall()

    log.info(f"Validating {len(rows)} fetch_log entries with status=ok")
    ok_count = err_count = 0
    for r in rows:
        result = validate_and_update(r["symbol"], r["date"], r["file_type"],
                                     output_dir, db_path)
        if result["ok"]:
            ok_count += 1
        else:
            err_count += 1
    log.info(f"Validation complete: {ok_count} OK, {err_count} issues")


def backfill_history(db_path: Path, output_dir: Path):
    """Validate all existing CSV files in history/ and insert fetch_log entries if missing."""
    from lib.db import insert_fetch_log
    log.info(f"Backfill: scanning {output_dir}")
    csv_files = sorted(output_dir.glob("*.csv"))
    log.info(f"Found {len(csv_files)} files")

    for path in csv_files:
        # Filename format: {SYMBOL}_{trades|bidask}_{YYYYMMDD}.csv
        stem = path.stem                              # e.g. "MES_trades_20260427"
        parts = stem.rsplit("_", 1)                   # ["MES_trades", "20260427"]
        if len(parts) != 2:
            continue
        prefix, date_compact = parts[0], parts[1]    # "MES_trades", "20260427"

        file_type = None
        for ft in ("trades", "bidask"):
            if prefix.endswith(f"_{ft}"):
                symbol = prefix[: -len(f"_{ft}")]
                file_type = ft
                break
        if file_type is None:
            continue

        if len(date_compact) != 8:
            continue
        date_str = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"

        with get_db(db_path) as con:
            existing = con.execute(
                "SELECT id FROM fetch_log WHERE symbol=? AND date=? AND file_type=?",
                (symbol, date_str, file_type)
            ).fetchone()
            if not existing:
                row_count = _row_count(path)
                insert_fetch_log(con, symbol, date_str, file_type,
                                 "ok", rows_fetched=row_count)

        validate_and_update(symbol, date_str, file_type, output_dir, db_path)


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"
            output_dir = tmp_path / "history"
            output_dir.mkdir()
            init_db(db_path)

            # 1. Valid trades file
            good = output_dir / "MES_trades_20260407.csv"
            with open(good, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
                for i in range(100):
                    w.writerow([f"2026-04-07T09:{i:02d}:00-05:00",
                                 f"2026-04-07T14:{i:02d}:00+00:00",
                                 6500.0 + i * 0.25, 5, "MESM6"])
            ok, err = validate_file(good, "trades")
            assert ok, f"Good file failed: {err}"

            # 2. File with inverted price jump
            bad_jump = output_dir / "MES_trades_20260408.csv"
            with open(bad_jump, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
                w.writerow(["2026-04-08T09:00:00-05:00", "2026-04-08T14:00:00+00:00",
                             6500.0, 5, "MESM6"])
                w.writerow(["2026-04-08T09:00:01-05:00", "2026-04-08T14:00:01+00:00",
                             6600.0, 5, "MESM6"])  # 100pt jump
            ok, err = validate_file(bad_jump, "trades")
            assert not ok, "Price jump should have been flagged"
            assert "jump" in err

            # 3. File with wrong schema
            bad_schema = output_dir / "MES_trades_20260409.csv"
            with open(bad_schema, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "price"])  # missing cols
                w.writerow(["2026-04-09T09:00:00", "6500"])
            ok, err = validate_file(bad_schema, "trades")
            assert not ok, "Wrong schema should fail"
            assert "missing columns" in err

            # 4. Empty file
            empty = output_dir / "MES_trades_20260410.csv"
            with open(empty, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
            ok, err = validate_file(empty, "trades")
            assert not ok, "Empty file should fail"

            # 5. Non-monotonic timestamps
            bad_ts = output_dir / "MES_trades_20260411.csv"
            with open(bad_ts, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
                w.writerow(["2026-04-11T09:01:00-05:00", "2026-04-11T14:01:00+00:00",
                             6500.0, 5, "MESM6"])
                w.writerow(["2026-04-11T09:00:00-05:00", "2026-04-11T14:00:00+00:00",
                             6500.25, 5, "MESM6"])  # earlier than previous
            ok, err = validate_file(bad_ts, "trades")
            assert not ok, "Non-monotonic timestamps should fail"
            assert "monotonic" in err

            # 6. Valid bidask file
            good_ba = output_dir / "MES_bidask_20260407.csv"
            with open(good_ba, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "bid_p", "bid_s", "ask_p", "ask_s", "symbol"])
                for i in range(50):
                    w.writerow([f"2026-04-07T09:{i:02d}:00-05:00",
                                 f"2026-04-07T14:{i:02d}:00+00:00",
                                 6500.0, 10, 6500.25, 10, "MESM6"])
            ok, err = validate_file(good_ba, "bidask")
            assert ok, f"Good bidask file failed: {err}"

        print("[self-test] validate_fetch: PASS")
        return True

    except Exception as e:
        print(f"[self-test] validate_fetch: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate fetched tick data files")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--date",   default=None, help="YYYY-MM-DD")
    parser.add_argument("--backfill", action="store_true",
                        help="Validate all existing files in history/")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    try:
        db_path = Path(cfg.paths.db)
        output_dir = Path(cfg.paths.history)
    except Exception:
        db_path = Path("data/galao.db")
        output_dir = Path("data/history")

    init_db(db_path)

    if args.backfill:
        backfill_history(db_path, output_dir)
    elif args.symbol and args.date:
        for ft in ("trades", "bidask"):
            result = validate_and_update(args.symbol.upper(), args.date, ft,
                                         output_dir, db_path)
            print(f"{args.symbol} {ft} {args.date}: "
                  f"{'OK' if result['ok'] else 'FAIL'} "
                  f"rows={result['row_count']} "
                  f"{'err='+result['error'] if result['error'] else ''}")
    else:
        validate_all_pending(db_path, output_dir)
