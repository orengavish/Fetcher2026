"""
trader/bars1s_fetcher.py  v1.1

Fetches OHLCV TRADES bars for MES / MNQ / MYM / M2K from Interactive Brokers.
Bar size is configurable (--bar-secs); defaults to 1-second bars, 4 symbols,
252 trading days back (~1 year) — the original/current backfill.

IB constraint:  reqHistoricalData max request duration depends on bar size
                (see _BAR_SECS_TABLE): 1 secs->1800S, 5 secs->3600S,
                10/15 secs->14400S, 30 secs->28800S. IB also doesn't retain
                bars <=30 secs older than ~6 months — expect empty chunks
                for older dates at those bar sizes, not an error.
Strategy:       chunk each CME session into windows of the max allowed
                duration, fetch sequentially per symbol/day.
Pacing:         proactive rate limiter, default 15 req/10min PER PROCESS —
                deliberately conservative because multiple bar-size fetchers
                (1s/5s/30s) are expected to run concurrently against the same
                IB account, sharing one real 60-req/10-min pacing budget.
Memory:         buffer ≤ 1000 bars; flush + clear on every crossing.
Progress:       data/bars{Ns}_progress.db — resume after any crash or restart
                (separate DB/lock/output dir per bar size, so concurrent
                instances never collide on files).

Output:   data/bars{Ns}/{SYM}_{Ns}_{YYYYMMDD}.csv  (one file per symbol per day)
CSV cols: datetime_utc, open, high, low, close, volume

Usage:
  python trader/bars1s_fetcher.py                              # 1s, all 4 symbols, 252 days (default/original)
  python trader/bars1s_fetcher.py --bar-secs 5 --symbols MES,MNQ --days 42   # 5s bars, 2 symbols, ~2 months
  python trader/bars1s_fetcher.py --bar-secs 30 --days 252      # 30s bars, all 4 symbols, 1 year
  python trader/bars1s_fetcher.py --test 10m                   # run for 10 minutes then exit cleanly
  python trader/bars1s_fetcher.py --symbol MES                 # single symbol (legacy flag, still works)
  python trader/bars1s_fetcher.py --days 5                     # last 5 trading days only
  python trader/bars1s_fetcher.py --self-test                  # offline unit tests (no IB needed)
"""

import argparse
import csv
import os
import re
import signal
import sqlite3
import sys
import time
import threading
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from ib_insync import IB, Future

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("bars1s_fetcher")

CT  = ZoneInfo("America/Chicago")
UTC = timezone.utc

_SYMBOLS      = ["MES", "MNQ", "MYM", "M2K"]
_EXCHANGE_MAP = {"MES": "CME", "MNQ": "CME", "M2K": "CME", "MYM": "CBOT"}
_WHAT         = "TRADES"
_INTER_REQ_S  = 5.0         # minimum courtesy sleep between requests — widened 2026-07-22:
                            # requests spaced only 2s apart correlated with IB/ib_insync handing
                            # back a stale cached response instead of the requested window
                            # (caught by the response-validation guard in _fetch_day, but wider
                            # spacing reduces how often it happens in the first place)
_FLUSH_EVERY  = 1000        # max bars held in RAM before flushing to disk
_VERSION      = "1.1"

_ROOT_DATA    = _ROOT / "data"

# IB's max reqHistoricalData duration per bar size (durationStr upper bound).
# Also: IB doesn't retain bars <=30 secs older than ~6 months — expect empty
# (not erroring) chunks for older dates at these bar sizes.
_BAR_SECS_TABLE = {
    1:  ("1 secs",  1800),
    5:  ("5 secs",  3600),
    10: ("10 secs", 14400),
    15: ("15 secs", 14400),
    30: ("30 secs", 28800),
}

# Bar-size-dependent globals — set by _configure_bar_size(), defaults below
# match the original/current 1-second backfill for full backward compatibility.
_BAR_SIZE     = "1 secs"
_CHUNK_SECS   = 1800
_FILE_SUFFIX  = "1s"
_OUTPUT_DIR   = _ROOT_DATA / "bars1s"
_PROGRESS_DB  = _ROOT_DATA / "bars1s_progress.db"
_LOCK_FILE    = _ROOT_DATA / "bars1s_fetcher.lock"
_CRASH_FILE   = _ROOT_DATA / "bars1s_chunk_failures.json"


def _configure_bar_size(bar_secs: int):
    """Set all bar-size-dependent globals. Each bar size gets its own output
    dir / progress DB / lock file, so concurrent instances (e.g. 1s + 5s + 30s
    fetchers running at once) never collide on files."""
    global _BAR_SIZE, _CHUNK_SECS, _FILE_SUFFIX, _OUTPUT_DIR, _PROGRESS_DB, _LOCK_FILE, _CRASH_FILE
    if bar_secs not in _BAR_SECS_TABLE:
        raise ValueError(f"Unsupported --bar-secs {bar_secs}; choose one of {sorted(_BAR_SECS_TABLE)}")
    _BAR_SIZE, _CHUNK_SECS = _BAR_SECS_TABLE[bar_secs]
    _FILE_SUFFIX = f"{bar_secs}s"
    _OUTPUT_DIR  = _ROOT_DATA / f"bars{_FILE_SUFFIX}"
    _PROGRESS_DB = _ROOT_DATA / f"bars{_FILE_SUFFIX}_progress.db"
    _LOCK_FILE   = _ROOT_DATA / f"bars{_FILE_SUFFIX}_fetcher.lock"
    _CRASH_FILE  = _ROOT_DATA / f"bars{_FILE_SUFFIX}_chunk_failures.json"


# ── Crash-loop circuit breaker ────────────────────────────────────────────────
# A chunk that fails on every attempt raises (see _fetch_day) so the process
# restarts fresh rather than silently faking completion — correct, but found
# 2026-07-23 to crash-loop forever when IB simply can't serve one specific
# chunk no matter how many times reconnected (always the same end-of-session
# chunk). This persists a per-chunk failure count across restarts so that
# after _MAX_CHUNK_CRASHES process crashes on the *exact same* chunk, that day
# is skipped (left correctly incomplete, never faked) instead of blocking
# forever — see BARS1S_STATUS.md §0i.
#
# Lowered 3->1 for TAIL chunks the same night (§0j): by then the pattern had
# been confirmed as a systemic, ~100%-reproducible failure — the last 1-2
# chunks of the MOST RECENTLY completed session, across every bar size
# (1s/5s/30s) and every symbol — not a transient blip worth 3 restarts'
# patience for. A chunk anywhere else in the day still gets the more patient
# budget (see the is_tail_chunk check in _fetch_day) since a non-tail chunk
# failing this hard looks like a real outage rather than the known quirk,
# and shouldn't be given up on as fast.
_MAX_CHUNK_CRASHES = 1


def _load_crash_counts() -> dict:
    try:
        import json
        return json.loads(_CRASH_FILE.read_text())
    except Exception:
        return {}


def _record_chunk_crash(key: str) -> int:
    import json
    counts = _load_crash_counts()
    counts[key] = counts.get(key, 0) + 1
    try:
        _CRASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_FILE.write_text(json.dumps(counts))
    except Exception:
        pass
    return counts[key]


def _clear_chunk_crash(key: str):
    counts = _load_crash_counts()
    if key in counts:
        del counts[key]
        try:
            import json
            _CRASH_FILE.write_text(json.dumps(counts))
        except Exception:
            pass


# Shared stop flag — set by SIGINT handler or --test timer
_state = {"running": True, "stop_at": None}   # mutable dict avoids global-in-closure

# ── IB pacing rate limiter ────────────────────────────────────────────────────
_req_timestamps: deque = deque()
# Was 15, set when 3 bar-size fetchers ran concurrently (each needing a small
# share of the shared 60-req/10-min budget). Since §0d/§0e that's no longer
# true — bars_fetch_watchdog.py runs exactly ONE fetcher at a time — so 15 was
# needlessly strangling throughput: the overnight 2026-07-23/24 benchmark
# showed the active stage spending ~90% of its allotted run time asleep on
# this throttle with ZERO real pacing violations the whole night (see
# BARS1S_STATUS.md §0k). Raised to 40, still leaving real headroom out of 60
# for external IB consumers on this machine (back-trading/trading_dashboard.py
# etc.) — re-lower this if pacing violations start showing up for real.
#
# Overridable via --pace-max (§0l): 1s and 30s now run concurrently with each
# other (not with 5s), so they need to split a budget between just the two of
# them rather than each assuming they have the full 40 to themselves.
_PACE_MAX_REQS = 40
_PACE_WINDOW_S = 600   # 10-minute rolling window


def _throttle():
    """Sleep if needed to stay under IB pacing limit of 60 req / 10 min."""
    now    = time.time()
    cutoff = now - _PACE_WINDOW_S
    while _req_timestamps and _req_timestamps[0] < cutoff:
        _req_timestamps.popleft()
    if len(_req_timestamps) >= _PACE_MAX_REQS:
        wait = _req_timestamps[0] + _PACE_WINDOW_S - now + 1.0
        if wait > 0:
            log.info("Pacing: %d req in window - sleeping %.0fs", len(_req_timestamps), wait)
            time.sleep(wait)
        now    = time.time()
        cutoff = now - _PACE_WINDOW_S
        while _req_timestamps and _req_timestamps[0] < cutoff:
            _req_timestamps.popleft()
    _req_timestamps.append(time.time())


def _setup_signals():
    if threading.current_thread() is threading.main_thread():
        def _handler(sig, frame):
            _state["running"] = False
            log.warning("Interrupt received — stopping after current chunk")
        signal.signal(signal.SIGINT, _handler)


def _ok() -> bool:
    """Return True while we should keep fetching."""
    if not _state["running"]:
        return False
    if _state["stop_at"] and time.time() >= _state["stop_at"]:
        _state["running"] = False
        log.info("--test duration elapsed - stopping cleanly")
        return False
    return True


# ── Session window ────────────────────────────────────────────────────────────

def _session_bounds(day: date):
    """Return (start_utc, end_utc) for the CME Globex session covering 'day'.
    Convention: prev_day 17:00 CT → day 17:00 CT (matches existing fetcher.py).
    """
    prev  = day - timedelta(days=1)
    start = datetime(prev.year, prev.month, prev.day, 17, 0, 0, tzinfo=CT)
    end   = datetime(day.year,  day.month,  day.day,  17, 0, 0, tzinfo=CT)
    return start.astimezone(UTC), end.astimezone(UTC)


# ── Working days ──────────────────────────────────────────────────────────────

def _working_days(n: int) -> list:
    """Return last n Mon-Fri days before today, most-recent first."""
    days = []
    d = (datetime.now(CT) - timedelta(days=1)).date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


# ── Contract resolution ───────────────────────────────────────────────────────

def _get_contract(ib: IB, symbol: str, day: date) -> Future:
    exchange = _EXCHANGE_MAP.get(symbol, "CME")
    day_str  = day.strftime("%Y%m%d")
    con = Future(symbol=symbol, exchange=exchange, currency="USD")
    con.includeExpired = True
    details = ib.reqContractDetails(con)
    if not details:
        raise ValueError(f"No contract details for {symbol}")
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    for det in details:
        if det.contract.lastTradeDateOrContractMonth >= day_str:
            c = det.contract
            ib.qualifyContracts(c)
            return c
    raise ValueError(f"No contract for {symbol} on {day}")


# ── Progress DB ───────────────────────────────────────────────────────────────

def _init_db() -> sqlite3.Connection:
    _PROGRESS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_PROGRESS_DB), timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            symbol       TEXT,
            date         TEXT,
            chunks_done  INTEGER DEFAULT 0,
            total_chunks INTEGER DEFAULT 0,
            bars_fetched INTEGER DEFAULT 0,
            finished     INTEGER DEFAULT 0,
            updated_at   TEXT,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.commit()
    return conn


def _is_done(conn: sqlite3.Connection, sym: str, date_str: str) -> bool:
    row = conn.execute(
        "SELECT finished FROM progress WHERE symbol=? AND date=?", (sym, date_str)
    ).fetchone()
    return bool(row and row[0])


def _chunks_done(conn: sqlite3.Connection, sym: str, date_str: str) -> int:
    row = conn.execute(
        "SELECT chunks_done FROM progress WHERE symbol=? AND date=?", (sym, date_str)
    ).fetchone()
    return row[0] if row else 0


def _save(conn: sqlite3.Connection, sym: str, date_str: str,
          cd: int, tc: int, bars: int, done: bool):
    conn.execute("""
        INSERT INTO progress (symbol, date, chunks_done, total_chunks,
                              bars_fetched, finished, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(symbol, date) DO UPDATE SET
            chunks_done=excluded.chunks_done,
            total_chunks=excluded.total_chunks,
            bars_fetched=excluded.bars_fetched,
            finished=excluded.finished,
            updated_at=excluded.updated_at
    """, (sym, date_str, cd, tc, bars, int(done),
          datetime.now(UTC).isoformat()))
    conn.commit()


# ── Bar-date parser ───────────────────────────────────────────────────────────

def _bar_dt(bar) -> datetime | None:
    """Parse bar.date to UTC datetime. IB returns unix epoch string (formatDate=2)."""
    try:
        return datetime.fromtimestamp(int(bar.date), tz=UTC)
    except (ValueError, TypeError):
        pass
    try:
        d = bar.date
        if isinstance(d, datetime):
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(d)).replace(tzinfo=UTC)
    except Exception:
        return None


# ── Fetch one day ─────────────────────────────────────────────────────────────

def _scan_clean_prefix(path: Path) -> int:
    """
    Count how many leading data rows of an existing CSV form a strictly
    increasing, duplicate-free timestamp sequence. Anything from the first
    out-of-order/duplicate row onward is treated as corrupt (e.g. left over
    from a resume that re-fetched chunks already on disk) and is not trusted.
    """
    clean_rows = 0
    prev_dt = None
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)   # header
            for row in reader:
                try:
                    dt = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                except Exception:
                    break
                if prev_dt is not None and dt <= prev_dt:
                    break   # duplicate or out-of-order — stop trusting the file here
                prev_dt = dt
                clean_rows += 1
    except FileNotFoundError:
        return 0
    return clean_rows


def _build_chunks(day: date):
    """Return (chunk_ends, chunk_sizes, total_chunks) for a session — the
    ordered list of chunk-end times and, for each, how many bars it's
    expected to contribute (usually _CHUNK_SECS, but computed rather than
    assumed so DST-edge days stay correct). Shared by _fetch_day and
    bars1s_repair.py so both use identical chunk math."""
    start_utc, end_utc = _session_bounds(day)
    chunk_ends = []
    t = start_utc + timedelta(seconds=_CHUNK_SECS)
    while t < end_utc:
        chunk_ends.append(t)
        t += timedelta(seconds=_CHUNK_SECS)
    chunk_ends.append(end_utc)   # always include exact session end
    chunk_sizes = [
        max(0, int((ce - max(ce - timedelta(seconds=_CHUNK_SECS), start_utc)).total_seconds()))
        for ce in chunk_ends
    ]
    return chunk_ends, chunk_sizes, len(chunk_ends)


def _verify_chunks_done(out_path: Path, chunk_sizes: list, claimed_done: int):
    """Check how many of the first `claimed_done` chunks are actually backed
    by clean (non-duplicate, in-order) rows on disk. Returns
    (verified_done, verified_rows). Used both by _fetch_day's resume self-heal
    and by bars1s_repair.py's proactive scan — same logic, same source of
    truth (the file itself), so a day can never be trusted as more complete
    than what's genuinely on disk."""
    if claimed_done <= 0 or not out_path.exists():
        return 0, 0
    clean_rows = _scan_clean_prefix(out_path)
    verified_done = 0
    running = 0
    for size in chunk_sizes[:claimed_done]:
        if running + size > clean_rows:
            break
        running += size
        verified_done += 1
    return verified_done, running


def _fetch_day(session: "_IBSession", conn: sqlite3.Connection,
               symbol: str, day: date, contract) -> int:
    """
    Fetch all 1-sec bars for (symbol, day).
    Resumes from last completed chunk. Flushes every _FLUSH_EVERY bars.
    Returns total bar count written this call (0 if already done or interrupted).

    Raises ConnectionError if the IB connection is lost and cannot be
    restored — deliberately fatal (see _IBSession/_is_disconnect_msg): the
    alternative, silently accepting empty results from a dead connection as
    "no trades this window", is what produced entire days falsely marked
    finished with zero real bars (2026-07-23 incident, see
    BARS1S_STATUS.md §0f). Better to crash and let bars_fetch_watchdog.py
    restart the process than to keep advancing progress on nothing.
    """
    date_str  = day.isoformat()
    start_utc, end_utc = _session_bounds(day)
    chunk_ends, chunk_sizes, total_chunks = _build_chunks(day)

    already_done = _chunks_done(conn, symbol, date_str)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT_DIR / f"{symbol}_{_FILE_SUFFIX}_{day.strftime('%Y%m%d')}.csv"

    # Self-heal: don't blindly trust the progress DB. Verify the file's actual
    # tail is genuinely clean before resuming from it — a prior crash could
    # have left chunks_done ahead of what's really flushed to disk, which
    # otherwise causes chunks to be silently re-fetched and duplicated (or the
    # real remaining chunks to be skipped entirely once finished=1 is set).
    if already_done > 0 and out_path.exists():
        verified_done, running = _verify_chunks_done(out_path, chunk_sizes, already_done)
        shortfall = already_done - verified_done
        # A day marked fully done but short by <=2 chunks matches the accepted
        # permanent tail-gap pattern (see §0k just above in this function) —
        # not corruption. Without this exception, self-heal would truncate and
        # endlessly re-attempt a gap that's already been deliberately accepted.
        is_accepted_tail_gap = already_done >= total_chunks and 0 < shortfall <= 2
        if verified_done < already_done and not is_accepted_tail_gap:
            log.warning(
                "%s %s: progress DB said chunks_done=%d but only %d chunks "
                "(%d rows) verified clean on disk — truncating and re-fetching "
                "the rest", symbol, date_str, already_done, verified_done, running)
            with open(out_path, "r", newline="", encoding="utf-8") as f:
                header = f.readline()
                lines = [next(f) for _ in range(running)]
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                f.write(header)
                f.writelines(lines)
            already_done = verified_done
            _save(conn, symbol, date_str, already_done, total_chunks, running, False)

    if already_done >= total_chunks:
        return 0   # all chunks fetched in a previous run

    open_mode = "a" if already_done > 0 and out_path.exists() else "w"

    # Count bars already on disk so the running total is accurate
    bars_so_far = 0
    if open_mode == "a" and out_path.exists():
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                bars_so_far = max(0, sum(1 for _ in f) - 1)
        except Exception:
            bars_so_far = 0

    buf         = []   # in-memory buffer — flushed every _FLUSH_EVERY bars
    bars_written = 0   # bars written this call

    with open(out_path, open_mode, newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if open_mode == "w":
            writer.writerow(["datetime_utc", "open", "high", "low", "close", "volume"])

        def flush():
            if buf:
                writer.writerows(buf)
                fh.flush()
                buf.clear()

        for i, chunk_end in enumerate(chunk_ends):
            if not _ok():
                flush()
                return bars_written

            if i < already_done:
                continue   # resume: skip chunks fetched in a prior run

            # durationStr must not exceed the actual window back to session start
            chunk_start  = chunk_end - timedelta(seconds=_CHUNK_SECS)
            # Don't request data before the session start
            actual_start = max(chunk_start, start_utc)
            dur_secs     = int((chunk_end - actual_start).total_seconds())
            if dur_secs <= 0:
                _save(conn, symbol, date_str, i + 1, total_chunks,
                      bars_so_far + bars_written, i + 1 >= total_chunks)
                continue

            end_str = chunk_end.strftime("%Y%m%d %H:%M:%S") + " UTC"

            # got_valid_result is the single source of truth for "is it safe
            # to advance progress for this chunk". Every rejection path below
            # (dead connection, pacing violation, stale/wrong-window response)
            # leaves it False. If every attempt is exhausted without it ever
            # becoming True, the chunk is a hard failure — never silently
            # accepted as "no trades this window" (that exact gap, for two
            # different rejection reasons, produced two separate incidents:
            # connection-loss on 2026-07-23 and stale-window exhaustion found
            # the same day while repairing the connection-loss incident — see
            # BARS1S_STATUS.md §0f/§0g).
            attempt = 0
            bar_list = []
            got_valid_result = False
            fail_streak = 0   # consecutive failures this chunk, any reason
            while attempt < 6 and _ok():
                attempt += 1
                _last_ib_error["code"] = None

                ib = session.get()
                if not ib.isConnected():
                    log.warning("%s %s chunk %d/%d: IB not connected — reconnecting",
                                symbol, date_str, i + 1, total_chunks)
                    ib = session.reconnect()
                    if not ib.isConnected():
                        fail_streak += 1
                        time.sleep(15)
                        continue

                try:
                    _throttle()
                    candidate = ib.reqHistoricalData(
                        contract,
                        endDateTime=end_str,
                        durationStr=f"{dur_secs} S",
                        barSizeSetting=_BAR_SIZE,
                        whatToShow=_WHAT,
                        useRTH=False,
                        formatDate=2,
                        keepUpToDate=False,
                        timeout=30,
                    ) or []
                except Exception as exc:
                    msg = str(exc)
                    fail_streak += 1
                    if _is_disconnect_msg(msg):
                        log.warning("%s %s chunk %d/%d attempt %d: connection error (%s) — reconnecting",
                                    symbol, date_str, i + 1, total_chunks, attempt, exc)
                        session.reconnect()
                        time.sleep(10)
                        continue
                    wait = 30 if "pacing" in msg.lower() else 5
                    log.warning("%s %s chunk %d/%d attempt %d: %s — retry in %ds",
                                symbol, date_str, i + 1, total_chunks, attempt, exc, wait)
                    time.sleep(wait)
                    continue

                # ib_insync doesn't always raise for a TWS-side error on this
                # request — it can just return []. An empty result caused by a
                # real pacing violation must NOT be accepted as "no trades
                # this window", or the chunk gets silently marked done with 0
                # bars. Only a genuinely empty response (no IB error fired)
                # is trusted as "no trades occurred".
                err_msg = (_last_ib_error["msg"] or "").lower()
                if not candidate and "pacing" in err_msg:
                    fail_streak += 1
                    log.warning("%s %s chunk %d/%d attempt %d: pacing violation "
                                "(%s) — retrying in 30s",
                                symbol, date_str, i + 1, total_chunks, attempt,
                                _last_ib_error["msg"])
                    time.sleep(30)
                    continue

                # Validate the window IB actually returned. IB/ib_insync has
                # been observed to occasionally hand back a STALE response —
                # bars from an earlier chunk's window instead of this one's —
                # with no exception raised. Row-count/finished-flag checks
                # don't catch this; only checking the returned timestamps does.
                if candidate:
                    first_dt = _bar_dt(candidate[0])
                    last_dt  = _bar_dt(candidate[-1])
                    if (first_dt and first_dt < actual_start - timedelta(seconds=1)) or \
                       (last_dt and last_dt >= chunk_end):
                        fail_streak += 1
                        log.warning(
                            "%s %s chunk %d/%d attempt %d: got stale window (%s..%s), "
                            "expected [%s, %s) — retrying (streak %d)",
                            symbol, date_str, i + 1, total_chunks, attempt,
                            first_dt, last_dt, actual_start, chunk_end, fail_streak)
                        if fail_streak >= 3:
                            # Reconnecting breaks whatever's serving the stale
                            # response far more reliably than just waiting —
                            # repeating the identical request against the same
                            # connection kept re-hitting the same stale data.
                            session.reconnect()
                        time.sleep(15)
                        continue

                bar_list = candidate
                got_valid_result = True
                _clear_chunk_crash(f"{symbol}|{date_str}|{i}")
                break

            if not got_valid_result:
                # Every attempt failed (connection, pacing, or stale-window) —
                # never silently accept this as "no trades this window". Normally
                # fatal by design: crash and let bars_fetch_watchdog.py restart
                # the process fresh (a new connection sometimes succeeds where
                # in-process reconnects didn't). But if this EXACT chunk has
                # already caused _MAX_CHUNK_CRASHES restarts, IB genuinely can't
                # serve it right now no matter how many times we reconnect —
                # crashing again would just loop forever with zero progress
                # (found 2026-07-23: same end-of-session chunk, crash-looped for
                # hours). Skip it instead: leave the day correctly incomplete
                # (never faked) and move on to the next pair; a future run can
                # retry once conditions change.
                # The confirmed-systemic failure (§0j) is specifically the last
                # 1-2 chunks of a session — only give those the fast (1-crash)
                # skip budget. A chunk anywhere else in the day failing this
                # hard is a different, more concerning signal (looks like a
                # real outage, not the known IB tail-data quirk) and deserves
                # more patience before giving up on it — a general outage
                # shouldn't gap out an otherwise-fine day just to move faster
                # past a known-bad tail chunk elsewhere.
                is_tail_chunk = i >= total_chunks - 2
                max_crashes = _MAX_CHUNK_CRASHES if is_tail_chunk else max(_MAX_CHUNK_CRASHES, 3)

                crash_key = f"{symbol}|{date_str}|{i}"
                crash_count = _record_chunk_crash(crash_key)
                if crash_count < max_crashes:
                    raise ConnectionError(
                        f"{fail_streak} consecutive failures, no valid response for {symbol} {date_str} "
                        f"chunk {i+1}/{total_chunks} — refusing to mark it done "
                        f"(restart {crash_count}/{max_crashes})")

                if is_tail_chunk:
                    # Confirmed 2026-07-24 (§0k): this isn't a transient issue —
                    # EVERY day checked so far, including sessions from days
                    # earlier (long since closed, nothing "still forming" about
                    # them), fails at exactly this same tail position(s).
                    # Evidence points to a genuine, likely permanent IB
                    # data-availability gap for the final settlement-adjacent
                    # hour of each CME session, not a bug in our request (the
                    # logged "expected" window above is exactly correct — IB
                    # just doesn't have it). Blocking every day forever on an
                    # apparently permanent gap defeats the point of fetching at
                    # all. Accept it: count THIS chunk as accounted-for (no
                    # bars, clearly logged) and move on to the next chunk —
                    # NOT the whole rest of the day, since the failure can hit
                    # the second-to-last chunk without the true last chunk
                    # necessarily failing too (observed variable gap width:
                    # usually 1 chunk, sometimes up to 3).
                    log.error(
                        "%s %s chunk %d/%d failed on %d restarts — accepting as a "
                        "permanent gap (known IB tail-hour issue, see §0k), no bars "
                        "for this chunk, continuing", symbol, date_str, i + 1, total_chunks, crash_count)
                    flush()
                    chunks_now = i + 1
                    total_bars = bars_so_far + bars_written
                    _save(conn, symbol, date_str, chunks_now, total_chunks,
                          total_bars, chunks_now >= total_chunks)
                    pct = 100 * chunks_now / total_chunks
                    print(f"  {symbol} {date_str}  {chunks_now:>2}/{total_chunks}"
                          f"  ({pct:3.0f}%)  +   0 bars [ACCEPTED GAP]  "
                          f"total={total_bars:,}", flush=True)
                    time.sleep(_INTER_REQ_S)
                    continue

                log.error(
                    "%s %s chunk %d/%d failed on %d separate process restarts — "
                    "SKIPPING this day for now (left incomplete, not marked finished; "
                    "will retry on a future pass)",
                    symbol, date_str, i + 1, total_chunks, crash_count)
                flush()
                return bars_written

            for bar in bar_list:
                dt = _bar_dt(bar)
                if dt is None:
                    continue
                buf.append([
                    dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    bar.open, bar.high, bar.low, bar.close, bar.volume,
                ])
                bars_written += 1
                if len(buf) >= _FLUSH_EVERY:
                    flush()

            # Flush BEFORE recording this chunk as done — the DB must never
            # claim a chunk is on disk before it actually is (that mismatch,
            # combined with a crash/kill in between, is what produced
            # silently duplicated/skipped tail chunks in the past).
            flush()
            chunks_now = i + 1
            total_bars = bars_so_far + bars_written
            _save(conn, symbol, date_str, chunks_now, total_chunks,
                  total_bars, chunks_now >= total_chunks)

            pct = 100 * chunks_now / total_chunks
            print(f"  {symbol} {date_str}  {chunks_now:>2}/{total_chunks}"
                  f"  ({pct:3.0f}%)  +{len(bar_list):>4} bars  "
                  f"total={total_bars:,}", flush=True)

            time.sleep(_INTER_REQ_S)

        flush()   # write any remaining buffered bars

    if bars_written > 0:
        log.info("[DONE] %s %s: %d bars -> %s", symbol, date_str,
                 bars_so_far + bars_written, out_path.name)
    return bars_written


# ── PID lock — prevents two instances writing to the same files concurrently ──
# (a duplicate/overlapping run is the most likely source of the duplicated-tail
# corruption found in a 2026-07-22 sanity check — see JULY_1S_DATA_HANDOFF.md)

def _acquire_lock() -> bool:
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text().strip())
            import psutil
            try:
                proc = psutil.Process(pid)
                if proc.status() not in ("zombie", "dead") and \
                   "bars1s_fetcher" in " ".join(proc.cmdline()):
                    log.warning("Another bars1s_fetcher is already running (pid=%d) — exiting", pid)
                    return False
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                pass
            _LOCK_FILE.unlink(missing_ok=True)   # stale lock
        except Exception:
            _LOCK_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        log.warning("Lost lock race — another bars1s_fetcher grabbed it first")
        return False


def _release_lock():
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── IB connection ─────────────────────────────────────────────────────────────

# ib_insync's default error handling logs TWS/IBG error callbacks (e.g. error
# 162 pacing violations) but does not always raise a Python exception from the
# reqHistoricalData() call that triggered them — it can just return an empty
# result. Track the last error here so the fetch loop can tell "pacing
# violation, must retry" apart from "genuinely no trades this window".
_last_ib_error = {"code": None, "msg": None}


def _on_ib_error(reqId, errorCode, errorString, contract=None, *_a, **_kw):
    _last_ib_error["code"] = errorCode
    _last_ib_error["msg"] = errorString


def _connect(cfg) -> IB:
    import random
    ib  = IB()
    ib.errorEvent += _on_ib_error
    ids = list(getattr(cfg.ib, "fetcher_client_ids", cfg.ib.live_client_ids))
    random.shuffle(ids)
    for cid in ids:
        try:
            ib.connect(cfg.ib.live_host, cfg.ib.live_port, clientId=cid,
                       timeout=cfg.ib.connection_timeout)
            if ib.isConnected():
                log.info("Connected  port=%d  clientId=%d", cfg.ib.live_port, cid)
                return ib
        except Exception as e:
            log.warning("clientId=%d failed: %s", cid, e)
    raise ConnectionError(f"Cannot connect to IB port {cfg.ib.live_port}")


def _is_disconnect_msg(msg: str) -> bool:
    """True if an exception/error message indicates the IB connection itself
    is dead (as opposed to a pacing violation, empty result, or other
    retryable-but-still-connected condition). Found 2026-07-23: without this
    distinction, a dropped connection just retries forever against a dead
    socket and eventually gets silently accepted as "no trades this window"
    — see BARS1S_STATUS.md §0f for the incident this fixes."""
    msg = msg.lower()
    return any(s in msg for s in (
        "not connected", "peer closed", "connection reset",
        "connection aborted", "socket is closed", "connection refused",
    ))


class _IBSession:
    """Mutable holder for the current IB connection so a reconnect inside
    _fetch_day() is visible to every other caller sharing this session
    (plain reassignment of a local `ib` variable would only update the
    reconnecting function's own copy)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ib = _connect(cfg)

    def get(self) -> IB:
        return self.ib

    def reconnect(self) -> IB:
        log.warning("Reconnecting to IB...")
        try:
            self.ib.disconnect()
        except Exception:
            pass
        for attempt in range(3):
            try:
                self.ib = _connect(self.cfg)
                return self.ib
            except Exception as exc:
                log.warning("Reconnect attempt %d/3 failed: %s", attempt + 1, exc)
                time.sleep(10)
        return self.ib   # still whatever's left; caller checks isConnected()

    def disconnect(self):
        try:
            self.ib.disconnect()
        except Exception:
            pass


# ── Main run ──────────────────────────────────────────────────────────────────

def run(symbols: list, days: list):
    if not _acquire_lock():
        sys.exit(1)

    cfg     = get_config()
    conn    = _init_db()
    session = _IBSession(cfg)

    contract_cache: dict = {}   # keyed by (symbol, "YYYYMM") — refreshed on month roll
    grand_total   = 0
    pairs_done    = 0
    pairs_total   = len(days) * len(symbols)
    t0            = time.time()

    print(f"\nbars1s_fetcher v{_VERSION} | bar={_BAR_SIZE} | {len(symbols)} symbols × {len(days)} days")
    print(f"Output  : {_OUTPUT_DIR}")
    print(f"Progress: {_PROGRESS_DB}")
    print(f"Range   : {days[-1]} to {days[0]}\n")

    try:
        for day in days:
            if not _ok():
                break
            for sym in symbols:
                if not _ok():
                    break

                date_str = day.isoformat()
                pairs_done += 1

                # Deliberately does NOT skip on finished=1 alone — a day can be
                # marked finished with a corrupted/short tail (see
                # JULY_1S_DATA_HANDOFF.md). _fetch_day() re-verifies the actual
                # file content itself and returns instantly (0 new bars) for
                # anything genuinely complete, so this stays cheap while
                # closing that blind spot.

                # Resolve contract (cache by month to survive intra-month reuse)
                month_key = (sym, day.strftime("%Y%m"))
                if month_key not in contract_cache:
                    try:
                        if not session.get().isConnected():
                            session.reconnect()
                        contract_cache[month_key] = _get_contract(session.get(), sym, day)
                        log.info("Contract %s %s -> %s", sym, day,
                                 contract_cache[month_key].localSymbol)
                    except Exception as exc:
                        log.error("Skip %s %s — contract error: %s", sym, date_str, exc)
                        continue
                contract = contract_cache[month_key]

                print(f"\n[{pairs_done}/{pairs_total}]  {sym}  {date_str}", flush=True)
                n = _fetch_day(session, conn, sym, day, contract)
                grand_total += n

    except ConnectionError as exc:
        log.error("FATAL: %s — exiting so the watchdog can restart cleanly", exc)
        print(f"\n=== bars1s_fetcher: FATAL connection loss — {exc} ===")
        raise

    finally:
        conn.close()
        elapsed = time.time() - t0
        print(f"\n=== bars1s_fetcher: {grand_total:,} new bars in {elapsed:.0f}s ===")
        log.info("Finished. grand_total=%d  elapsed=%.0fs", grand_total, elapsed)
        session.disconnect()
        _release_lock()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        # 1. Session bounds — must be same-time-same-offset, so always ~24h
        s, e = _session_bounds(date(2026, 7, 18))
        assert s < e, "start must precede end"
        dur = (e - s).total_seconds()
        assert 82800 <= dur <= 90000, f"Session duration unexpected: {dur}s"

        # 2. Chunk list — 24h / 30min = 48 chunks
        chunks = []
        t = s + timedelta(seconds=_CHUNK_SECS)
        while t < e:
            chunks.append(t)
            t += timedelta(seconds=_CHUNK_SECS)
        chunks.append(e)
        assert 46 <= len(chunks) <= 50, f"Unexpected chunk count: {len(chunks)}"

        # 3. Working days — all weekdays, descending
        days = _working_days(10)
        assert len(days) == 10
        for i, d in enumerate(days):
            assert d.weekday() < 5, f"{d} is weekend"
            if i > 0:
                assert days[i - 1] > d, "not descending"

        # 4. Progress DB round-trip
        with tempfile.TemporaryDirectory() as tmp:
            global _PROGRESS_DB
            orig_db    = _PROGRESS_DB
            _PROGRESS_DB = Path(tmp) / "test.db"
            try:
                c = _init_db()
                sym, ds = "MES", "2026-07-18"
                assert not _is_done(c, sym, ds)
                assert _chunks_done(c, sym, ds) == 0
                _save(c, sym, ds, 12, 48, 5000, False)
                assert _chunks_done(c, sym, ds) == 12
                assert not _is_done(c, sym, ds)
                _save(c, sym, ds, 48, 48, 22000, True)
                assert _is_done(c, sym, ds)
                c.close()
            finally:
                _PROGRESS_DB = orig_db

        # 5. Buffer flush logic
        buf = list(range(_FLUSH_EVERY + 50))
        chunk_a = buf[:_FLUSH_EVERY]
        chunk_b = buf[_FLUSH_EVERY:]
        assert len(chunk_a) == _FLUSH_EVERY
        assert len(chunk_b) == 50

        # 6. Bar-date parser — unix epoch string
        class FakeBar:
            date = "1753487700"   # a valid unix epoch
            open = high = low = close = 5500.0
            volume = 10
        dt = _bar_dt(FakeBar())
        assert dt is not None and dt.tzinfo is UTC

        # 7. _ok() with stop_at
        _state["running"] = True
        _state["stop_at"] = time.time() - 1   # already past
        assert not _ok()
        _state["running"] = True
        _state["stop_at"] = None
        assert _ok()

        print("[self-test] bars1s_fetcher: PASS")
        return True
    except Exception as exc:
        print(f"[self-test] bars1s_fetcher: FAIL — {exc}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _setup_signals()

    parser = argparse.ArgumentParser(description=f"OHLCV bar fetcher v{_VERSION}")
    parser.add_argument("--bar-secs", type=int, default=1, choices=sorted(_BAR_SECS_TABLE),
                        help="Bar size in seconds (default 1). Each size uses its own "
                             "output dir/progress DB/lock, safe to run concurrently.")
    parser.add_argument("--symbol", help="Single symbol, e.g. MES (default: all 4)")
    parser.add_argument("--symbols", help="Comma-separated symbols, e.g. MES,MNQ (default: all 4)")
    parser.add_argument("--days",   type=int, default=252,
                        help="Trading days back from yesterday (default 252 ≈ 1 year)")
    parser.add_argument("--test",   metavar="DURATION",
                        help="Stop after this long: 10m | 30s | 1h")
    parser.add_argument("--pace-max", type=int, default=None,
                        help="Override the pacing rate limiter's requests-per-10min "
                             "budget (default 40). Lower this when running two bar-size "
                             "fetchers concurrently so together they still fit under IB's "
                             "real ~60/10min ceiling.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run offline unit tests, no IB needed")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    _configure_bar_size(args.bar_secs)
    if args.pace_max is not None:
        _PACE_MAX_REQS = args.pace_max

    if args.test:
        m = re.match(r"^(\d+)(s|m|h)$", args.test.strip())
        if not m:
            sys.exit("--test format: Ns | Nm | Nh  (e.g. 10m)")
        secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        _state["stop_at"] = time.time() + secs
        print(f"[TEST MODE] Will stop after {args.test} ({secs}s)\n")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = _SYMBOLS
    days = _working_days(args.days)

    run(symbols, days)
