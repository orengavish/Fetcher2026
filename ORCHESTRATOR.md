# ORCHESTRATOR — Fetcher2026's Role, for a Higher-Level Claude Code Session Coordinating This Project Alongside Its Siblings
> For a "meta" Claude Code session (or human) operating multiple Claude Code instances in
> parallel across this project and related algo projects — not for a session working only
> inside this repo. If you're just going to `cd` into Fetcher2026 and stay there, read
> `ORIENTATION.md` and `OPERATIONS.md` instead.
>
> **The canonical, ecosystem-wide version of this doc is
> `C:\Projects\CriticalCorallations2026\ORCHESTRATOR.md`** (written 2026-08-18) — it covers
> the full map, all "golden rules" for running multiple sessions in parallel, the standing
> known-issues list across all three projects, and the coordination protocol. Read that one
> first. **This file is the short version scoped to what an orchestrator specifically needs
> to know about *this* repo** — Fetcher2026's interfaces, what it owns, what it must never
> touch, and its own health-check commands — so a coordinating session doesn't have to
> re-derive them by reading Fetcher2026's source.
>
> Written 2026-08-22.

---

## 1. What Fetcher2026 is, in one paragraph

A standalone, fetch-only data pipeline for CME micro futures (MES/MNQ/MYM/M2K) from IB
Gateway (paper, port 4002). No trading logic, no positions, no broker/decider. Two
independent pipelines: (A) TRADES/BID_ASK tick CSVs, feeding CC2026/legacy consumers, and
(B) OHLCV bars (1s/5s/30s), feeding local/CorrelationAnalyzer-style consumers. Full detail:
`ORIENTATION.md`.

## 2. Interfaces an orchestrator can rely on

| Interface | What it tells you |
|---|---|
| `http://localhost:5050` / `/api/status` | Pipeline A (TRADES/BID_ASK) dashboard + JSON status: fetcher PID, gateway up/down, scheduler health |
| `http://localhost:5004/api/status` | Pipeline B (bars) status: per-stage (1s/5s/30s) health, gap seconds, throughput |
| `data/fetch_scheduler.lock`, `data/bars*_fetcher.lock`, `data/bars_watchdog_supervisor.lock` | PID lock files — stale lock (dead PID) means that component isn't actually running even if you expected it to be |
| `logs/fetch_scheduler.log`, `logs/fetch_watchdog.log`, `logs/bars_fetch_watchdog.log`, `data/bars*_run.log` | Ground truth for "is this actually making progress" — check `LastWriteTime`, don't trust a port being open alone |

Full port/log/lock table: `OPERATIONS.md` §6.

## 3. What Fetcher2026 owns vs. shares

**Owns (safe to restart/manage from here without asking another project first):**
- `fetch_scheduler.py`, `fetch_watchdog.py`, `fetcher.py` (pipeline A)
- `bars_watchdog_supervisor.py`, `bars_fetch_watchdog.py`, `bars1s_fetcher.py`,
  `bars_status_server.py` (pipeline B)
- `dashboard.py` (port 5050)

**Shares — coordinate before restarting:**
- **IB Gateway (port 4002)** — also used live by CC2026's `broker.py`/`decider.py`
  (real trading, not simulated) and GevaExtract's price reads. Restarting Gateway is not
  free for them. See CC2026's `ORCHESTRATOR.md` golden rule #1 on the shared 60-req/10-min
  pacing budget before launching any additional concurrent IB-hitting process.
- **`C:\Projects\Galgo2026\june\trader\data\history\`** — Fetcher2026 writes here, CC2026
  reads from here (legacy path predating the 3-way split). Don't relocate without checking
  CC2026's fetch/priority code first.

**Never touch from a Fetcher2026-focused session or orchestrator action:** `broker.py`,
`decider.py --mode session`, `back-trading/trading_dashboard.py` — CC2026's live trading
processes. They share Gateway health with Fetcher2026 (if Gateway is down, they're
disconnected too) but are otherwise completely out of scope here.

## 4. Fetcher2026-specific known issues (don't re-diagnose, already documented)

| Issue | Where | Status |
|---|---|---|
| SYSTEM-context Task Scheduler principal breaks Gateway auto-restart, caused a 19-day outage | `OPERATIONS.md` §4, `BARS1S_STATUS.md` §0m | Diagnosed, not fixed — needs an elevated session |
| `trader/config.yaml` → `paths.db` points at a dead, empty pre-split `galao.db` instead of CC2026's live one | `RESTART_PROJECT.md` §4 | Breaks `fetch_priority.py`'s verified-trades cross-reference silently; not fixed |
| Bars scheduler's `time_spent` weights don't get reset after an extended outage, starving a stage that should be catching up | `BARS1S_STATUS.md` §0n, `OPERATIONS.md` §2a | Fixed once (2026-08-17), documented as a required step after any future extended-outage recovery — verify it was actually done before trusting stage rotation post-outage |
| Mystery SYSTEM-session `python.exe` running `fetch_watchdog.py` that no `Get-ScheduledTask`/`Win32_Service` query could identify | `OPERATIONS.md` §4, `BARS1S_STATUS.md` §0m | Unresolved — if seen again, needs an elevated Task Manager/Process Explorer session to trace |

## 5. Quick health check (Fetcher2026 only)

```powershell
foreach ($p in 4002,5050,5004) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) { "$p LISTENING" } else { "$p NOT LISTENING" }
}
Get-ChildItem logs\*.log, data\*.log | Select-Object Name, LastWriteTime | Sort-Object LastWriteTime -Descending | Select-Object -First 8
```

If a port is listening but its log hasn't moved in the last several minutes while
component-specific work should be in flight, treat it as stalled, not healthy — see
`OPERATIONS.md` §2 for the full check and §3 for how to restart each piece.

## 6. If you're the orchestrator and need to act here specifically

- Restarting pipeline A or B independently is safe and expected — they don't share state
  with each other, only Gateway.
- Before restarting Gateway itself, check §3 above and CC2026's `ORCHESTRATOR.md` §4
  ("before any session restarts a shared dependency...") — Fetcher2026's bars pipeline
  survives a Gateway bounce fine (resumes from its own progress DB), but CC2026's live
  broker does not get the same free pass.
- This repo pushes to its own `origin` (`github.com/orengavish/Fetcher2026`) — independent
  from CC2026's `cc2026` remote and GevaExtract's `origin`. A push here never affects them.
- Version/state claims in any of this repo's docs can drift between sessions the same way
  CC2026's did (see CC2026 `ORCHESTRATOR.md` §4) — check `git log --oneline -5` in this repo
  directly rather than trusting a doc's "current state" table at face value before acting on it.
