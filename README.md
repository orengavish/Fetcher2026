# Fetcher2026

Standalone tick data fetcher for CME micro futures (MES/MNQ/MYM).
Split from Galgo2026 for stability — fetching only, no trading logic.

## Quick start

```
python dashboard.py --real        # http://localhost:5050
python trader/fetch_scheduler.py --backfill
```

## Install Task Scheduler (admin, once)

```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\Fetcher2026\scripts\install_scheduler.ps1'
```

Installs two tasks:
- **GalgoFetcher2026** — watchdog every 5 min, auto-restarts fetch_scheduler if dead
- **GalgoDashboard2026** — dashboard every 5 min, exits if port 5050 already bound

## Architecture

- `dashboard.py` — thin fetch-status UI on port 5050 (no trading, no bt scores)
- `trader/fetch_scheduler.py` — priority fetch loop (CRITICAL → RESUME → STANDARD)
- `trader/fetch_watchdog.py` — process health monitor with G19 path-filter fix
- `trader/fetcher.py` — IB historical data fetch (paper port 4002 only)
- `data/fetch_progress.db` — single source of truth; restart any time, resumes from last position

## Config

`trader/config.yaml` — update `paths.db` to point to galao.db for verified trade counts.
