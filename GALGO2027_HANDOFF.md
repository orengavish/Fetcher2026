# Fetcher2026 — Galgo2027 Handoff Document
> **Merge-planning doc, not current operational truth.** Written 2026-07-21 for the
> Galgo2027 consolidation effort (started 2026-07-21/22, incomplete/stale as of last check —
> see `CriticalCorallations2026\ORCHESTRATOR.md` §5). Several facts below are now outdated:
> the bars pipeline (§2/§7 don't mention it — it didn't exist yet), the Task Scheduler
> status in §7 (see `OPERATIONS.md` §4 for the actual, still-unfixed SYSTEM-principal bug),
> and the "brother project" framing throughout (`ORIENTATION.md` now correctly identifies
> CC2026, not Galgo2026, as the live trading brain). For **current** operational state, use
> `ORIENTATION.md` + `OPERATIONS.md`; for a **fresh-machine restart**, use
> `RESTART_PROJECT.md`. This doc's architectural analysis (§9-15, the Galgo2027 fit
> assessment) is still useful context for that future merge and is kept as-is.
>
> Complete briefing for a fresh Claude instance. No prior context needed.
> Written: 2026-07-21 | Version at time of writing: v3.1

---

## 1. PURPOSE

Fetcher2026 is a **standalone, 24/7 tick-data fetcher** for CME micro futures. It pulls historical
and live tick data from Interactive Brokers (IB paper gateway, port 4002) and writes CSV files that
CriticalCorallations2026 (CC2026) reads for chart analysis and backtesting.

It does **not trade**. It has no broker, no decider, no positions. Fetching only.

---

## 2. DISK LAYOUT

```
C:\Projects\Fetcher2026\
  dashboard.py               Flask dashboard — port 5050
  send_email.py              Email utility (alerts)
  ORIENTATION.md             Living brief — keep updated when state changes
  lib/
    config_loader.py         Loads trader/config.yaml → typed config object
    db.py                    SQLite helpers (get_db context manager)
    gdrive.py                Google Drive upload client (currently disabled)
    ibc_launcher.py          Starts IBC gateway via C:\IBC\StartGateway.bat
    ib_client.py             IB EClient/EWrapper connection manager
    logger.py                UTC timestamps, consistent format
  trader/
    config.yaml              ALL tunables — never hardcode paths/ports/symbols
    fetch_scheduler.py       Main 24/7 loop: priority queue → fetch → verify → upload
    fetcher.py               Low-level IB tick fetcher (paginated async windows)
    fetch_priority.py        CLI: shows dates with verified_trades but missing CSVs
    fetch_watchdog.py        Watchdog: restarts scheduler if dead
    gateway_watchdog.py      Watchdog: keeps IBC gateway alive
    verify_data.py           Post-fetch CSV correctness checks
    validate_fetch.py        Additional validation helpers
  scripts/
    run_dashboard.bat        Batch launcher for dashboard
  data/
    fetch_scheduler.lock     PID lock — auto-managed; delete manually if stale
    fetch_progress.db        ← 0 bytes, UNUSED (real one is in Galgo2026/june/data/)
    history/                 ← EMPTY, UNUSED (real output is in Galgo2026/june/trader/data/history/)
  logs/                      Scheduler and fetcher logs
```

---

## 3. DATA PATHS (CRITICAL — SHARED WITH OLD GALGO2026)

**Fetcher writes CSVs to:**
```
C:\Projects\Galgo2026\june\trader\data\history\
```
Pattern: `{SYMBOL}_{type}_{YYYYMMDD}.csv`
Examples: `MES_trades_20260718.csv`, `MNQ_trades_live.csv`

**Fetcher reads galao.db for priority signals from:**
```
C:\Projects\Galgo2026\june\data\galao.db
```
(This is the OLD Galgo2026 monolith DB — CC2026's live trading uses a different galao.db)

**Fetcher's own progress DB (auto-created at runtime):**
```
C:\Projects\Galgo2026\june\data\fetch_progress.db
```
Table: `fetch_progress(symbol, date, data_type, records_fetched, finished, updated_at)`

**IMPORTANT FOR GALGO2027:** These paths are legacy from before the split. Galgo2027 should unify
data ownership: one history folder, one galao.db, one fetch_progress.db — all under Galgo2027.

---

## 4. CONFIGURATION (trader/config.yaml)

```yaml
ib:
  live_port: 4002           # paper gateway — data AND orders
  fetcher_client_ids: [801, 802, 803, 804]   # 4 parallel workers
  ibc_startgateway_bat: "C:\\IBC\\StartGateway.bat"
  ibc_mode: paper

symbols: [MES, MNQ, MYM, M2K]

fetcher:
  fetch_bid_ask: false          # TRADES-only mode as of 2026-07-21
  backfill_bid_ask: false
  fetch_live_trades: true       # 23.5h rolling live window
  live_window_hours: 23.5
  live_refresh_minutes: 60
  backfill_days: 180
  symbols_override: [MES, MNQ, MYM, M2K]

paths:
  db: C:\Projects\Galgo2026\june\data\galao.db
  history: C:\Projects\Galgo2026\june\trader\data\history
  logs: C:\Projects\Fetcher2026\logs

google_drive:
  enabled: false              # not wired up yet

visualizer:
  port: 5050
```

---

## 5. WORKING FUNCTIONALITY

### Priority Queue (fetch_scheduler.py → _get_priority_dates)
```
P0  Last working day — all 4 symbols, always fetched first
P1  M2K catch-up — M2K was added late, needs backfill before general pass
P2  verified_trades dates — dates where trades happened but CSVs are missing
    (ordered by trade count DESC — highest-value dates first)
P3  Resume partials — fetch_progress rows with finished=0, records>0
P4  Standard backfill — last 180 trading days, most-recent first
    (P4a: TRADES done, BID_ASK missing → fast path)
    (P4b: TRADES missing → full fetch)
```

### Live Rolling Window
- Fetches a 23.5h TRADES window for all 4 symbols every 60 min
- Output: `{SYM}_trades_live.csv` (overwritten each refresh)
- Represents "current session" — CC2026 reads these for real-time data

### Startup Heal (reconcile_progress)
- On every startup: scans fetch_progress for finished=0 rows where the CSV actually
  covers the full session. Marks them finished=1 so they don't re-enter the queue.
- Fixes hard-kill corruption (taskkill, scheduler timeout, reboot mid-fetch).

### Lock File
- `data/fetch_scheduler.lock` — contains PID of running scheduler
- Atomic O_CREAT|O_EXCL creation prevents double-run
- On startup: checks if PID in lock is alive; if not, removes stale lock automatically
- If scheduler crashes hard and lock is stale: `Remove-Item data\fetch_scheduler.lock`

### IBC Gateway Management
- Scheduler starts gateway on launch (if not already up), waits up to 60s
- On shutdown: gateway stays running (watchdog manages it independently)
- Use `--no-keep-gateway` only for maintenance

---

## 6. DASHBOARD (dashboard.py — port 5050)

Flask app. Browser: `http://localhost:5050`

**API endpoints used internally:**
- `/api/queue` — priority queue with tier/status/reason per row
- `/api/grid` — coverage grid: done/active/missing per symbol×date×dtype
- `/api/status` — fetcher PID, gateway up/down, scheduler health

**What it shows:**
- Queue tab: P0/P1/P2/P3/P4 rows, each showing sym, date, dtype, status (done/active/pending)
- Grid tab: symbol × recent dates matrix showing coverage
- Fetcher PID and gateway status LED in header

**Cross-dashboard menu** (🔗 icon): links to CC2026 (port 5003) and GevaExtract (port 5005)
- Uses `location.hostname` so it works from localhost / LAN / Tailscale unchanged

---

## 7. SCHEDULERS AND WATCHDOGS

### Windows Task Scheduler (registered tasks)
| Task | Trigger | Command |
|------|---------|---------|
| `GalgoFetcher2026` | Every 5 min | Starts `fetch_scheduler.py` if not running |
| `GalgoDashboard2026` | Every 5 min | Starts `dashboard.py` if port 5050 not bound |

**Status:** These tasks may or may not be currently registered. Verify with:
```powershell
schtasks /query /tn "GalgoFetcher2026"
schtasks /query /tn "GalgoDashboard2026"
```
Install script: `scripts/install_scheduler.ps1` (must run elevated)

### fetch_watchdog.py
- Standalone watchdog that polls for the scheduler process and restarts if dead
- Can run independently of Task Scheduler as a fallback

### gateway_watchdog.py
- Polls IB port 4002 every 60s; relaunches IBC if gateway goes down
- Runs independently — start it once at boot and leave it

---

## 8. RULES AND INVARIANTS

1. **Paper only.** IB paper port 4002 only. Live port 4001 is NEVER connected here.
2. **Config is truth.** No hardcoded paths, ports, or symbols anywhere. All in `trader/config.yaml`.
3. **Lock before run.** Only one scheduler instance runs at a time. Lock is atomic.
4. **4 workers max.** `fetcher_client_ids: [801, 802, 803, 804]` — don't raise without testing IB pacing.
5. **Reconcile on startup.** Always call `_reconcile_progress()` to heal zombie rows before fetching.
6. **Empty-window skip.** v2.5+: after N consecutive empty 60-min windows, skip the window. Prevents
   grinding through the 17:00–23:00 CT overnight gap. Do not regress this.
7. **BID_ASK off for now.** `fetch_bid_ask: false` as of 2026-07-21. BID_ASK files are very large
   (~214MB each) and slow. Re-enable only after TRADES backfill is complete.
8. **Live CSV is temporary.** `{SYM}_trades_live.csv` is overwritten every hour. It is NOT a final
   historical file — don't validate or archive it.
9. **Galao.db path is hardcoded in config.** If Galgo2026 moves, update `paths.db` in config.yaml.

---

## 9. KNOWN GOTCHAS

- **Two galao.db files exist**: `C:\Projects\Galgo2026\june\data\galao.db` (Fetcher's priority DB,
  empty — no verified_trades table) vs `C:\Projects\CriticalCorallations2026\trader\data\galao.db`
  (CC2026's live trading DB). Fetcher only reads the first one for priority; CC2026 uses the second.
  This split is a legacy artifact from the Galgo2026 monolith split in July 2026.

- **Fetcher writes to Galgo2026 paths.** The output directory is still
  `C:\Projects\Galgo2026\june\trader\data\history\`. This is intentional (CC2026 reads from there)
  but fragile — if Galgo2026 moves, update `paths.history`.

- **fetch_progress.db in Fetcher2026/data is 0 bytes and unused.** Real one is in Galgo2026/june/data/.

- **psutil required.** The lock file check uses psutil. If missing: `pip install psutil`.

- **IB pacing.** 4 parallel workers is the tested limit. If IB returns "pacing violation" errors,
  reduce `fetcher_client_ids` to 2 or 3.

- **Monday P0 bug (fixed v2.3).** `_last_working_day()` in the scheduler is smart: if today is a
  weekday past 17:00 CT, returns today. Otherwise walks back to last weekday. The dashboard's
  `_last_working_day()` always returns yesterday — slight display discrepancy, not a bug.

---

## 10. PERMISSIONS (Claude Code settings)

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": ["Bash(*)", "PowerShell(*)", "Write(*)", "Edit(*)", "Skill(run)"]
  }
}
```
**For Galgo2027: allow all, no confirmation prompts.**

---

## 11. GIT

```
Repo:    C:\Projects\Fetcher2026
Remote:  origin → GitHub (orengavish/Fetcher2026 or similar)
Branch:  main
User:    Oren Gavish
```

---

## 12. WHAT TO ARCHIVE (DO NOT CARRY INTO GALGO2027 AS-IS)

| Item | Action |
|------|--------|
| `data/fetch_progress.db` (0 bytes) | Delete — it's the wrong location |
| `data/history/` (empty) | Delete — output goes to Galgo2026 paths |
| Fetcher2026 as standalone project | Merge into Galgo2027 as a module/subprocess |
| `C:\Projects\Galgo2026\june\data\galao.db` | Migrate verified_trades (if any) to Galgo2027 DB |
| Split output paths | Unify under Galgo2027 — one history dir, one galao.db |

**Keep:**
- `trader/fetch_scheduler.py` — priority logic is solid, port it
- `trader/fetcher.py` — IB pagination logic
- `trader/fetch_watchdog.py`, `gateway_watchdog.py` — watchdog patterns
- `lib/ibc_launcher.py`, `lib/ib_client.py` — IB connection management
- `dashboard.py` — UI concepts (queue + grid view)

---

## 13. FIT INTO GALGO2027

Fetcher becomes an **internal service** in Galgo2027:

1. **Run as managed subprocess** — Galgo2027's main process starts/stops the fetcher
2. **Unified paths** — write to `C:\Projects\Galgo2027\data\history\`; read from `Galgo2027\data\galao.db`
3. **Single fetch_progress.db** — owned by Galgo2027, not Galgo2026
4. **Watchdog** — absorb gateway_watchdog + fetch_watchdog into Galgo2027's supervisor
5. **Dashboard** — merge the queue/grid view into Galgo2027's unified dashboard (suggest port 5000)
6. **Priority based on live verified_trades** — once CC2026's trading populates galao.db, P2 priority
   becomes meaningful. In Galgo2027, Fetcher and Trader share one galao.db, so this works naturally.

---

## 14. QUICK RESTART REFERENCE

```powershell
# Kill stale fetcher + clear lock
Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*fetch_scheduler*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item C:\Projects\Fetcher2026\data\fetch_scheduler.lock -ErrorAction SilentlyContinue

# Start fetcher (background)
Start-Process python -ArgumentList "trader/fetch_scheduler.py" -WorkingDirectory "C:\Projects\Fetcher2026" -WindowStyle Hidden

# Start dashboard (background)
Start-Process python -ArgumentList "dashboard.py" -WorkingDirectory "C:\Projects\Fetcher2026" -WindowStyle Hidden

# Check gateway
python -c "import socket; s=socket.create_connection(('127.0.0.1',4002),3); print('Gateway UP'); s.close()"

# Check dashboard
Invoke-WebRequest http://localhost:5050 -UseBasicParsing | Select -Expand StatusCode
```
