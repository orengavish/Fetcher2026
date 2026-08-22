# ARCHITECTURE_RESPONSE — Fetcher2026

> Architecture-question round only. No production code modified, nothing refactored,
> nothing restarted, IB Gateway not touched. All claims below are grounded in the actual
> code and schemas read during this round (file/line references given), not from memory of
> prior conversations about this repo, though prior operational work (`OPERATIONS.md`,
> `BARS1S_STATUS.md`, `RESTART_PROJECT.md`, `ORCHESTRATOR.md`) is cited where it's the more
> complete source for an operational detail.

---

## 1. Project identity

```text
PROJECT:       Fetcher2026
REPO PATH:     C:\Projects\Fetcher2026
ROLE:          Market-data acquisition (ticks + OHLCV bars) for CME micro futures, IB paper
               account. No strategy, no execution, no positions.
CURRENT COMMIT: 2dcfcbb96f105f2bf2007bd0e37e384369577d1f (main, 2026-08-22 09:50:41 +0300)
```

This repo answers **Part A** and **Part B** below. Parts C (GevaExtract), D (CC2026 live
execution) and E (back-trading/experiment engine) don't apply here — none of that code
lives in this repo — and are left for the respective sessions to answer. Where this
document needs to say something about CC2026's execution internals (e.g. the ID model), it
cites `C:\Projects\CriticalCorallations2026\BACK_TRADING_INTEGRATION_REPORT.md`, a forensic
report produced in a sibling round of this same effort, rather than re-deriving it.

---

## 2. Proposed responsibility (A1)

Fetcher2026 should own: connecting to IB, requesting historical and live tick/bar data for
CME micro futures, validating that a requested window was actually and completely
delivered (not silently truncated or stale), and persisting + serving that data to
whatever consumes it. It should not decide what to trade, should not place or track
orders, and should not need to know anything about any consumer's strategy logic to do its
job. The user's proposed responsibility statement is correct as stated — the gap is not in
what it claims, but in what "validate" currently means in practice (see A3, A9): today it
means *complete*, not *correct* (no outlier/gap/price-sanity checking beyond session-window
coverage).

### SHOULD OWN
- IB connection lifecycle **for its own data requests** (not Gateway process lifecycle — see A2)
- Tick CSVs (TRADES/BID_ASK) and their progress/completeness state
- OHLCV bar CSVs (1s/5s/30s) and their progress/completeness state
- Fetch scheduling, prioritization, and its own share of IB's pacing budget
- Health/status reporting for its own pipelines

### SHOULD NOT OWN
- `galao.db` / any trading, order, or P&L data — `lib/db.py` in this repo currently defines
  a **full duplicate** of CC2026's trading schema (`commands`, `positions`,
  `completed_trades`, the `verified_trades` recursive-CTE view, `spawn_replenishment()`) —
  see A3/A5. This repo does not trade; none of this should be here.
- The physical output path for its own data (`C:\Projects\Galgo2026\june\trader\data\history\`)
  — writing into another (legacy, third) project's directory tree is a packaging accident
  from before the 3-way split, not a real ownership decision.
- IB Gateway process lifecycle exclusively — it's shared with CC2026 (live paper trading)
  and GevaExtract (price reads); unilateral restart authority here is a liability, not an
  asset (see A2, A6).

---

## 3. Independence / coupling analysis (A2)

| Dependency | Why it exists | Keep / replace | Proposed future contract |
|---|---|---|---|
| Writes CSVs to `C:\Projects\Galgo2026\june\trader\data\history\` | CC2026 (formerly the pre-split Galgo2026 monolith) reads tick CSVs from there | **Replace** | Fetcher owns and serves its own `data/history/` under its own repo/service boundary; consumers request via the `MarketDataService` contract (§5), not a fixed filesystem path |
| `trader/config.yaml` → `paths.db` points at `C:\Projects\Galgo2026\june\data\galao.db` (confirmed empty, no `verified_trades` table) instead of CC2026's live one | Meant to let `fetch_priority.py` prioritize fetch dates by verified-trade activity | **Replace, not just repoint** | See below — pointing Fetcher directly at CC2026's live SQLite file is *more* coupling, not less. The right fix is a `get_priority_dates()` call against CC2026's own data API. |
| `lib/db.py` (`_SCHEMA`, `_VERIFIED_TRADES_VIEW`, `spawn_replenishment()`, `get_pending_commands()`, `get_filled_commands()`, `update_price_cache()`) — a byte-for-byte structural duplicate of CC2026's trading schema | Pre-split monolith artifact; this code is never exercised by anything Fetcher2026 actually runs (no broker/decider here) | **Archive entirely**, keep only `fetch_log` + its helpers | N/A — this isn't a contract to design, it's dead weight to delete |
| Shared IB Gateway process, port 4002 | One IB account, one Gateway, three consumers (Fetcher, CC2026, GevaExtract) | **Keep the Gateway, fix the ownership model** | A single, explicitly-owned `GatewayLifecycle` component (proposal: owned by CC2026, since it has real paper money and orders riding on it — see A6) exposes `ensure_gateway_up()` / `GatewayHealth`; **this repo currently has *two* independent, uncoordinated processes trying to manage Gateway** (`fetch_watchdog.py` and `gateway_watchdog.py`) — that duplicate, unowned authority is exactly the shape of bug that caused the real 19-day SYSTEM-context outage (`OPERATIONS.md` §4) going unnoticed until someone looked. Collapse to one owner, everywhere else becomes a consumer. |
| Shared IB historical-data pacing budget (60 req/10min, account-wide, not per-process) | Real IB constraint | **Keep the constraint, add a real allocator** | Today every IB-hitting process self-throttles independently and hopes (`_PACE_MAX_REQS` in `fetcher.py`/`bars1s_fetcher.py`, tuned by trial and incident — see `BARS1S_STATUS.md` §0d/§0i/§0k). A shared budget with no shared broker is a coordination gap, not solved infrastructure; proposal: co-locate a simple request-slot allocator with `GatewayLifecycle` since they're both properties of "the one shared Gateway," and have Fetcher/CC2026/GevaExtract request slots instead of self-throttling blind. |
| `lib/config_loader.py`, `lib/logger.py` — same names/shapes as CC2026's, but **not shared code**, independently diverged | Split-era copy-paste, not a real shared module | **Replace** | One versioned `platform_common` package (config resolution, logging, DB WAL helpers) used by every component, so a fix in one place actually fixes it everywhere — today a fix to one repo's `config_loader.py` silently does not apply to the other's, which is its own footgun class (`ORCHESTRATOR.md` §2 rule 6) |
| Bare `python` on PATH resolving to whichever interpreter a given shell happens to have | Environment drift, not code | **Replace** | Each service pinned to an explicit interpreter (venv or fully-qualified path) in its own launcher, not inherited from the calling shell — this exact ambiguity crash-looped the bars watchdog on 2026-08-17 (`BARS1S_STATUS.md` §0n) |
| `sys.path.insert(0, _ROOT)` pattern at the top of every module for `lib/` imports | No installed package, just path hacking | **Replace** | `platform_common` (above) as a proper installable package removes the need for this entirely |

---

## 4. KEEP / WRAP / REWORK / RESEARCH / ARCHIVE (A3)

| Component | Classification | Why | Future interface |
|---|---|---|---|
| `trader/fetcher.py` — session-window math, `_N_WORKERS`-parallel chunked async fetch, dedicated `fetcher_client_ids` pool separate from `live_client_ids` | **KEEP AS-IS** | Proven, handles real IB pagination/pacing; `_N_WORKERS` was deliberately reduced 8→4 after a real pacing-cascade incident — don't re-raise without re-testing | Wrap, don't touch internals |
| `bars1s_fetcher.py` core fetch/pacing/stale-window-detection/crash-recovery logic | **KEEP AS-IS** | Extensively incident-hardened — reconnect-on-disconnect, per-chunk crash-loop circuit breaker, stale/wrong-window response rejection, the "accepted permanent gap" rule for IB's unfixable last-hour quirk. Each of these came from a specific documented failure (`BARS1S_STATUS.md` §0b/§0g/§0i/§0k) — this is exactly the "proven, don't rewrite" category Golden Rule 2 describes. | WRAP: expose one clean `fetch_bars(symbol, bar_secs, date_range)` call; hide the CLI-flag surface behind it |
| `bars_fetch_watchdog.py`'s deficit-weighted group scheduler (`GROUP_WEIGHTS`, `MIN_QUANTUM_S`) | **KEEP AS-IS** | Went through two real redesigns under incident pressure (strict serial queue → weighted individual stages → weighted groups) before landing here; not obviously correct on first read, correct because it survived contact with reality | Wrap as the reference implementation of "how to time-share a scarce shared resource across sub-jobs" — likely reusable pattern for the future `GatewayLifecycle` pacing allocator too |
| `bars_watchdog_supervisor.py` (watchdog-of-a-watchdog pattern) | **KEEP AS-IS**, generalize | Simple, correct, already verified to survive a forced-kill test of the layer below it | Worth lifting into `platform_common` as a generic "keep this child process alive" primitive any future service can reuse |
| `fetch_scheduler.py`'s P0–P4 priority-tier concept | **WRAP** | The tiering idea (last-working-day always first, resume-partials next, verified-trade-weighted backfill last) is sound, but it's currently entangled with the wrong DB pointer and a legacy output path | Wrap behind a `PriorityQueue` contract; fix the entanglements as part of the wrap, not before |
| `dashboard.py` (queue/coverage-grid UI concepts) | **WRAP** | UI ideas worth keeping, but tightly coupled to this repo's internal file layout and DB shape (confirmed: its real routes are `/api/status`, `/api/queue`, `/api/files`, `/api/prices` — note `GALGO2027_HANDOFF.md` claims `/api/grid`, which does not exist in the code; that doc was wrong, a live example of why this round insists on code over docs) | Sit behind the same `FetcherHealth`/coverage contract (§5) every other consumer uses, rather than reading internal files directly |
| `scripts/install_scheduler.ps1` | **REWORK** | Actively broken, not proven — registers the watchdog task as `SYSTEM`, which caused a real 19-day outage. This is a bug, not battle-tested infrastructure; Golden Rule 2 doesn't protect it. | Fix the principal, or better: retire it in favor of `GatewayLifecycle`/its own scheduling once that exists |
| `gateway_watchdog.py` | **REWORK → likely eliminate** | Duplicates `fetch_watchdog.py`'s Gateway-management responsibility inside the same repo; two independent, uncoordinated restart authorities for the same shared resource is itself the coupling problem, not a feature | Superseded by the single shared `GatewayLifecycle` owner (§3) |
| `lib/db.py`'s trading schema (everything except `fetch_log`) | **ARCHIVE** | Confirmed dead: `commands`/`positions`/`completed_trades`/`verified_trades`/`spawn_replenishment()`/`get_pending_commands()`/`get_filled_commands()`/`update_price_cache()` are a structural copy of CC2026's live schema, but nothing in this repo's actual runtime path (fetch_scheduler → fetcher → bars fetchers → dashboard) calls any of them except `get_db()`/`fetch_log` helpers | N/A — delete on migration, don't port forward |
| `lib/gdrive.py` (Google Drive upload) | **RESEARCH ONLY** | Never wired up (`google_drive.enabled: false`), unproven | Re-evaluate need before porting; don't assume it works because it exists |
| `send_email.py` alerting | **RESEARCH ONLY / likely REWORK** | Currently a confirmed no-op (`secrets.ini` missing on the actual machine) — untested in practice for a long time, correctness unverified | Don't trust it "works" just because the calling code looks right; either fix and prove it or replace with a monitored contract-level health check |

**A9 — improvements worth doing only *after* parity, not during migration:** real
statistical/outlier validation of fetched data (today "validate" = session-window
completeness only); collapsing the three bar-size CLI-launched processes into one
parameterized job-queue service; a real venv-per-component setup to remove the bare-`python`
footgun permanently; and — explicitly — **do not "fix" `paths.db` by just repointing it at
CC2026's live SQLite file as a quick patch.** That trades one coupling bug for a worse one
(direct cross-project raw-file DB access, the exact pattern §3 recommends replacing). The
correct fix is the `get_priority_dates()` contract call in §5, which is migration work, not
a quick fix to apply now.

---

## 5. Proposed public contracts (A4 + Part B's specific asks)

If every internal file/DB/CLI-flag detail of this repo were hidden, the smallest surface
the rest of the platform actually needs:

```python
# Request/ensure data exists (may trigger a fetch — async, returns immediately)
ensure_dataset(symbol: str, kind: Literal["trades","bidask","bars_1s","bars_5s","bars_30s"],
                date_range: DateRange) -> DatasetHandle
# input: symbol + kind + date range. output: a handle, not the data itself.
# side effects: may enqueue IB fetch work. failure: never raises for "not available yet" —
# that's a normal DatasetHandle state, not an error. async. idempotent (re-requesting an
# already-fetched or already-in-flight range is a no-op, not a duplicate fetch).

# Read what's actually on disk right now, no side effects
get_dataset(symbol, kind, date_range) -> DatasetHandle | NotAvailable
# input/output as above. no side effects — pure read. sync. idempotent by definition.

# What's covered, what's missing, what's in flight — the coverage-grid concept in
# dashboard.py generalized into a real contract instead of an internal-only view
get_coverage(symbol, kind, date_range) -> CoverageReport
# output: per-day status (done/active/missing) + record counts. sync, read-only, idempotent.

# Health/status — generalizes dashboard.py's /api/status AND bars_status_server.py's
# /api/status (which already has the right shape: per-stage health/gap_s/rate_per_min —
# pipeline A's dashboard.py should be brought up to that same shape, not the other way round)
get_health() -> list[PipelineHealth]
# PipelineHealth: {pipeline, status: running|stalled|down, last_progress_at,
#                   gateway_dependency_up: bool, current_target, backlog_estimate}
# sync, read-only, idempotent, side-effect-free — safe for any orchestrator to poll freely.
```

**`DatasetHandle` / dataset identity contract (Part B Q7):** does not exist today in any
form. Files are identified only by `{symbol}_{type}_{YYYYMMDD}.csv` filename convention —
no content hash, no stamp of which fetcher-code version produced it, no record of *when*
it was fetched relative to IB's own data finalization (relevant given the confirmed,
permanent "last hour of the most recent session" gap documented in `BARS1S_STATUS.md`
§0k — a file fetched immediately after a session close is verifiably incomplete in a way
that re-fetching later might fix, and nothing today records that). Golden Rule 4 requires
`dataset_id/version` for any future experiment to prove what data it used — proposed
minimum: `{symbol, kind, date, fetcher_code_version, fetched_at, row_count,
known_gaps: [...]}` as a manifest sidecar per file (or a manifest table), populated at
write time, immutable after.

**`FetcherHealth` contract (Part B Q11):** the shape above (`get_health()`). The bars
pipeline's `/api/status` is close to this already — it's the reference implementation to
generalize *from*, not a gap to fill from scratch.

**`MarketDataService` contract:** the four functions above, taken together.

---

## 6. Data ownership (A5)

| Asset | Current owner (physical location) | Current readers | Current writers | Proposed future owner |
|---|---|---|---|---|
| Tick CSVs (TRADES/BID_ASK) | `C:\Projects\Galgo2026\june\trader\data\history\` — **physically inside a third, legacy repo** | CC2026 (reads directly from this path) | Fetcher2026 only | **Fetcher2026**, served via `MarketDataService`, not a shared filesystem path |
| `fetch_progress.db` | Same Galgo2026-adjacent directory (`Path(cfg.paths.db).parent`) | Fetcher2026 only | Fetcher2026 only | Fetcher2026, no change needed except relocating out of Galgo2026's tree |
| Bars CSVs (1s/5s/30s) | `C:\Projects\Fetcher2026\data\bars{1,5,30}s\` | Fetcher2026's own dashboard; external consumers (CorrelationAnalyzer per `BARS1S_STATUS.md` §0h) read files directly today | Fetcher2026 only | Fetcher2026, already correctly located — this one's fine as-is, just needs the `MarketDataService` contract layered on top for non-file consumers |
| `bars{1,5,30}s_progress.db`, `bars_watchdog_schedule.json`, `*_chunk_failures.json` | `data/` in this repo | Fetcher2026's own watchdogs | Fetcher2026's own watchdogs | Fetcher2026 — pure internal runtime state, never meant to be read externally or migrated across machines (confirmed incident: stale `time_spent` carried across an outage caused the §0n stuck-stage bug) |
| `trader/config.yaml`'s `paths.db` target — **a dead, empty, pre-split copy of `galao.db`** | `C:\Projects\Galgo2026\june\data\galao.db` | `fetch_priority.py` (reads, gets nothing useful) | Nobody currently writes to it in any observed run | **Not Fetcher2026's to own at all** — this file shouldn't exist as a Fetcher2026 dependency; the real live `galao.db` is CC2026's, and per §3 the fix is a contract call, not a repointed path |
| `lib/db.py`'s trading schema (commands, positions, completed_trades, verified_trades) | Structurally present in this repo's own `lib/db.py`, but never populated by anything Fetcher2026 runs | Nobody (in this repo) | Nobody (in this repo) | **Nobody — archive, see §4** |

---

## 7. Failure boundaries (A6)

If Fetcher2026 fails entirely (both pipelines down):

- **CC2026 (live paper execution):** unaffected in real time — it doesn't call into
  Fetcher2026 synchronously for anything (confirmed: no cross-process call from
  `broker.py`/`decider.py` into this repo). It would eventually be affected only if its own
  historical-data needs (backtesting, `bars.db` refresh) depend on fresh Fetcher output —
  that's a batch/offline dependency, not a live one.
- **Experiments/algorithm analysis:** blocked from getting *new* data, but historical data
  already on disk remains fully usable — this is the right shape (data producer down ≠
  data consumer down), and should be preserved explicitly in any redesign.
- **Paper/live execution:** unaffected directly. It shares Gateway health with Fetcher2026,
  but Fetcher2026 going down doesn't take Gateway down with it (today) — that should remain
  true; if Fetcher2026 becomes the sole owner of `GatewayLifecycle` in some future design,
  that would be the wrong choice for exactly this reason (§3 already proposes CC2026 as
  Gateway owner instead).
- **Dashboards:** Fetcher's own dashboards (5050/5004) go dark; nothing else does, since
  nothing else reads from them synchronously today.
- **IB Gateway itself:** should be completely unaffected by Fetcher2026 dying — and per §3,
  Fetcher2026 shouldn't have unilateral restart authority over it regardless.
- **Stored state:** progress DBs are durable (SQLite) and resumable by design — a Fetcher2026
  crash loses at most the current in-flight chunk, confirmed safe-to-kill-anytime by design
  (`BARS1S_STATUS.md` §9 / `OPERATIONS.md`).

Conversely, if **Gateway** fails: both of Fetcher2026's pipelines correctly stall and wait
(resumable), rather than corrupting state — this was specifically hardened after the 2026-08-17
incident and the earlier connection-loss incident (`BARS1S_STATUS.md` §0g). This is the
right failure mode to preserve: **Gateway down should degrade Fetcher2026 to "waiting," never
to "silently wrong."**

---

## 8. Migration strategy (A7)

```text
wrap fetcher.py / bars1s_fetcher.py behind MarketDataService (§5)
  → regression test against current CSV output (byte-identical for a fixed date range)
  → stand up the new dataset-manifest/identity layer alongside existing files (additive)
  → shadow: point a read-only consumer (e.g. a new experiment-engine prototype) at the new
    contract while CC2026 keeps reading the legacy CSV path directly
  → compare: confirm the contract-served data matches the legacy files for the same range
  → switch consumers one at a time (CC2026's fetch/priority code first, since it's the only
    real cross-repo consumer today) to the new contract
  → only once nothing reads the legacy Galgo2026 path anymore, retire that path and archive
    lib/db.py's trading schema
```

Reversible at every step through "switch consumers": the legacy file path keeps working
until the last consumer is moved off it, so a bad contract implementation can be rolled
back by just pointing consumers back at files. The one non-reversible step is deleting
`lib/db.py`'s trading schema and the legacy output path — do that last, after confirming
via `git log`/grep that nothing (including any script not covered by this round's search)
still imports those functions or reads that path.

---

## 9. DO NOT LOSE list (A8)

1. **`_N_WORKERS=4` and the reasoning behind it** — deliberately reduced from 8 after a real
   pacing-cascade incident (`trader/fetcher.py:59`). Don't re-raise without re-testing.
2. **The empty-window skip logic** (v2.5+) that clears the 17:00–23:00 CT overnight dead
   zone in ~15 min instead of grinding through it chunk by chunk — `ORIENTATION.md` "Things
   That Bite."
3. **`_reconcile_progress()`'s startup heal** (`trader/fetch_scheduler.py` ~line 205) — fixes
   hard-kill corruption where a fetch completed but the DB never got the `finished=1` write.
   Silent removal would reintroduce every historical "looks stuck, actually done" bug this
   was written to fix.
4. **The "accepted permanent gap" rule** for the last ~1hr of the most recent CME session
   (`BARS1S_STATUS.md` §0k) — without it, zero days can ever reach `finished=1`, and the
   dashboard silently looks broken even though it isn't. This is a real, confirmed IB
   limitation, not a bug to "eventually fix" by retrying harder.
5. **Per-chunk crash-loop circuit breaker with tail-chunk-specific tuning**
   (`_MAX_CHUNK_CRASHES`, `is_tail_chunk` in `bars1s_fetcher.py`, `BARS1S_STATUS.md` §0i/§0j)
   — distinguishes "genuinely stuck, skip and move on" from "real outage, be patient."
6. **The `got_valid_result` unified-rejection rule** (`BARS1S_STATUS.md` §0g) — a chunk's
   progress is *only* ever saved if a response was positively validated; every rejection
   path (dead connection, pacing violation, stale/wrong-window) leaves it `False`. This is
   the single most important correctness invariant in the bars fetcher — it directly
   prevents the silent-corruption class of bug that cost real data twice before it existed.
7. **The deficit-weighted group scheduler's persistence of `time_spent` across restarts**
   (`data/bars_watchdog_schedule.json`) — but paired with the lesson from §0n: it *must* be
   reset after any extended outage, or it starves the wrong stage for a long time. Preserve
   both the mechanism and that specific operational caveat.
8. **Dedicated `fetcher_client_ids` pool, separate from `live_client_ids`/`paper_client_ids`**
   (`trader/config.yaml`, `trader/fetcher.py:690`) — keeps this repo's own connections from
   colliding with anything else touching the same IB account.
9. **Watchdog-of-a-watchdog pattern** (`bars_watchdog_supervisor.py`) — verified by an actual
   forced-kill test to recover within ~16s. Simple, and proven under an intentional adversarial
   test, not just code review.
10. **The stale-lock dead-PID-check pattern**, repeated across every watchdog in this repo —
    the reason a hard crash never permanently wedges the next start attempt. Losing this
    reintroduces "delete the lock file by hand" as normal operating procedure instead of an
    edge case.

---

## 10. Role-specific answers — Part B (Fetcher2026)

**1. Independently runnable service, package, or both?**
Both, but not as currently coupled. Runnable standalone for local dev/backfill (no
dependency on CC2026 or GevaExtract being up), *and* embeddable as a package once
`MarketDataService` exists as a real contract rather than files-on-disk-by-convention. The
two pipelines (TRADES/BID_ASK vs bars) should stay independently runnable from each other
too — they already are (separate lock files, separate progress DBs, no shared state) and
that independence is worth preserving explicitly, not just as an accident of history.

**2. Canonical contract per data type?**
Same shape for all five (TRADES, BID_ASK, 1s/5s/30s bars) — `{symbol, kind, date,
rows/bars, coverage_pct, known_gaps, dataset_manifest}` — kind is a discriminator, not a
reason for five different contracts. Today's reality is close: all five already share the
same CSV-per-symbol-per-day convention and a progress-DB-per-kind pattern; formalizing the
shared shape is mostly naming the existing convention, not inventing a new one.

**3. Who should own historical data files/database?**
Fetcher2026 — see §6. Today it's split across two repos' directory trees for no reason
except split-era inertia; that should end.

**4. Eliminate legacy cross-repo paths without changing fetch behavior?**
Additive first: start writing new fetches to a Fetcher2026-owned path *in addition to* the
legacy Galgo2026 path (dual-write), verify parity, migrate CC2026's read path to the new
location or the contract, then stop the legacy write. Never a hard cutover in one step —
CC2026 reading a suddenly-empty legacy directory is a real, avoidable outage.

**5. How should a consumer request existing/missing/update/coverage?**
Via the four `MarketDataService` functions in §5. Today there is no request mechanism at
all — consumers either read files directly (CC2026) or poll a dashboard's internal JSON
API not designed as a stable contract (`dashboard.py`, `bars_status_server.py`).

**6. Should consumers ever cause an IB fetch directly?**
No. `ensure_dataset()` is the only trigger path. Direct IB access from a consumer
re-creates the exact multi-process pacing-budget collision documented in
`BARS1S_STATUS.md` §0d (three of Fetcher's own processes plus one external dashboard
caused an ~88% request failure rate) — that was Fetcher's own processes colliding with
each other; a consumer bypassing Fetcher entirely to hit IB directly is the same failure
mode with an extra, harder-to-see participant.

**7. Dataset identity/versioning?**
Does not exist today — see the `DatasetHandle` manifest proposal in §5. Necessary for
Golden Rule 4 (`dataset_id/version` for reproducible experiments) and directly motivated by
a real, confirmed gap: the permanent last-hour session gap (§9 item 4) means two files with
the same filename can have genuinely different content-completeness depending on *when*
they were fetched relative to IB's data finalization, and nothing today records that.

**8. How does existing progress/recovery logic map into the future platform?**
Almost unchanged as the *implementation* behind `ensure_dataset()`/`get_coverage()` — the
progress-DB-per-pipeline pattern, the reconciliation-on-startup heal, and the
watchdog/supervisor chain are all sound and should sit directly behind the new contract,
not be replaced by it.

**9. Which scheduler/watchdog/gap/pacing mechanisms must remain untouched initially?**
Everything in §9 (DO NOT LOSE), plus the P0–P4 priority tiering concept in
`fetch_scheduler.py` (wrap, don't rewrite the tiering logic itself).

**10. IB-pacing coordination needed when sharing the account with execution + GevaExtract?**
A real shared allocator, not independent self-throttling — see §3's pacing-budget proposal.
This is not solved today; it's solved *per-process* by trial-and-error tuning
(`_PACE_MAX_REQS` values chosen empirically across several incidents), which works only
because the current set of concurrent consumers happens to be small and mostly known.

**11. What health/status API should the orchestrator depend upon?**
`get_health()` from §5, generalizing the bars pipeline's existing `/api/status` shape
(already good: per-stage health/gap_s/rate_per_min) to also cover the TRADES/BID_ASK
pipeline, which today reports a different, less structured shape.

**12. Can both existing Fetcher pipelines remain internally independent?**
Yes, and they should stay that way — no shared state between them today beyond the Gateway
connection itself, and that independence has already paid off operationally (one pipeline's
19-day outage in `BARS1S_STATUS.md` §0m and the other's stuck-stage bug in §0n were each
independently diagnosable and fixable without touching the other).

**13. Safest migration of the current legacy history paths?**
Dual-write → verify parity → migrate the one known consumer (CC2026) → retire the legacy
path. Same shape as §8, this is that same migration applied specifically to the tick-CSV
output path.

**14. What should happen if Fetcher is down while live execution is running?**
Nothing should happen to live execution — see §7. It should keep running on whatever
market data it already has cached/fetched; only *new* backtesting/analysis work that needs
fresh data should visibly wait, not fail.

**15. Which improvements are worth doing after migration, not during?**
See A9 in §4.

**Proposed contracts, restated together** (already detailed in §5):
```text
MarketDataService: ensure_dataset() / get_dataset() / get_coverage() / get_health()
Dataset identity: {symbol, kind, date, fetcher_code_version, fetched_at, row_count, known_gaps}
Fetcher health: {pipeline, status, last_progress_at, gateway_dependency_up, current_target, backlog_estimate}
```

---

## 11. Questions / requirements for sibling projects (Part F)

```text
TO EXECUTION (CC2026):
I need `paths.db`'s current role (verified-trade-weighted fetch prioritization) replaced by
a real call, not a shared file. Can your proposed contract expose a
`get_verified_trade_dates(symbol, date_range) -> list[date]`-shaped read, so Fetcher never
opens galao.db directly?

TO EXECUTION (CC2026):
Per the back-trading integration report's §5 finding (no stored logical-trade identity,
`root_cmd_id` computed at query time via recursive CTE over `commands.parent_command_id`) —
does your proposed `LogicalTrade` identity design also apply to anything Fetcher-adjacent,
or is it purely internal to execution? I want to confirm Fetcher never needs to understand
command/order ID lineage, only trade *dates and outcomes* for prioritization.

TO EXECUTION (CC2026):
Who should own `GatewayLifecycle`? §3/§6 of this document proposes CC2026, on the reasoning
that it has real paper positions/orders at stake and the most to lose from an uncoordinated
restart. Do you agree, or is there a reason Gateway ownership should sit elsewhere (a
fourth, dedicated infra component)?

TO EXECUTION (CC2026):
If Fetcher2026 shares an IB pacing-budget allocator with you (§3), what's your peak expected
request rate during a live session, so the allocator's default split isn't chosen blind?

TO GEVAEXTRACT:
You currently read prices — via Fetcher's data, via CC2026's `galao.db` price_cache, or
directly from IB? If directly from IB, that's a fourth uncoordinated pacing-budget consumer
not accounted for in this document's proposed allocator — please confirm which it is.

TO EXPERIMENT ENGINE (back-trading):
`ensure_dataset()`/`get_dataset()` in §5 are proposed as async/sync-read respectively — does
an experiment runner need a *blocking* "fetch and wait" variant for on-demand backtesting
against not-yet-fetched historical ranges, or is "queue it, poll `get_coverage()`" an
acceptable pattern for your use case?

TO EXPERIMENT ENGINE (back-trading):
The dataset-identity manifest in §5 (`fetcher_code_version`, `fetched_at`, `known_gaps`) is
designed to satisfy Golden Rule 4's `dataset_id/version` requirement from Fetcher's side —
does this give you enough to fully specify an experiment's data provenance, or do you need
additional fields (e.g. a content hash, or IB's own data-vintage timestamp if that's ever
exposed)?

TO EXPERIMENT ENGINE (back-trading):
The confirmed permanent last-hour-of-session gap (`BARS1S_STATUS.md` §0k) means some bars
data is *structurally* incomplete, forever, for the most recent hour of any given session.
Does your simulator/backtester need to know about this explicitly (e.g. via `known_gaps` in
the manifest), or is silently-shorter-than-expected daily bar data acceptable for your
purposes?

TO CENTRAL ARCHITECT:
This document proposes CC2026 as `GatewayLifecycle` owner rather than a new, separate
infra component. Is a shared-infra owner allowed to *be* one of the three product
components, or does Golden Rule 1's "shared infrastructure must have explicit ownership"
imply a fourth, infra-only component is required?

TO CENTRAL ARCHITECT:
`lib/db.py`'s trading-schema duplication (§4/§6) is proposed for outright deletion, not
migration. Is there any reason to preserve it (e.g. as a reference implementation, or
because something outside this round's search still imports it) before that happens?
```

---

## 12. Risks / disagreements (Part G)

1. **Disagreement with the global objective diagram, mild:** the linear pipeline
   (`Market Data → Algorithms → Experiment Engine → ... → Execution`) implies data flows
   one direction only. In practice, execution *produces* data Fetcher-adjacent components
   care about too (verified trade dates, for prioritization) — that's a feedback edge from
   Execution back toward Market Data's consumers, not a strict one-way pipeline. Worth the
   central architect explicitly drawing that edge rather than leaving it implicit.
2. **Requirement that would force unnecessary coupling:** if any sibling assumes it can
   read Fetcher's SQLite progress DBs directly (the way CC2026 currently reads tick CSVs
   directly), that recreates exactly the coupling this document argues against. The
   `MarketDataService` contract only works if it's the *only* sanctioned access path.
3. **Existing mechanism at risk from "maximum independence":** the deficit-weighted bars
   scheduler (§4/§9) coordinates *within* Fetcher2026 across three bar-size fetchers
   sharing one pacing budget. If "independent" is interpreted as "each bar-size fetcher
   becomes its own fully separate component," that specific proven coordination mechanism
   has nowhere to live. Recommend: independence at the Fetcher2026-service boundary, not
   necessarily at the sub-pipeline level.
4. **Performance concern:** none identified specific to this repo beyond the existing,
   already-documented and already-tuned IB pacing constraints. Splitting Fetcher into more
   granular services (per bar-size, say) would *add* coordination overhead without a clear
   performance win, given the bottleneck is IB's pacing limit, not local compute.
5. **Reliability concern:** the proposed `GatewayLifecycle` consolidation (§3) is a net
   reliability improvement (single owner, single source of truth) but is itself a new
   single point of failure for three consumers where there were previously — accidentally —
   two independent (redundant, if uncoordinated) restart attempts. Worth the central
   architect deciding whether that component itself needs its own watchdog-of-watchdog
   treatment (§9 item 9's pattern is a candidate).
6. **Migration risk:** the dual-write/shadow/compare/switch pattern (§8) is safe but assumes
   someone actually verifies parity before switching consumers — the historical record in
   this repo (`BARS1S_STATUS.md` §0b/§0g) shows that *silent* data corruption during a
   transition is a real, repeated failure mode here, not a hypothetical one. Explicit
   automated parity checks, not manual spot-checks, should gate each "switch consumer" step.
7. **Unresolved question for the central architect:** whether `platform_common` (shared
   config/logging/DB-WAL helpers, §3) is itself in scope for this round, or a separate,
   later effort. This document assumes it's necessary but doesn't attempt to design it.

---

## 13. Recommended architecture decisions

- Fetcher2026 becomes the sole owner of all CME tick/bar data, served via
  `MarketDataService` (§5), regardless of physical file location.
- `GatewayLifecycle` (Gateway process management + pacing-budget allocation) becomes a
  single, explicitly-owned component — proposed owner CC2026, open for the architect/CC2026
  session to confirm or override (§11).
- `lib/db.py`'s trading-schema duplication is deleted, not migrated, once nothing depends
  on it (verify via search first, per Golden Rule "evidence over assumption").
- Dataset identity/versioning (the `DatasetHandle` manifest, §5) is a new capability, not
  present today, required for Golden Rule 4 and directly justified by the confirmed
  permanent-gap finding (§9 item 4).
- `platform_common` (shared config/logging/DB helpers) is recommended but out of scope to
  design in this round (§12 risk 7).

## 14. Open decisions requiring the central architect

- Who owns `GatewayLifecycle` — a product component (CC2026, as proposed) or a new,
  dedicated infra-only fourth component (§11, §12 risk 2)?
- Whether `platform_common` is in scope now or a later effort (§12 risk 7).
- Whether the Market-Data→Execution feedback edge (verified-trade dates influencing fetch
  priority) belongs in the platform's core pipeline diagram or stays a documented
  point-to-point exception (§12 risk 1).

---

# MESSAGE TO CENTRAL ARCHITECT

Fetcher2026 is close to the target shape already: two independently-runnable, incident-
hardened pipelines with proven pacing/crash-recovery logic (keep all of it, see the DO NOT
LOSE list). The real problems are boundary problems, not algorithm problems — a legacy
output path physically inside a third repo, a `paths.db` pointer aimed at a dead pre-split
`galao.db` copy, and a `lib/db.py` that carries a full, unused duplicate of CC2026's trading
schema (commands/positions/completed_trades/verified_trades) from before the 3-way split.
None of that is proven infrastructure worth preserving — it's split-era debt worth deleting.

Two decisions need your call, not mine: **who owns IB Gateway's lifecycle and its shared
pacing budget** (I've proposed CC2026, since it has real paper positions at stake — but this
repo currently has *two* uncoordinated processes trying to manage Gateway themselves, which
is itself evidence nobody has actually decided this yet); and **whether a shared
`platform_common` package is in scope for this round** (config/logging/DB-helper code has
already diverged once between this repo and CC2026 despite near-identical names — that's a
footgun, not a feature).

The one new capability this repo needs that doesn't exist anywhere today: **dataset
identity/versioning**. Files are identified only by a filename convention
(`{symbol}_{type}_{date}.csv`), with no record of which fetcher-code-version produced them
or whether a file was fetched before IB finalized that data. This matters concretely: IB
has a confirmed, permanent gap in the last ~1 hour of the most recently completed session
for every bar size — not a bug, verified across weeks of production data — so two files with
the same name can differ in actual completeness depending on *when* they were fetched. Any
experiment-reproducibility requirement (Golden Rule 4) needs this fixed before it can trust
Fetcher's output as an input.

Everything else is a contract-wrapping exercise, not a rewrite. My biggest risk flag: the
migration history in this specific repo shows *silent* data corruption during transitions is
a real, repeated failure mode here (two separate incidents, `BARS1S_STATUS.md` §0b/§0g) —
whatever migration process the architect settles on platform-wide, it needs automated parity
verification at each cutover step, not manual review, or this repo's specific history will
repeat itself during the rebuild.
