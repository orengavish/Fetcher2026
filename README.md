# Fetcher2026

Standalone tick data fetcher for CME micro futures (MES/MNQ/MYM/M2K).
Split from Galgo2026 for stability — fetching only, no trading logic.

**Setting up on a fresh machine?** See `RESTART_PROJECT.md`.
**Day-to-day operations, health checks, known issues?** See `OPERATIONS.md`.
**Coordinating this repo alongside CC2026/GevaExtract?** See `ORCHESTRATOR.md`.

## Quick start

```
python dashboard.py --real                    # http://localhost:5050
python trader/fetch_scheduler.py --backfill
python trader/bars_watchdog_supervisor.py     # separate OHLCV bars pipeline, http://localhost:5004
```

## Install Task Scheduler (admin, once)

```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\Fetcher2026\scripts\install_scheduler.ps1'
```

Installs two tasks:
- **GalgoFetcher2026** — watchdog every 5 min, auto-restarts fetch_scheduler if dead
- **GalgoDashboard2026** — dashboard every 5 min, exits if port 5050 already bound

**Known bug, fix before running this installer:** it registers `GalgoFetcher2026` to run
as `SYSTEM`, which can't see the interactive user's IBC `config.ini` and breaks Gateway
auto-restart — caused a real 19-day outage. See `OPERATIONS.md` §4 before installing.

## Architecture

- `dashboard.py` — thin fetch-status UI on port 5050 (no trading, no bt scores)
- `trader/fetch_scheduler.py` — priority fetch loop (CRITICAL → RESUME → STANDARD)
- `trader/fetch_watchdog.py` — process health monitor with G19 path-filter fix
- `trader/fetcher.py` — IB historical data fetch (paper port 4002 only)
- `data/fetch_progress.db` — single source of truth; restart any time, resumes from last position
- `trader/bars_watchdog_supervisor.py` → `bars_fetch_watchdog.py` → `bars1s_fetcher.py` —
  separate OHLCV bars pipeline (watchdog cycles 1s/5s/30s; `bars1s_fetcher.py` also
  supports `--bar-secs 900` = 15-min, run ad-hoc), independent of the above; status on
  port 5004 via `bars_status_server.py`. See `OPERATIONS.md` / `BARS1S_STATUS.md` for the full map.

## Config

`trader/config.yaml` — `paths.db` currently points at a dead, empty, pre-split copy of
`galao.db` under Galgo2026, **not** CriticalCorallations2026's live one. Fix before relying
on verified-trade-count prioritization; see `ORIENTATION.md`.
