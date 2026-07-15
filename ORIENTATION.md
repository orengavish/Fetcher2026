# ORIENTATION — Claude's Living Brief for Fetcher2026
> Keep this file current: update whenever scope changes, a task completes, or next-steps shift.
> Last updated: 2026-07-15

---

## Who I Am Here

I'm Claude Code, embedded as the AI pair for this repo. My job: implement, debug, and evolve the tick-data fetching pipeline. I do not trade — this project does not trade.

---

## What Fetcher2026 Is

Standalone historical tick-data fetcher for CME micro futures, **split from Galgo2026** for stability. Fetching only — no trading logic, no positions, no broker commands.

**Symbols:** MES, MNQ, MYM, M2K  
**Data types:** TRADES (primary) + BID_ASK (secondary, currently off — see below)  
**Session window:** prev_day 17:00 CT → target_day 17:00 CT (~23h CME Globex session)  
**Output:** `C:\Projects\Galgo2026\june\trader\data\history\{SYMBOL}_{type}_{YYYYMMDD}.csv`  
**Progress DB:** `data/fetch_progress.db` — single source of truth, restart-safe  
**IB connection:** paper port 4002, client IDs 801–804 (4 parallel workers)

---

## My Relationship with Brother Fetcher

**Brother = Galgo2026** (`C:\Projects\Galgo2026`). It is the trading brain.

- Galgo2026 executes live trades, manages positions, runs the decider.
- Fetcher2026 feeds Galgo2026's tick archive (`galao.db` + CSV history files).
- Fetcher reads `paths.db` → Galgo's `galao.db` to know which trade dates need tick coverage.
- `fetch_priority.py` cross-references `verified_trades` in galao.db against files on disk.
- The two repos share `lib/` via path injection — Fetcher imports `lib.config_loader`, `lib.logger`, `lib.db`, `lib.gdrive` from root.

**Dependency direction:** Fetcher depends on Galgo's DB for priority signals. Galgo depends on Fetcher's CSVs for backtesting/analysis. They do not run in the same process.

---

## How I Run (Key Commands)

```powershell
# Normal scheduled run (called by Task Scheduler at 17:30 CT)
python trader/fetch_scheduler.py

# Backfill all missing priority dates
python trader/fetch_scheduler.py --backfill

# Fetch a specific date
python trader/fetch_scheduler.py --date 2026-06-02

# Dashboard (live fetch status, port 5050)
python dashboard.py --real

# See priority queue (what's missing vs what's verified in galao.db)
python trader/fetch_priority.py

# Verify a specific file
python trader/fetcher.py --verify --symbol MES --date 2026-04-07
```

**Task Scheduler tasks (Windows):**
- `GalgoFetcher2026` — watchdog every 5 min, restarts fetch_scheduler if dead
- `GalgoDashboard2026` — dashboard every 5 min, exits if port 5050 already bound

---

## Current State (as of 2026-07-15)

| Item | Status |
|------|--------|
| Version | v2.5 |
| TRADES fetch | Active |
| BID_ASK fetch | **ON** — re-enabled 2026-07-15 (Jul 13 trigger passed) |
| BID_ASK backfill | **ON** |
| Backfill window | 180 days |
| Lock file | Valid — PID 8208 active as of 2026-07-15 morning |

**TRADES coverage (as of 2026-07-15):**
- M2K: 67 files, Apr 13 – Jul 14 (mostly complete for recent 3 months)
- MES: 24 files, May 5 – Jul 14 (gaps in May–Jun, nothing before May 5)
- MNQ: 22 files, May 5 – Jul 14 (same)
- MYM: 21 files, May 5 – Jul 14 (same)
- Full 180-day backfill (to ~Jan 16) still in progress for MES/MNQ/MYM

**BID_ASK coverage:** 41 files total (pre-TRADES-only-mode), now running again.

**galao.db:** Empty — no `verified_trades` table. `fetch_priority.py` can't cross-reference trade dates yet.

**Key recent fixes:**
- v2.5: 60-min empty-window skip — clears the overnight dead zone (17:00–23:00 CT gap) in ~15 min instead of grinding through it
- v2.4: 3-timeout skip threshold — stops hanging on genuinely empty windows
- v2.3: `_last_working_day()` fix — Monday P0 fetch now works correctly
- v2.2: M2K promoted to catch-up priority

---

## Planned Next Steps

1. ~~**Re-enable BID_ASK**~~ — Done 2026-07-15. `fetch_bid_ask: true` + `backfill_bid_ask: true`.
2. ~~**Verify TRADES completeness / lock file**~~ — Done 2026-07-15. Scheduler PID 8208 running; galao.db empty (no verified_trades yet).
3. **Continue TRADES + BID_ASK backfill** — Let scheduler run. MES/MNQ/MYM need ~50+ more trading days back to Jan 2026. Monitor dashboard at http://localhost:5050.
4. **galao.db population** — Once Galgo2026 runs live sessions, `verified_trades` will populate and `fetch_priority.py` will become useful for prioritizing.
5. **Google Drive upload** — `google_drive.enabled: false`. Wire up if off-machine CSV backup is wanted.

---

## Things That Bite

- **IB pacing:** 4 workers (`_N_WORKERS=4`) — reduced from 8 after pacing cascade issues. Don't bump without testing.
- **Empty-window trap:** Overnight CT gap (approx 17:00–23:00) returns 0 ticks per window. v2.5 now skips 60-min chunks after N consecutive empties — don't regress this.
- **Lock file:** If the scheduler crashes hard, `data/fetch_scheduler.lock` stays. Next run refuses to start. Delete it manually.
- **Shared lib path:** `sys.path.insert(0, _ROOT)` at top of every module. If root changes, all imports break silently.
- **galao.db path:** Hardcoded in config as `C:\Projects\Galgo2026\june\data\galao.db` — if Galgo2026 moves, update `paths.db`.
