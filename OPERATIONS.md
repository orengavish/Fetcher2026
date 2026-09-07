# OPERATIONS — Fetcher2026 Runbook
> How to check, start, and stop every moving part in this repo, plus known issues.
> Last updated: 2026-08-17. For narrative history/incidents see `ORIENTATION.md` (overall) and `BARS1S_STATUS.md` (bars pipeline specifically).

---

## 1. Architecture summary

Fetcher2026 has **two independent pipelines** that both depend on the same IB
Gateway connection but otherwise don't interact:

**A. TRADES/BID_ASK tick CSV pipeline** (feeds Galgo2026's tick archive)
```
fetch_watchdog.py  →  fetch_scheduler.py  →  fetcher.py  →  IB Gateway (port 4002)
                                                  ↓
                          C:\Projects\Galgo2026\june\trader\data\history\*.csv
```

**B. OHLCV bars pipeline** (watchdog cycles 1s / 5s / 30s; `bars1s_fetcher.py`
also does `--bar-secs 900` = 15-min ad-hoc — separate output dir per size)
```
bars_watchdog_supervisor.py → bars_fetch_watchdog.py → bars1s_fetcher.py → IB Gateway (port 4002)
                                                              ↓
                                  C:\Projects\Fetcher2026\data\bars{1,5,30}s\*.csv  (+ bars900s\ ad-hoc)
```

Both pipelines have their own dashboards:
- `dashboard.py --real` — port **5050** — pipeline A status
- `bars_status_server.py` — port **5004** — pipeline B status

Everything above ultimately needs **IB Gateway** up on **port 4002** (paper
account). Gateway itself is launched via IBC (`C:\IBC\StartGateway.bat`),
which needs the interactive user's `config.ini`
(`C:\Users\galsh\Documents\IBC\config.ini`) — see §4 for why this matters.

**Also sharing the same IB account/Gateway, not part of this repo, do not
touch:** `broker.py`, `decider.py --mode session`,
`back-trading/trading_dashboard.py` — these live in `CriticalCorallations2026`
(the "CC2026" trading brain, port 5003), not Galgo2026 (Galgo2026 is legacy,
see `ORIENTATION.md` "My Relationship with Sibling Projects").

**New here or restarting from a fresh machine?** See `RESTART_PROJECT.md`.
**Coordinating this repo alongside CC2026/GevaExtract?** See `ORCHESTRATOR.md`.

**`paths.db` (config.yaml) vs. the live trading DB:** `paths.db` points at
this repo's own progress-tracking `galao.db` (and derives `fetch_progress.db`'s
location) — it is intentionally NOT the real trading database. The live,
actively-written `galao.db` (verified trades, positions, etc.) lives at
`C:\Projects\CriticalCorallations2026\trader\data\galao.db`. The only place in
this repo that reads that live DB is `trader/fetch_priority.py`, via CC2026's
`get_priority_dates()` (loaded directly from CC2026's `lib/db.py` by file
path, not `cfg.paths.db`) — every other `cfg.paths.db` consumer in this repo
is unaffected and still points at Fetcher2026's own DB. See bugs 3/11 in
`C:\Projects\All\plan.md`.

---

## 2. How to check what's running

```powershell
# Is Gateway up?
Test-NetConnection -ComputerName localhost -Port 4002

# Is dashboard / bars status server up?
Test-NetConnection -ComputerName localhost -Port 5050
Test-NetConnection -ComputerName localhost -Port 5004

# Any of our processes alive right now, with command line (run from an elevated
# shell if you need to see SYSTEM-owned processes too — see §5)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine

# Lock files (existence + PID) — a stale lock (dead PID) blocks a fresh manual
# start until removed; the watchdogs clean these automatically
Get-Content data\fetch_scheduler.lock, data\bars5s_fetcher.lock, `
  data\bars1s_fetcher.lock, data\bars30s_fetcher.lock, `
  data\bars_watchdog_supervisor.lock -ErrorAction SilentlyContinue

# Latest activity per component
Get-ChildItem logs\*.log, data\*.log | Select-Object Name, LastWriteTime | Sort-Object LastWriteTime -Descending
```

Web UIs: http://localhost:5050 (TRADES/BID_ASK) · http://localhost:5004 (bars)

---

## 2a. Post-outage recovery checklist addendum (2026-08-17, see BARS1S_STATUS.md §0n)

After bringing the bars pipeline chain back up from any extended outage, also reset
`data/bars_watchdog_schedule.json`'s `time_spent` counters to `0` for every stage —
otherwise a stage with a large pre-outage accumulated value (e.g. `5s` after the §0m
19-day gap) looks artificially "already over its target share" to the deficit-weighted
scheduler and gets starved for a long time even though it should be catching up. Do this
with the chain stopped (edit while `bars_fetch_watchdog.py` isn't running, to avoid a
write race), then restart via `bars_watchdog_supervisor.py` as usual.

Also: always launch these scripts with the fully-qualified interpreter path below, not a
bare `python` — a bare `python` resolves per-shell and can silently pick up an unrelated
project's virtualenv missing `psutil`, which crash-loops `bars_fetch_watchdog.py` every
10s under the supervisor's own restart loop.

---

## 3. How to start everything (manual, foreground-safe)

Run from `C:\Projects\Fetcher2026`. Each of these runs forever (its own
internal loop) — background them (`&` in PowerShell, `nohup ... &` in bash,
or a separate terminal) if not left in a dedicated window.

```powershell
# 0. Gateway first — everything else needs this. As the INTERACTIVE user
#    (not via a SYSTEM-context task — see §4).
C:\IBC\StartGateway.bat paper
# wait ~30-60s, then confirm: Test-NetConnection localhost -Port 4002

# 1. TRADES/BID_ASK pipeline
python trader/fetch_scheduler.py --backfill
python dashboard.py --real

# 2. Bars pipeline — start only the supervisor, it cascades the rest
python trader/bars_watchdog_supervisor.py
python trader/bars_status_server.py
```

You do **not** need to start `fetch_watchdog.py` or `bars_fetch_watchdog.py`
by hand in the normal case — the supervisor starts the watchdog, and the
watchdog starts/restarts the actual fetcher. Only run them standalone for
debugging (`--once` flag on most of these for a single check-and-exit).

---

## 4. Known issue: SYSTEM-context watchdog breaks Gateway auto-restart

**Symptom:** IB Gateway is down and won't come back up on its own, even
though something is clearly trying (logs show repeated
`Gateway DOWN ... attempting restart` / `Gateway did not come up after 90s`
every ~1-5 min, forever).

**Root cause:** `scripts/install_scheduler.ps1` registers the
`GalgoFetcher2026` Task Scheduler task with
`-UserId "SYSTEM" -LogonType ServiceAccount`. A process running as SYSTEM has
`%USERPROFILE%` pointing at the SYSTEM profile
(`C:\WINDOWS\system32\config\systemprofile`), not `C:\Users\galsh`. IBC's
`StartGateway.bat` builds its config path as `%USERPROFILE%\Documents\IBC\config.ini`
— under SYSTEM that file doesn't exist, so IBC aborts before Gateway even
launches. Confirmed directly in `C:\IBC\Logs\IBC-3.24.0_GATEWAY-*.txt`:
```
Error: IBC configuration file: C:\WINDOWS\system32\config\systemprofile\Documents\IBC\config.ini  does not exist
```
The real config (with actual paper-account login) only exists at
`C:\Users\galsh\Documents\IBC\config.ini`.

**Diagnosed 2026-08-17, not yet fixed.** Two ways to fix it, either works:

- **Option A (simplest):** change the task's principal to run as the
  interactive user instead of SYSTEM. In `scripts/install_scheduler.ps1`,
  replace:
  ```powershell
  $P1 = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  ```
  with a principal for `galsh` (`-LogonType Interactive` or `Password`,
  depending on whether the task should run whether-logged-on-or-not), then
  re-run the installer as admin.
- **Option B:** make IBC's config path explicit instead of
  `%USERPROFILE%`-relative — edit the `set CONFIG=` line in
  `C:\IBC\StartGateway.bat` to a fixed path
  (`C:\Users\galsh\Documents\IBC\config.ini`), which works regardless of which
  account launches it. (Note: this is IBC's own bat file, not part of this
  repo — a future IBC reinstall/update could overwrite it.)

**Also unresolved:** on 2026-08-17 a SYSTEM-session (Session 0) `python.exe`
running `fetch_watchdog.py` was found actively thrashing Gateway this way,
but it didn't match any task found via `Get-ScheduledTask` / `schtasks /query
/v` / `Get-CimInstance Win32_Service` from a non-elevated shell, and couldn't
be killed (`Stop-Process` / `taskkill` → Access Denied, despite the account
being a local admin — UAC token filtering blocks a non-elevated shell from
touching SYSTEM processes). If you hit this again: open an **elevated**
PowerShell or Task Manager (Details tab) to find and kill the SYSTEM-owned
`python.exe`, and reconcile it with whatever's registered as
`GalgoFetcher2026` (`Get-ScheduledTask -TaskName GalgoFetcher2026 |
Get-ScheduledTaskInfo` from an elevated prompt should see it even if a
non-elevated one can't).

**Workaround that doesn't require fixing the above:** the broken watchdog
only interferes when it *thinks* Gateway is down. If you manually launch
Gateway as the interactive user and it comes up, the watchdog's next check
will see it healthy and leave it alone — no need to kill anything to recover.
That's what was done on 2026-08-17 (see `BARS1S_STATUS.md` §0m).

---

## 5. 2026-08-17 incident (summary)

Whole pipeline found down: Gateway, `fetch_scheduler.py`, `dashboard.py`, all
three bars fetchers, `bars_status_server.py`. The bars pipeline specifically
had been silently down since **2026-07-29** (~19 days, no crash trace — just
never relaunched). Cause: the SYSTEM-context watchdog bug in §4, which had
Gateway stuck in a permanent failed-restart loop, so nothing downstream could
connect. Fixed by manually relaunching Gateway as the interactive user (which
the existing broken watchdog then left alone once healthy) and manually
starting all four downstream components. Everything confirmed live and
fetching real data afterward. Full detail in `BARS1S_STATUS.md` §0m and
`ORIENTATION.md` "Current State".

**Not done, left for follow-up:** the §4 fix itself (still misconfigured),
and identifying the mystery SYSTEM-session process precisely.

---

## 6. Port / log / lock reference

| Component | Port | Log | Lock |
|---|---|---|---|
| IB Gateway | 4002 | `C:\IBC\Logs\IBC-*.txt` | — |
| `fetch_scheduler.py` | — | `logs/fetch_scheduler.log` | `data/fetch_scheduler.lock` |
| `fetch_watchdog.py` | — | `logs/fetch_watchdog.log` | — |
| `dashboard.py` | 5050 | console only | — |
| `bars_watchdog_supervisor.py` | — | `data/bars_watchdog_supervisor.log` | `data/bars_watchdog_supervisor.lock` |
| `bars_fetch_watchdog.py` | — | `logs/bars_fetch_watchdog.log`, `data/bars_fetch_watchdog_run*.log` | — |
| `bars1s_fetcher.py` (1s) | — | `data/bars1s_run.log` | `data/bars1s_fetcher.lock` |
| `bars1s_fetcher.py --bar-secs 5` | — | `data/bars5s_run.log` | `data/bars5s_fetcher.lock` |
| `bars1s_fetcher.py --bar-secs 30` | — | `data/bars30s_run.log` | `data/bars30s_fetcher.lock` |
| `bars1s_fetcher.py --bar-secs 900` (15-min, ad-hoc) | — | console / `data/bars900s_*` | `data/bars900s_fetcher.lock` |
| `bars_status_server.py` | 5004 | `data/bars_status_server.log` | — |
