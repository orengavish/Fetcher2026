# RESTART_PROJECT — Bootstrapping Fetcher2026 on a Fresh PC
> **This repo does not stand alone in production.** It depends on a shared IB Gateway that
> two sibling repos (`CriticalCorallations2026`, `GevaExtract`) also depend on, and its
> live output currently lands under a third, legacy repo (`Galgo2026`'s `june\` subtree —
> see "Known path debt" below). If you're bootstrapping the **whole system** (all three
> live projects + shared Gateway), use the canonical, more thorough doc instead:
> **`C:\Projects\CriticalCorallations2026\RESTART_PROJECT.md`** — written 2026-08-18,
> covers all three repos, IBC/Gateway install with known-bug fixes, Task Scheduler state,
> and data that must be re-created by hand. This file is the condensed **"I only need
> Fetcher2026"** path (e.g. a dev machine that just needs tick data, not the live trading
> brain) plus a pointer to the parts you can't skip even so.
>
> Written 2026-08-22, verified against the actual running system.

---

## 0. What this repo needs, at minimum

Fetcher2026 has **two independent pipelines**, both needing IB Gateway on port 4002:

| Pipeline | Entry point | Writes to |
|---|---|---|
| TRADES/BID_ASK tick CSVs | `trader/fetch_scheduler.py` (watched by `trader/fetch_watchdog.py`) | `C:\Projects\Galgo2026\june\trader\data\history\*.csv` (legacy path, see below) |
| OHLCV bars (1s/5s/30s) | `trader/bars_watchdog_supervisor.py` → `bars_fetch_watchdog.py` → `bars1s_fetcher.py` | `C:\Projects\Fetcher2026\data\bars{1,5,30}s\*.csv` (local) |

Full architecture/runbook once running: **`OPERATIONS.md`** in this repo.

---

## 1. Prerequisites

- **Windows** — Task Scheduler + IBC's `.bat` launchers are assumed throughout.
- **Python 3.11**, added to PATH. Always launch this repo's scripts with the
  **fully-qualified interpreter path** (e.g.
  `C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe`), not a bare
  `python` — a bare `python` resolves per-shell and can silently pick up an unrelated
  project's interpreter missing `psutil`, which crash-loops the bars watchdog. This exact
  mistake happened on 2026-08-17 (see `BARS1S_STATUS.md` §0n).
- **Git**.
- **IB Gateway + IBC**, shared with the sibling projects — see §3.

## 2. Clone and install

```powershell
git clone https://github.com/orengavish/Fetcher2026.git C:\Projects\Fetcher2026
cd C:\Projects\Fetcher2026
pip install -r requirements.txt
python -c "import flask, ib_insync, psutil, yaml, requests; print('ok')"
```

`requirements.txt` in this repo is accurate and sufficient for Fetcher2026 alone
(`ib_insync`, `flask`, `psutil`, `requests`, `pyyaml`, plus Google Drive libs that are
unused while `google_drive.enabled: false`).

## 3. IB Gateway + IBC

Don't reinstall this per-repo if CC2026 or GevaExtract are already set up on the same
machine — it's one shared Gateway. If this is a genuinely fresh machine, follow
`CriticalCorallations2026\RESTART_PROJECT.md` §5 in full — it documents a real,
already-hit installer bug (Windows' `cmd.exe` no longer resolving a bare `java.exe` via
cwd, breaking IBC's Java-version detection) and the exact fix. Condensed version:

1. Install IB Gateway (not full TWS) from interactivebrokers.com.
2. Install IBC from `github.com/IbcAlpha/IBC/releases` to `C:\IBC\` — this repo's
   `trader/config.yaml` hardcodes `ib.ibc_startgateway_bat: "C:\\IBC\\StartGateway.bat"`.
3. Create `C:\Users\<you>\Documents\IBC\config.ini` by hand (not in git, has real
   credentials) with `TradingMode=paper`, `IbLoginId=...`, `IbPassword=...`.
4. Launch with `C:\IBC\StartGateway.bat /INLINE /COLOR` as the **interactive user** —
   not via a SYSTEM-context scheduled task. Verify: `Get-NetTCPConnection -LocalPort 4002
   -State Listen`, and `C:\IBC\Logs\IBC-*.txt` should show `IBC: Login has completed`.

**Do not register the Task Scheduler auto-restart (`scripts\install_scheduler.ps1`) with
its current `-UserId "SYSTEM"` principal without fixing it first** — this caused a real
19-day silent outage (2026-07-29 → 2026-08-17, `BARS1S_STATUS.md` §0m). SYSTEM's
`%USERPROFILE%` doesn't resolve to your profile, so IBC can't find `config.ini` and
Gateway auto-restart silently fails forever. Fix before installing: change the principal
in `scripts\install_scheduler.ps1` to the interactive user, or make IBC's `StartGateway.bat`
config path explicit instead of `%USERPROFILE%`-relative. Full detail: `OPERATIONS.md` §4.

## 4. Config that needs manual attention

| Setting | File | Notes |
|---|---|---|
| `paths.db` | `trader/config.yaml` | Points at `C:\Projects\Galgo2026\june\data\galao.db` — a **dead, empty, pre-split copy**, not CC2026's live trading DB (`C:\Projects\CriticalCorallations2026\trader\data\galao.db`). `fetch_priority.py`'s verified-trades cross-reference silently does nothing useful until this is repointed. Not fixed as of this writing — a deliberate-or-not path debt, verify current status before assuming it's fixed. |
| `paths.history` | `trader/config.yaml` | `C:\Projects\Galgo2026\june\trader\data\history\` — legacy path outside this repo, kept because CC2026 reads from there too. Don't "clean up" without checking who else depends on it. |
| `ib.ibc_startgateway_bat` | `trader/config.yaml` | Must match wherever IBC is actually installed (`C:\IBC\` by convention across all sibling projects). |
| `google_drive.*` | `trader/config.yaml` | Disabled (`enabled: false`), no credentials wired up — leave as-is unless off-machine backup is wanted. |
| `secrets.ini` (Gmail SMTP, for `send_email.py` alerts) | repo root | **Not present on the current machine — not in git.** Email alerting is a known no-op until this exists. Format: `[gmail]\nuser = ...\napp_password = ...` |

## 5. Data that won't come from `git clone`

- **Tick-CSV history** — not in git (too large), lands under the legacy `Galgo2026\june\...`
  path above. Re-backfill with `python trader/fetch_scheduler.py --backfill` once Gateway
  is up; expect this to take real time given IB's pacing limits (see `BARS1S_STATUS.md` for
  the tuning history).
- **Bars CSVs** (`data/bars{1,5,30}s/`) — gitignored, local only. Re-backfill via the bars
  pipeline (§6). Losing them just means the watchdog resumes a backfill from day 1 again —
  no corruption risk, just time.
- **Progress DBs / lock files / `data/bars_watchdog_schedule.json`** — runtime state, not
  meant to be migrated from an old machine. Let them start fresh (`time_spent: 0`) rather
  than copying stale values over — copying a stale `bars_watchdog_schedule.json` across a
  long gap is exactly what caused the §0n stuck-stage bug in `BARS1S_STATUS.md`.

## 6. Start-up order

```powershell
$py = "C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe"   # fully-qualified, see §1
cd C:\Projects\Fetcher2026

# 0. Gateway first (skip if already up from a sibling project)
C:\IBC\StartGateway.bat /INLINE /COLOR
# wait ~30-60s, confirm: Get-NetTCPConnection -LocalPort 4002 -State Listen

# 1. TRADES/BID_ASK pipeline
Start-Process $py -ArgumentList "trader/fetch_scheduler.py --backfill" -WorkingDirectory . -WindowStyle Hidden
Start-Process $py -ArgumentList "dashboard.py --real" -WorkingDirectory . -WindowStyle Hidden

# 2. Bars pipeline (supervisor cascades the rest)
Start-Process $py -ArgumentList "trader/bars_watchdog_supervisor.py" -WorkingDirectory . -WindowStyle Hidden
Start-Process $py -ArgumentList "trader/bars_status_server.py" -WorkingDirectory . -WindowStyle Hidden
```

## 7. Verify

```powershell
foreach ($p in 4002,5050,5004) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) { "$p LISTENING" } else { "$p NOT LISTENING" }
}
Invoke-WebRequest http://localhost:5050 -UseBasicParsing | Select-Object -Expand StatusCode
Invoke-WebRequest http://localhost:5004/api/status -UseBasicParsing
```

Ongoing operations, log/lock/port reference, and the known-issues list:
**`OPERATIONS.md`** in this repo. Cross-project coordination (this repo alongside
CC2026/GevaExtract): **`ORCHESTRATOR.md`** in this repo, or the canonical
**`CriticalCorallations2026\ORCHESTRATOR.md`**.

## 8. Permissions

`.claude/settings.local.json` in this repo sets `"defaultMode": "bypassPermissions"` —
no confirmation prompts within a Claude Code session. Carry this forward on a fresh
checkout if you want the same working style.
