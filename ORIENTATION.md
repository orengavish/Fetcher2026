# ORIENTATION — Claude's Living Brief for Fetcher2026
> Keep this file current: update whenever scope changes, a task completes, or next-steps shift.
> Last updated: 2026-09-04
> New here or setting up on a fresh machine? Start with **`RESTART_PROJECT.md`**. Coordinating
> this repo alongside its siblings (CC2026, GevaExtract)? Start with **`ORCHESTRATOR.md`**.

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

## My Relationship with Sibling Projects

**Correction (2026-08-22): the "trading brain" is `CriticalCorallations2026` ("CC2026"),
not `Galgo2026`.** `Galgo2026` is the pre-split original monolith — mostly legacy now, but
still load-bearing for one specific reason (see below). This section previously said
Galgo2026 was the live trading brain; that was stale. Verified directly: CC2026's
`trader/broker.py` + `trader/decider.py` + `back-trading/trading_dashboard.py` are the
actual live (paper) trading processes running today.

- **CC2026** executes live (paper) trades, manages positions, runs the decider. Dashboard
  on port 5003.
- **Galgo2026** is legacy, but Fetcher2026's tick-CSV output and `fetch_progress.db` still
  physically live under its `june\` subtree (`C:\Projects\Galgo2026\june\trader\data\history\`)
  — a holdover from before the 3-way split, not fixed, don't delete/archive Galgo2026 until
  this is paid off.
- Fetcher2026's `trader/config.yaml` → `paths.db` points at
  `C:\Projects\Galgo2026\june\data\galao.db` — **a dead, empty, pre-split copy**, not
  CC2026's live one (`C:\Projects\CriticalCorallations2026\trader\data\galao.db`).
  `fetch_priority.py`'s `verified_trades` cross-reference is effectively inert as a result —
  not just "waiting for data to populate" as previously documented here, but pointed at the
  wrong file entirely. See `RESTART_PROJECT.md` §4 / `ORCHESTRATOR.md` §4.
- The two repos share `lib/`-style helpers by convention (`config_loader`, `logger`, `db`),
  but **not the same code** — CC2026 and Fetcher2026 each have their own
  `lib/config_loader.py` that diverged; don't assume a fix in one applies to the other.

**Dependency direction:** Fetcher2026 depends on CC2026's `galao.db` for priority signals
(once `paths.db` is fixed to point at it). CC2026 depends on Fetcher2026's CSVs for
backtesting/analysis. Full cross-project map: `ORCHESTRATOR.md`, or the canonical
`CriticalCorallations2026\ORCHESTRATOR.md`.

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

# Bars pipeline (1s/5s/30s OHLCV, separate from TRADES/BID_ASK CSV fetch above)
python trader/bars_watchdog_supervisor.py     # top-level: keeps bars_fetch_watchdog alive forever
python trader/bars_status_server.py            # live bars status UI, port 5004
```

Full operational runbook (start/stop/check every component, port map, log map,
known issues): **`OPERATIONS.md`**.

**Task Scheduler tasks (Windows) — as designed, see caveat below:**
- `GalgoFetcher2026` — watchdog every 5 min, restarts fetch_scheduler if dead
- `GalgoDashboard2026` — dashboard every 5 min, exits if port 5050 already bound

**Caveat (found 2026-08-17, not yet fixed):** `scripts/install_scheduler.ps1`
registers `GalgoFetcher2026` with `-UserId "SYSTEM" -LogonType ServiceAccount`.
Running as SYSTEM means `%USERPROFILE%` resolves to the SYSTEM profile, not
`C:\Users\galsh`, so when this task's `fetch_watchdog.py` tries to auto-start
IB Gateway via `C:\IBC\StartGateway.bat`, IBC looks for
`config.ini` under the *SYSTEM* profile's `Documents\IBC\` (which doesn't
exist) instead of `C:\Users\galsh\Documents\IBC\config.ini` (which has the
real login). Gateway then never comes up, and the watchdog retries forever
every ~60s — see `OPERATIONS.md` "Known issue: SYSTEM-context watchdog" for
the full incident and the fix (re-run the installer with the principal
changed to the interactive user, or point IBC at an explicit config path
that doesn't depend on `%USERPROFILE%`).

---

## Current State (as of 2026-09-07)

| Item | Status |
|------|--------|
| IB Gateway (port 4002) | Up |
| `fetch_scheduler.py` | Running, live TRADES window active |
| `dashboard.py --real` (5050) | Running |
| Bars pipeline (`bars_watchdog_supervisor.py` → `bars_fetch_watchdog.py`) | Running — group `1s+30s` active, `5s` queued next |
| `bars_status_server.py` (5004) | Running |
| BID_ASK fetch | Off (`fetch_bid_ask: false` in `trader/config.yaml` as of last check — verify before assuming) |
| galao.db | Empty — no `verified_trades` table, `fetch_priority.py` still can't cross-reference trade dates |
| **MES 1s backfill** (270 trading days, → ~2025-07-17) | Complete. Ran ad-hoc via `bars1s_fetcher.py --symbol MES --days 270` in a self-restarting loop. Gaps: 2026-05-25 (Memorial Day early close) + normal per-day tail-hour gaps. |
| **15-min (900s) bars — all 4 symbols, 252 days** | Complete (MES/MNQ/M2K/MYM, 252 files each). Empty days: 2026-01-01 & 2025-12-25 (real closures) + 6 holiday-adjacent IB-quirk gap days (see Next Steps / `BARS1S_STATUS.md`). Ad-hoc, **not** watchdog-managed. |
| **Geva priority MES 1s days** | 10 previously-missing "no-trades Geva" days fetched via new `--dates` flag (2025-07-17…2025-09-29). All present; 2025-09-01 is 68,400 bars = correct Labor-Day early close. |

**Incident 2026-08-17:** Whole pipeline (gateway, scheduler, dashboard, all
three bars fetchers, status server) was down. Bars pipeline had been down
since **2026-07-29** (~19 days, all lock files stale). Root cause and
recovery: see `OPERATIONS.md` → "2026-08-17 incident" and `BARS1S_STATUS.md`
§0m. Everything was brought back up manually; the underlying SYSTEM-context
watchdog bug above is still unfixed, so Gateway could go down again the same
way if it ever needs the auto-restart path.

For 1s/5s/30s bars fetch state specifically (coverage, known IB tail-chunk
gap, repair tooling), see `BARS1S_STATUS.md` — it has its own detailed,
dated history and is the source of truth for that subsystem.

---

## Planned Next Steps

1. **Fix the SYSTEM-context watchdog bug** — either change `install_scheduler.ps1`'s
   principal to run as the interactive user, or make IBC's config path
   explicit (not `%USERPROFILE%`-relative) so it works under any account.
   Until fixed, a Gateway drop will not self-heal correctly.
2. **Find out what's currently launching the SYSTEM-session watchdog** — a
   SYSTEM-owned `python.exe` (Session 0) was found running `fetch_watchdog.py`
   on 2026-08-17, but no matching Scheduled Task or Windows Service could be
   found from a non-elevated shell, and it couldn't be killed (access denied).
   Track it down from an elevated session (Task Manager → Details, or
   Process Explorer) and reconcile it with `GalgoFetcher2026`.
3. **Continue TRADES + BID_ASK backfill / bars backfill** — now that everything
   is running again, let it catch up. Monitor http://localhost:5050 (TRADES)
   and http://localhost:5004 (bars).
4. **Fix `paths.db`** — repoint `trader/config.yaml`'s `paths.db` from the dead
   Galgo2026 copy to CC2026's live `galao.db`
   (`C:\Projects\CriticalCorallations2026\trader\data\galao.db`). Only once that's
   fixed does "wait for `verified_trades` to populate" become the right next step
   for making `fetch_priority.py` useful.
5. **Google Drive upload** — `google_drive.enabled: false`. Wire up if
   off-machine CSV backup is wanted.
6. ~~**MES 1s backfill to match Geva ground truth**~~ — **DONE (2026-09-07)**.
   270 trading days back to ~2025-07-17 via `bars1s_fetcher.py --symbol MES
   --days 270`. Gaps: 2026-05-25 (Memorial Day early close, auto-skipped) +
   the normal per-day tail-hour gaps. Plus the 10 "no-trades Geva" priority
   days (2025-07-17…2025-09-29) fetched via the new `--dates` flag.
7. ~~**MES 15-min bars, 1 year back**~~ — **DONE (2026-09-06)**, then extended
   to all 4 symbols (MNQ/M2K/MYM). 252 files each. Changes made in
   `trader/bars1s_fetcher.py` (all committed, `f85fdd1` + `56116aa`):
   - `900: ("15 mins", 86400)` added to `_BAR_SECS_TABLE` — 1 whole-session
     chunk/day, sidesteps the multi-chunk resume-verification math (assumes
     1 row/sec).
   - `is_tail_chunk` now requires `total_chunks > 1` (a 1-chunk day must not
     get the 1-crash "permanent gap" leniency meant for end-of-session gaps).
   - `durationStr` uses `"D"` units for exact-day requests (`"86400 S"` came
     back from IB shifted ~2h early, deterministically). Sub-day bar sizes
     unaffected.
   - `total_chunks == 1`: after retries are exhausted, **skip the day cleanly
     instead of `raise ConnectionError`** — the crash-and-restart path buys
     nothing for a single-request bar size and the failure it hits is 100%
     deterministic. Multi-chunk (1s/5s/30s) behaviour unchanged.
   - New `--dates YYYY-MM-DD,...` flag: fetch a priority list first, then the
     `--days` backfill resumes. Lets a priority set jump the queue without a
     second concurrent process (same bar size shares one lock/progress DB).

   **Known 15-min gap (all 4 symbols): 6 holiday-adjacent days** —
   2026-07-06, 2026-06-22, 2026-05-26, 2026-02-17, 2026-01-20, 2025-11-28,
   each the first trading day after a 3-day holiday weekend or abutting a
   short holiday week. IB deterministically returns a shifted multi-day
   window instead of the requested single day (100% reproducible across
   dozens of retries / fresh connections). A real fix means fetching the
   wider window IB wants to serve and trimming locally — decided not worth
   it for 6/252 days. `2026-01-01` & `2025-12-25` are also empty but that's
   correct (full closures). A one-off transient miss on `2025-10-09`
   (MNQ/M2K) was re-fetched successfully.
8. **15-min pipeline is ad-hoc, not watchdog-managed.** `bars_fetch_watchdog.py`
   still cycles only 1s/5s/30s. If 15-min should be kept current, add `900`
   to the watchdog's stage set + weights.

---

## Things That Bite

- **IB pacing:** shared account-wide 60 req/10min budget across every process
  connected to this IB account (fetch_scheduler, all bars fetchers, plus the
  external Galgo2026 `broker.py`/`decider.py`/`trading_dashboard.py`). See
  `BARS1S_STATUS.md` for the concurrency tuning history.
- **Empty-window trap:** Overnight CT gap (approx 17:00–23:00) returns 0 ticks
  per window. v2.5 skips 60-min chunks after N consecutive empties — don't
  regress this.
- **Lock files:** If a fetcher/scheduler crashes hard, its `data/*.lock` file
  stays and the next run refuses to start. The various watchdogs clean stale
  locks automatically (dead PID check) — a manual run doesn't, delete by hand.
- **One fetcher per bar size:** each `--bar-secs` value has its own
  lock/progress DB/output dir, so 1s + 5s + 15-min can run at once, but two
  instances of the *same* bar size cannot — the second exits on the lock.
  To prioritise specific days within a bar size, use `--dates` (runs them
  ahead of the `--days` backfill in one process), not a second process.
- **Whole-day-chunk bar sizes (900s / 15-min):** one request covers the whole
  session (`total_chunks == 1`). Holiday-adjacent days deterministically get a
  bad multi-day window from IB and are skipped, not fetched — a *later* run
  re-attempts them (they never get marked finished), so a bare restart loop
  will re-try-and-skip them every pass (~90s each). Expected, not a bug.
- **Shared lib path:** `sys.path.insert(0, _ROOT)` at top of every module. If
  root changes, all imports break silently.
- **galao.db path is wrong, not just fragile:** `paths.db` points at a dead
  pre-split copy under Galgo2026, not CC2026's live `galao.db` — see the
  relationship section above. Fixing this is `paths.db` repointing, not a
  "when Galgo2026 moves" future concern.
- **SYSTEM-context watchdog:** see caveat above — don't assume
  `GalgoFetcher2026`/Task Scheduler auto-heals Gateway correctly; it currently
  doesn't.
- **Don't touch `broker.py` / `decider.py --mode session` /
  `back-trading/trading_dashboard.py`** — these live in `CriticalCorallations2026`
  (not Galgo2026, see above), are CC2026's live trading system, share the same IB
  paper account and Gateway, and are explicitly out of scope for Fetcher2026 work.
  They do share Gateway health with us, though: if Gateway is down, they're
  disconnected too.
