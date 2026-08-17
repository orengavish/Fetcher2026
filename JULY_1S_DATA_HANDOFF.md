# July 2026 1-Second Bar Data — Handoff for Graphing / Correlation Work
> Written: 2026-07-22 ~07:20 UTC | Updated: 2026-07-22 ~08:27 UTC
> For a fresh Claude instance, no prior context needed.
> Source project: `C:\Projects\Fetcher2026` (background fetcher, still running)

---

## 0. START HERE — build a temp playground now, don't wait for the full dataset

The fetcher is mid-repair (see §4) and will take **several more days** to reach
a fully clean, complete state across all 252 trading days. **Don't block on
that.** Start building the graphing/correlation tooling now against whatever's
currently on disk, using the cleaning step in §4 — that data is real and
usable once cleaned, it's just a growing subset rather than the full year.

**What to do:**
1. Set up a **scratch/playground area** (a notebook, a `playground/` or
   `scratch/` folder, whatever fits your workflow) for prototyping charts and
   correlation code against the July data that exists right now. Treat it as
   throwaway/iterative — the goal is to get the plotting and correlation
   logic right, not to produce final output yet.
2. Always load through the `load_clean()` snippet in §4. Never read a CSV in
   this dataset directly and trust its row count — see why in §4.
3. Re-check coverage before each session of work with `python trader/bars1s_viewer.py`
   (see §2) — the set of usable days grows every day the fetcher runs.
4. Once the fetcher finishes its repair pass (ETA is currently long and
   pacing-limited — check the viewer for a live estimate, don't trust a
   number written into this doc), promote the playground code into real
   analysis: at that point every file should be genuinely complete and the
   `load_clean()` defensive check becomes a no-op safety net rather than a
   load-bearing requirement.

**How to access everything, concretely:**
- Repo root: `C:\Projects\Fetcher2026` (this machine, no remote/cloud access needed)
- Data files: `C:\Projects\Fetcher2026\data\bars1s\{SYMBOL}_1s_{YYYYMMDD}.csv` — just read them with pandas/csv, no auth, no API
- Progress DB: `C:\Projects\Fetcher2026\data\bars1s_progress.db` (SQLite, read-only queries are safe to run anytime, even while the fetcher is writing — SQLite handles concurrent readers fine)
- Live status: **http://localhost:5004/** (or `http://localhost:5004/api/status` for JSON) — a persistent dashboard (`trader/bars_status_server.py`) now covers all three concurrently-running fetchers (1s/5s/30s bars, see `BARS1S_STATUS.md` §0c), auto-refreshing every 10s with a real observed-throughput ETA. `python trader/bars1s_viewer.py` (see §2) still works for a quick one-shot console printout of the 1s fetcher only.
- No credentials needed for any of this — it's all local filesystem + local SQLite, the IB connection is the fetcher's problem, not yours

**Current snapshot (2026-07-22 ~08:27 UTC, will be stale immediately — re-check, don't cite this):**
12/1,008 (symbol, day) pairs finished, ~6.7M bars on disk, actively repairing
July 21 → 17 → ... backward. Progress is currently slow (real IB pacing
contention, see §4) — expect this to take days, not hours.

---

## 1. WHAT THIS IS

`C:\Projects\Fetcher2026\trader\bars1s_fetcher.py` is a background process pulling
1-second OHLCV **TRADES** bars for 4 CME micro futures (MES, MNQ, MYM, M2K) from
Interactive Brokers, going back ~1 year. It writes one CSV per symbol per trading
day to:

```
C:\Projects\Fetcher2026\data\bars1s\{SYMBOL}_1s_{YYYYMMDD}.csv
```

Columns: `datetime_utc, open, high, low, close, volume` — timestamps are UTC,
ISO 8601 (`2026-07-14T20:30:00Z`), one row per second across the full CME
Globex session (prev-day 17:00 CT → day 17:00 CT, ~24h).

Progress is tracked in a SQLite DB: `C:\Projects\Fetcher2026\data\bars1s_progress.db`,
table `progress(symbol, date, chunks_done, total_chunks, bars_fetched, finished, updated_at)`.

**This doc is about whether July 2026 data specifically is safe to graph/correlate
right now, and it is NOT fully clean — read §4 before you load anything.**

---

## 2. HOW TO CHECK "WHAT'S FINISHED" RIGHT NOW

Don't hardcode the numbers below — the fetcher is a live background process and
they change. Two ways to check current state:

**A. Run the live viewer** (built 2026-07-22, reads the progress DB):
```powershell
python trader/bars1s_viewer.py
```
Prints bars-per-symbol-per-month/year, overall % complete, a RUNNING/STALLED/
COMPLETE health badge, and an ETA. Also writes `data/bars1s_viewer.html` (open
in a browser) if you want a visual dashboard. `--watch 60` keeps it refreshing.

**B. Query the DB directly:**
```python
import sqlite3
conn = sqlite3.connect(r"C:\Projects\Fetcher2026\data\bars1s_progress.db")
rows = conn.execute(
    "SELECT symbol,date,chunks_done,total_chunks,bars_fetched,finished "
    "FROM progress WHERE date LIKE '2026-07%' ORDER BY symbol,date"
).fetchall()
```

**IMPORTANT CAVEAT:** `finished=1` and `bars_fetched=86400` do **NOT** guarantee
the file's data is actually complete/correct. See §4 — there's a known bug where
"finished" days can still be missing real hours of data, silently backfilled
with duplicate rows to hit the expected row count. Row count and the `finished`
flag are not sufficient validation by themselves.

---

## 3. JULY 2026 STATUS SNAPSHOT (as of 2026-07-22 07:20 UTC — HISTORICAL, see §0/§2 for current)

This table predates the bug fix + repair pass in §4 and is kept only for
context on what the *original* sanity check found. Don't use it to decide
what's usable today — every one of these "complete" days needs re-checking
against the repair's progress. Use `python trader/bars1s_viewer.py` (§2) for
live numbers.

| Symbol | Complete trading days | Gaps |
|--------|----|------|
| MES | 07-01 through 07-21 | none — fully current |
| MYM | 07-01 through 07-20 | 07-21 not yet fetched (fetcher hasn't reached it today) |
| M2K | 07-01 through 07-20 | 07-21 not yet fetched |
| MNQ | 07-01 through 07-17 | **07-20 not started (0/48 chunks)** — known gap, see below; 07-21 in progress (~65% as of last check) |

The MNQ 2026-07-20 gap is a **known, separate issue** (documented in
`BARS1S_STATUS.md` §6): an earlier pacing-related bug caused it to be marked
"finished" with only partial data, so it was reset to unfinished. The
currently-running fetcher will reach it automatically within the next few
symbol-days (it processes most-recent-first: 07-21 → 07-20 → 07-17 → ...).

**Bottom line: July 1–17 is the only stretch where all 4 symbols have data
today.** 07-20/07-21 are still filling in. Check §2 for current state before
committing to a date range.

---

## 4. CRITICAL — DATA QUALITY BUG FOUND 2026-07-22 (sanity check)

A sanity pass over every "finished" July file (all 4 symbols) found a
**systematic, universal defect**, not random noise:

- Every finished day file checked (MES/MNQ/MYM/M2K, multiple dates in July)
  has its **last ~3600 rows (2 chunks / 1 hour) be an exact duplicate** of an
  earlier stretch of the same file, instead of containing the real final hour
  of the trading session.
- Concretely, for `MES_1s_20260714.csv`: the file has 86,400 rows (looks
  complete), but only **82,800 unique timestamps** — real data stops at
  `2026-07-14T20:59:59Z`; the last hour (21:00–22:00Z, i.e. the final hour
  before CME close) is **missing and replaced with a duplicate of the
  20:00–21:00Z hour**. Confirmed identical across MES/MNQ/MYM/M2K for the same
  date, and also present on 2026-07-01.
- **2026-07-03 is worse**: only 68,400 unique timestamps out of 86,400 — the
  **last 5 hours** are missing/duplicated, not just 1. (This may be related to
  July 3rd being a CME early-close day, but the mechanism duplicates data
  rather than legitimately reporting a short session, so treat it as the same
  bug, just worse that day.)
- The progress DB reports `chunks_done=48/48, bars_fetched=86400, finished=1`
  for all of these — **the row count and `finished` flag do not catch this.**
  Only a unique-timestamp check does.
- Duplicated rows are byte-identical to their source (same OHLCV values), so
  a naive `drop_duplicates` on the whole row is safe for removing the
  duplicate copy — but that still leaves a **real gap** (missing hour) in the
  time series that must not be silently interpolated over without flagging it.

**UPDATE 2026-07-22 ~07:40 UTC — fixed and repair in progress.** Root cause
turned out to be simpler and worse than a resume artifact: IB itself was
returning a **stale/cached response** (an earlier chunk's data) for the last
1-2 chunks of a session's fetch, with no exception raised — the old code had
no way to detect this and wrote it as if valid. A likely contributor: a
separate, unrelated process (`back-trading/trading_dashboard.py`) was found
running concurrently against the same IB paper account, which would explain
persistent pacing pressure beyond what `bars1s_fetcher.py`'s own throttle
could see or control.

Fixes applied to `bars1s_fetcher.py` (see `BARS1S_STATUS.md` §0b for full
detail): validates every response's timestamps against the requested window
and rejects/retries stale ones; distinguishes a real pacing-violation error
from a genuinely-empty result; flushes to disk before marking a chunk done
in the DB; self-heals on resume by verifying the file's actual tail rather
than trusting `chunks_done` blindly; no longer skips a day just because
`finished=1` (that flag alone was never sufficient — see below).
`trader/bars1s_repair.py` flagged 76 of 78 already-fetched files as
genuinely incomplete so the fetcher revisits and correctly re-fetches just
their missing tail (no already-correct data was discarded or re-fetched).

**As of this writing the repair is still running** and progress is slow —
real IB pacing pressure (possibly from the dashboard process above) is
causing frequent retries. Re-run the validation snippet in this section
before trusting any specific file is now clean; don't assume the bug is
fully swept from every file yet.

### Required validation before using ANY day's file for graphing/correlation

```python
import pandas as pd

def load_clean(path):
    df = pd.read_csv(path, parse_dates=["datetime_utc"])
    n_total = len(df)
    df = df.drop_duplicates(subset="datetime_utc", keep="first")
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    n_unique = len(df)
    expected = 86400
    if n_unique < expected:
        missing = expected - n_unique
        print(f"{path}: {n_total - n_unique} dup rows removed, "
              f"{missing} real seconds still MISSING (gap, not filled)")
    return df
```

Do **not** trust `chunks_done`/`finished`/row-count alone. Always run the
unique-timestamp check above (or equivalent) per file before charting or
computing correlations — otherwise you will silently plot/duplicate-weight
fabricated data in the last hour of affected sessions, which will visibly
show as a flat/repeating segment or a sawtooth if you reindex to a full
86,400-second grid.

---

## 5. SUGGESTED APPROACH FOR BAR GRAPHS & CROSS-SYMBOL CORRELATION

1. **Load + clean** each symbol/day file with the snippet in §4 (dedupe, sort,
   report missing seconds — don't fabricate/interpolate silently).
2. **Resample** 1-second bars to a coarser granularity for correlation work
   (1s noise dominates at native resolution) — e.g. 1-min or 5-min OHLCV:
   `open=first, high=max, low=min, close=last, volume=sum`, grouped on the
   shared UTC index across all 4 symbols so timestamps line up.
3. **Align on a common index** before correlating — after resampling, reindex
   all 4 symbols to the same time grid (inner join on timestamp) so gaps in
   one symbol (e.g. a missing hour) don't silently shift/misalign the others.
4. **Correlation**: compute on **returns** (`close.pct_change()`), not raw
   price levels, to avoid spurious correlation from shared trend/drift.
   Rolling-window correlation (e.g. 30-min or 1-hour rolling) is more useful
   than a single full-day scalar for spotting regime changes.
5. Flag/exclude the known-missing final-hour windows (§4) from any per-session
   "close" or "settlement" analysis specifically, since that's exactly the
   window most likely to be corrupted right now.

---

## 6. WHERE TO GO FOR MORE CONTEXT

- `BARS1S_STATUS.md` (repo root) — fetcher operational status, restart history,
  known MNQ 2026-07-20 gap, kill/restart instructions.
- `trader/bars1s_fetcher.py` — the fetcher itself (chunking, pacing, resume logic).
- `trader/bars1s_viewer.py` — live status viewer (§2 above).
- `data/bars1s_progress.db` — authoritative progress state (with the caveat in §4).
