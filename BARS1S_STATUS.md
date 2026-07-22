# bars1s_fetcher — Status & Restart Reference
> Written: 2026-07-22 | Updated: 2026-07-22 ~07:05 UTC | Script: v1.0 | State: RUNNING in background

## 0b. UPDATE 2026-07-22 ~07:40 UTC — duplicate-tail data bug fixed + repaired

A sanity check found every "finished" day (76 of 78 files, all 4 symbols) was
missing its real final ~1 hour of data, silently backfilled with a duplicate
of an earlier hour, while the progress DB still reported `finished=1` /
`86,400 bars`. Full writeup: `JULY_1S_DATA_HANDOFF.md` §4.

**Root cause**: not a resume/crash artifact as first suspected — IB itself
was returning a **stale/cached response** (the previous chunk's window)
for the last 1-2 chunks of a session, with no exception raised, so the old
code wrote it as if valid. Strong suspect for *why*: `back-trading/trading_dashboard.py`
(a separate, unrelated project) was found running concurrently against the
same IB paper account (port 4002) — two processes competing for the same
account-wide historical-data pacing budget would explain why our own
proactive 55-req/10-min throttle wasn't sufficient. That process was left
untouched (not ours to manage), but it's worth knowing about if this
resurfaces.

**Fixes applied to `bars1s_fetcher.py`:**
1. Flush to disk *before* marking a chunk done in the DB (previously the DB
   could claim a chunk was saved before it was actually flushed).
2. Self-heal on resume: verifies the file's actual tail is genuinely
   monotonic/duplicate-free before trusting `chunks_done` from the DB;
   truncates and corrects the DB if it isn't.
3. Validates every response's timestamps against the requested window
   before accepting it — rejects and retries a stale/wrong-window response
   instead of writing it.
4. Distinguishes a real IB pacing-violation error (which must retry) from a
   genuinely-empty result (which is fine to accept) — previously both looked
   like `[]` and a pacing violation could get silently accepted as "no trades".
5. `run()` no longer skips a day purely because `finished=1` in the DB — it
   always calls `_fetch_day`, which now self-verifies and returns instantly
   for anything genuinely complete. Closes the blind spot where a
   corrupted-but-flagged-finished day would never be looked at again.
6. Added a PID lock file (`data/bars1s_fetcher.lock`, same pattern as
   `fetch_scheduler.py`) so two instances of this script can't write to the
   same files concurrently.

**Repair**: `trader/bars1s_repair.py` scanned all existing CSVs and flagged
76 of 78 (symbol, date) pairs as `finished=0` (no CSV rows were deleted by
the repair script itself — `_fetch_day`'s self-heal truncates the corrupt
tail and re-fetches it lazily, the next time each day is visited).

**Status as of this fix**: the fetcher is re-running through all 1008 pairs;
already-good chunks (0-45/46 of 48 per day) are preserved, only the corrupted
tail (1-2 chunks, occasionally more) gets re-fetched per day. Progress is
currently **slow** due to real IB pacing pressure (possibly from the
concurrent dashboard process above) — expect frequent
"stale window" / "pacing violation" retry warnings in the log for a while;
these are being correctly rejected/retried now, not silently written.

---

## 0. UPDATE 2026-07-22 07:00 UTC — process had died, restarted; viewer added

The original run (PID 17940) had **silently died** sometime around 06:42 UTC
(no crash traceback, no clean-shutdown message — looks like it was killed
abruptly, e.g. by a forceful terminate or a machine/IB gateway blip). Progress
was untouched (SQLite DB is durable), so it was simply **restarted** — it
resumed from the progress DB and is skipping everything already finished.

- New process launched 2026-07-22 07:00:44 UTC. Track it via **Windows PID
  1344** (launched through a bash job; see §4 for the correct check command).
- A new **live viewer** was added: `trader/bars1s_viewer.py` — see §14. A
  `--watch 60` instance of it is also running in the background, continuously
  rewriting `data/bars1s_viewer.html` (open it in a browser; it auto-refreshes
  every 60s).
- Because the fetcher restarted, it recomputed "yesterday" as **2026-07-21**
  (today advanced a day), so the window shifted to `2026-07-21 → 2025-08-04`.
  MNQ 2026-07-20 (the known gap, §6) is still inside this window and will be
  fetched automatically within the first ~10 pairs — no manual follow-up
  needed unless the run dies again before reaching it.

---

## 1. WHAT THIS IS

`trader/bars1s_fetcher.py` fetches 1-second OHLCV TRADES bars for
**MES / MNQ / MYM / M2K** going back ~1 year (252 trading days) from IB
gateway port 4002.

**Output:** one CSV per symbol per day  
**Format:** `datetime_utc, open, high, low, close, volume`  
**Location:** `C:\Projects\Fetcher2026\data\bars1s\{SYM}_1s_{YYYYMMDD}.csv`

---

## 2. CURRENT STATE (2026-07-22 ~07:05 UTC, post-restart)

| Status | Count | Detail |
|--------|-------|--------|
| Finished entries | 74 | ~18-19 trading days × 4 symbols |
| Bars on disk | ~6.42 million | |
| In progress | MNQ 2026-07-21 (chunk 7/48, pacing sleep ~462s) | |
| Needs follow-up | MNQ 2026-07-20 — will be reached automatically within ~10 pairs (see §0, §6) | |
| Remaining | ~934 more (symbol,day) pairs | est. 5-6 days wall-clock from restart |

The background process covers days from **2026-07-21 backward to 2025-08-04**
(252 trading days, most-recent-first). Window shifts by one day each time the
script restarts, since "yesterday" is recomputed at launch.

**For a live view of this table at any time**, run or check
`trader/bars1s_viewer.py` — see §14.

---

## 3. SCRIPT LOCATION & HOW TO RUN

```powershell
# Full year — all 4 symbols, 252 days (the current run)
python trader/bars1s_fetcher.py

# Single symbol, last N days
python trader/bars1s_fetcher.py --symbol MNQ --days 1

# Time-limited test
python trader/bars1s_fetcher.py --test 10m

# Offline unit tests (no IB needed)
python trader/bars1s_fetcher.py --self-test
```

Run from: `C:\Projects\Fetcher2026`  
Python: `C:\Users\galsh\AppData\Local\Programs\Python\Python311\python.exe`

---

## 4. BACKGROUND PROCESS

The current run was (re)launched 2026-07-22 ~07:00:44 UTC. Windows PID: **1344**.
(The original 2026-07-21 ~20:18 UTC run, PID 17940, died silently around
06:42 UTC and was restarted — see §0.)

```powershell
# Check if still running
Get-Process -Id 1344 -ErrorAction SilentlyContinue

# If that PID has drifted (script restarted again), find it by log freshness instead:
(Get-Item C:\Projects\Fetcher2026\data\bars1s_run.log).LastWriteTime   # compare to Get-Date
# If stale by more than ~12 min while pairs remain unfinished, the process is dead — restart it (see §9).

# Check last log lines (live progress)
Get-Content C:\Projects\Fetcher2026\data\bars1s_run.log | Select-Object -Last 20

# Check errors
Get-Content C:\Projects\Fetcher2026\data\bars1s_run_err.log | Select-Object -Last 10
```

**Health-check shortcut:** `python trader/bars1s_viewer.py` reports a
RUNNING / STALLED / COMPLETE badge based on exactly this staleness check —
no need to do the timestamp math by hand (see §14).

A `--watch 60` instance of the viewer is also running in the background
(bash job PID 941 at time of writing), continuously rewriting
`data/bars1s_viewer.html` so an open browser tab stays live.

Log files:
- `C:\Projects\Fetcher2026\data\bars1s_run.log` — stdout (progress per chunk)
- `C:\Projects\Fetcher2026\data\bars1s_run_err.log` — stderr (mostly ib_insync internals, non-fatal)

---

## 5. IB CONSTRAINTS & RATE LIMITER

### IB hard limits
- `barSizeSetting="1 secs"` max duration per request: **1800 S (30 min)**
- Global pacing: **60 requests per 10-minute rolling window** (enforced by IB broker-side)
- Error 162 = pacing violation; returned as empty BarDataList, NOT a Python exception

### CME session window
Each "day" = CME Globex session: **prev_day 17:00 CT → day 17:00 CT** (~24h)  
24h ÷ 30min = **48 chunks per symbol per day**

### Rate limiter in script
The script uses a **proactive rolling-window rate limiter** (`_throttle()` function):
- Tracks timestamps of all `reqHistoricalData` calls in a `deque`
- If ≥ 55 requests are in the last 600 seconds, sleeps until the oldest drops out
- Called before every request — IB Error 162 should never occur

This is why `Pacing: 55 req in window - sleeping 442s` appears in the log:
that sleep is **intentional and correct**, not a symptom of a problem.

---

## 6. KNOWN GAP: MNQ 2026-07-20

During the initial 10-minute test run (before the rate limiter existed), the script
hit pacing violations and processed all 48 chunks for MNQ 2026-07-20 but only got
real data for 12 of them (21,600 bars). It then incorrectly marked the day as
`chunks_done=48, finished=1`.

**Fix applied (2026-07-22):**
- The partial CSV (`MNQ_1s_20260720.csv`) was deleted
- Progress DB was reset to `chunks_done=0, finished=0` for MNQ 2026-07-20

**BUT:** the currently running process already skipped past MNQ 2026-07-20 (it
processes days most-recent-first and has moved to 2026-06-xx). It won't re-visit it.

**Action required after the main run finishes:**
```powershell
python trader/bars1s_fetcher.py --symbol MNQ --days 1
```
That fetches only the most-recent trading day for MNQ (which will be 2026-07-20
since it's the only unfished MNQ day near the top).

Or just restart the full run — it will skip all finished entries and only fetch
what's missing, including MNQ 2026-07-20.

---

## 7. PROGRESS DB

**Path:** `C:\Projects\Fetcher2026\data\bars1s_progress.db`  
**Table:** `progress`

```sql
SELECT symbol, date, chunks_done, total_chunks, bars_fetched, finished
FROM progress ORDER BY date DESC, symbol;
```

| Column | Meaning |
|--------|---------|
| chunks_done | How many 30-min windows have been fetched (0–48) |
| total_chunks | Always 48 for a full session |
| bars_fetched | Total bars written to CSV for this (symbol, date) |
| finished | 1 = complete, 0 = in progress or not started |

### Check overall status
```powershell
$s = @'
import sqlite3
conn = sqlite3.connect(r"C:\Projects\Fetcher2026\data\bars1s_progress.db")
done  = conn.execute("SELECT COUNT(*) FROM progress WHERE finished=1").fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
bars  = conn.execute("SELECT SUM(bars_fetched) FROM progress").fetchone()[0] or 0
print(f"Done: {done}/{total} entries   Bars: {bars:,}")
conn.close()
'@
$s | python
```

### Manual reset (if a day is stuck or has 0 bars)
```powershell
$s = @'
import sqlite3, pathlib
db   = r"C:\Projects\Fetcher2026\data\bars1s_progress.db"
conn = sqlite3.connect(db)
conn.execute("UPDATE progress SET chunks_done=0, bars_fetched=0, finished=0 WHERE symbol=? AND date=?", ("MNQ", "2026-07-20"))
conn.commit()
conn.close()
print("Reset done")
'@
$s | python
# Also delete the corresponding CSV if it exists:
Remove-Item C:\Projects\Fetcher2026\data\bars1s\MNQ_1s_20260720.csv -ErrorAction SilentlyContinue
```

---

## 8. OUTPUT FILES

```
C:\Projects\Fetcher2026\data\bars1s\
  MES_1s_20260720.csv      86,400 rows (48 chunks × 1800 bars)
  MYM_1s_20260720.csv      86,400 rows
  M2K_1s_20260720.csv      86,400 rows
  ...
```

**CSV format:**
```
datetime_utc,open,high,low,close,volume
2026-07-20T17:00:01Z,5475.25,5475.5,5475.0,5475.25,12
...
```

Full day = **86,400 rows** (1 per second × 86,400 seconds in 24h).  
Nights / maintenance windows = bars still present (just zero-volume during
very thin periods — IB returns whatever traded).

---

## 9. HOW TO KILL AND RESTART

```powershell
# Kill the current run (check §4 for the current PID — it changes on every restart)
Stop-Process -Id 1344 -Force

# Or kill by name if PID is unknown (careful: kills ALL python.exe processes)
Get-Process python | Stop-Process -Force

# Restart — it resumes from where it left off (progress DB tracks position)
cd C:\Projects\Fetcher2026
Start-Process python -ArgumentList "trader/bars1s_fetcher.py" `
    -WorkingDirectory C:\Projects\Fetcher2026 `
    -RedirectStandardOutput data\bars1s_run.log `
    -RedirectStandardError  data\bars1s_run_err.log `
    -WindowStyle Hidden -PassThru | Select Id, ProcessName
```

**2026-07-22 incident:** the run died with no crash trace in either log —
just silence mid-sleep. If this happens again, the `python trader/bars1s_viewer.py`
health badge (STALLED) is the fastest way to notice — check it before assuming
it's still working just because the process list shows a `python.exe`.

Safe to kill at any time — the script flushes every 1000 bars and saves
progress after each chunk. At most one 30-min chunk is lost on a hard kill.

---

## 10. KEY CONSTANTS IN SCRIPT

```python
_SYMBOLS      = ["MES", "MNQ", "MYM", "M2K"]
_CHUNK_SECS   = 1800       # 30 min per IB request (IB hard limit)
_BAR_SIZE     = "1 secs"
_WHAT         = "TRADES"
_INTER_REQ_S  = 2.0        # minimum sleep between requests
_FLUSH_EVERY  = 1000       # bars buffered before disk flush
_PACE_MAX_REQS = 55        # rate limiter threshold (of 60 allowed)
_PACE_WINDOW_S = 600       # 10-minute rolling window
```

IB connection: **port 4002** (paper gateway), client IDs from
`config.yaml` → `ib.fetcher_client_ids: [801, 802, 803, 804]`

---

## 11. ESTIMATED COMPLETION

Throughput at 55 req / 10 min = 5.5 req/min.  
Total requests: 4 symbols × 252 days × 48 chunks = 48,384.  
At 5.5/min: ~8,800 min = **~6 days** from cold start.

Started 2026-07-21 ~20:18 UTC. At current pace (~72 entries / ~10 hrs):
~72 entries/10hr × ongoing → roughly 4–5 more days to finish.

Expected completion: **~2026-07-26–27**.

---

## 12. STDERR NOTE

The log error:
```
KeyError: 6
  File "...ib_insync/wrapper.py", line 521, in contractDetails
```
is a known ib_insync internal warning emitted when a duplicate contract-details
response arrives for a request that already completed. Non-fatal — safe to ignore.

---

## 13. AFTER THE RUN COMPLETES

1. Run follow-up for MNQ 2026-07-20 (see §6):
   ```
   python trader/bars1s_fetcher.py --symbol MNQ --days 1
   ```

2. Verify output:
   ```powershell
   (Get-ChildItem C:\Projects\Fetcher2026\data\bars1s\*.csv | Measure-Object).Count
   # Expected: 252 × 4 = 1,008 files
   ```

3. Data is ready to use for backtesting / bars.db import into Galgo2027.

---

## 14. LIVE VIEWER (`trader/bars1s_viewer.py`)

Added 2026-07-22. Reads `data/bars1s_progress.db` (read-only, safe to run
alongside the fetcher) and reports, at any time:

- Bars collected **per symbol, per month, and per year**
- Overall completion % (chunks done / expected total)
- A **health badge** — RUNNING / STALLED / COMPLETE — based on how long ago
  the progress DB was last written (flags STALLED past 12 min of silence,
  which is how the 2026-07-22 dead-process incident, §0, would be caught
  automatically next time)
- **Estimated time remaining** — uses the throughput actually observed
  between the last two times the viewer ran (cached in
  `data/bars1s_rate_cache.json`); falls back to the fetcher's designed
  55-req/10-min pacing ceiling if no recent sample exists yet

```powershell
# One-shot: print console summary + (re)write the HTML report
python trader/bars1s_viewer.py

# Keep it live: regenerate every 60s until Ctrl+C
python trader/bars1s_viewer.py --watch 60

# Console only, skip the HTML file
python trader/bars1s_viewer.py --no-html
```

Output HTML: `data/bars1s_viewer.html` — open it directly in a browser
(auto-refreshes every 60s via meta-refresh; only shows new data if something
is actively rewriting the file, e.g. a `--watch` instance running in the
background).
