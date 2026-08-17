# bars1s_fetcher — Status & Restart Reference
> Written: 2026-07-22 | Updated: 2026-08-17 ~05:35 UTC | Script: v1.6 | Watchdog v3.0 | State: pipeline restarted after 19-day outage; 1s + 30s running CONCURRENTLY, 5s queued next

## 0m. UPDATE 2026-08-17 ~05:35 UTC — whole pipeline down for ~19 days (since 2026-07-29), IB Gateway stuck in a broken auto-restart loop, everything manually recovered

**Found on check-in**: nothing in Fetcher2026 was running — not Gateway, not
`fetch_scheduler.py`, not `dashboard.py`, not the bars supervisor chain, not
`bars_status_server.py`. All progress DBs and lock files were stale. The bars
pipeline specifically (`bars_watchdog_supervisor.py` → `bars_fetch_watchdog.py`
→ `bars1s_fetcher.py`) hadn't logged anything since **2026-07-29 ~01:29 UTC**
(the §0l handoff above) — a ~19-day silent outage, not a crash-and-stay-down
so much as never having been relaunched after whatever stopped it that day.

**Root cause of why it couldn't self-heal**: a separate `fetch_watchdog.py`
process (the older TRADES/BID_ASK-scheduler watchdog, not the bars one) *was*
running and *was* actively trying, every ~60s, to bring IB Gateway back up on
port 4002 — but failing every single time. Traced to
`scripts/install_scheduler.ps1`: it registers the `GalgoFetcher2026` Task
Scheduler task with `-UserId "SYSTEM" -LogonType ServiceAccount`. Running as
SYSTEM means `%USERPROFILE%` resolves to the SYSTEM profile
(`C:\WINDOWS\system32\config\systemprofile`), not `C:\Users\galsh` — so when
it calls IBC's `C:\IBC\StartGateway.bat`, IBC looks for
`config.ini` at `...\systemprofile\Documents\IBC\config.ini`, which doesn't
exist (confirmed directly in `C:\IBC\Logs\IBC-3.24.0_GATEWAY-1048_MONDAY.txt`:
`Error: IBC configuration file: C:\WINDOWS\system32\...\config.ini does not
exist`). The real config with actual login credentials lives at
`C:\Users\galsh\Documents\IBC\config.ini`, only reachable under the
interactive user's own profile. Every restart attempt aborted before Gateway
even launched, the watchdog waited 90s, gave up, and tried again 60s later —
forever, without ever succeeding. **This is a real bug, not yet fixed** — see
`OPERATIONS.md` for the recommended fix (change the task principal, or make
IBC's config path explicit instead of `%USERPROFILE%`-relative).

**A SYSTEM-session (Session 0) `python.exe` running `fetch_watchdog.py` was
found alive and doing this thrashing**, but no matching Scheduled Task or
Windows Service could be found from a non-elevated shell (`Get-ScheduledTask`,
`schtasks /query /v`, and `Get-CimInstance Win32_Service` all came back empty
for anything referencing `fetch_watchdog`/`Fetcher2026`/`gateway`), and the
process couldn't be killed either (`Stop-Process`/`taskkill` both returned
Access Denied even though the account is a local admin — UAC token filtering
on a non-elevated shell). **Not resolved** — needs an elevated session to
identify and reconcile with `GalgoFetcher2026`.

**Recovery performed** (didn't require killing the stuck process — it only
interferes when it thinks Gateway is down, so getting Gateway up and keeping
it up was enough):
1. Manually launched `C:\IBC\StartGateway.bat paper` as the interactive user
   (correct profile → correct config.ini). IBC log confirms a clean login:
   `IBC: Login has completed`, account `DUK803831`, window `PAPER 4002`.
2. Started `trader/fetch_scheduler.py --backfill` — connected immediately,
   resumed live TRADES fetching (confirmed real ticks flowing for MES).
3. Started `dashboard.py --real` (port 5050) — confirmed serving.
4. Started `trader/bars_watchdog_supervisor.py` — came up clean, launched
   `bars_fetch_watchdog.py`, which correctly resumed the `1s+30s` group
   (cleaned one stale 5s lock from a dead PID) and both fetchers immediately
   showed real progress (1s: `MES 2026-08-14` climbing chunk by chunk; 30s:
   same, and hit the known §0k tail-chunk quirk on schedule, which self-healed
   as designed).
5. Started `trader/bars_status_server.py` (port 5004) — confirmed serving.

**Verified at handoff**: ports 4002 (Gateway), 5050 (dashboard), 5004 (bars
status) all confirmed listening; `fetch_scheduler.py` log showing real tick
counts; `bars1s_fetcher.py`/30s log showing real chunk progress against
`MES 2026-08-14`.

**Known unresolved risk**: the SYSTEM-context watchdog bug above means if
Gateway drops again for any reason, the existing auto-restart path will not
bring it back — someone (or a future Claude session) will need to notice and
relaunch `C:\IBC\StartGateway.bat` manually (or fix the installer first).

---

## 0l. UPDATE 2026-07-24 ~09:56 UTC — 1s + 30s now run concurrently; 1s scope cut to 84 days

**Requested**: shrink 1s's scope from 252 to 84 trading days, and run 1s and
30s concurrently (machine has plenty of CPU headroom). Worth noting: the
constraint we've been managing all along is IB's account-wide **API rate
limit**, not CPU — these fetchers are I/O-bound (waiting on network +
pacing sleeps), so extra cores don't directly help throughput. What *does*
make this pairing safe is that 30s is very light on the shared pacing
budget (only 3 requests/day/symbol vs 1s's 48), so running it alongside 1s
doesn't meaningfully recreate the 3-way collapse from §0d — that collapse
came from 3 *heavy* fetchers each fighting for the same budget.

**Changes**:
- `STAGE_CONFIG["1s"]`: `days` 252 → 84 (`--days 84`).
- `bars1s_fetcher.py` gained `--pace-max N`, overriding `_PACE_MAX_REQS` per
  invocation. 1s runs with `--pace-max 30`, 30s with `--pace-max 10` — split
  from the validated-safe solo budget of 40 (§0k) so the pair together still
  fits comfortably under IB's real ~60/10-min ceiling with headroom to
  spare for external consumers on this machine.
- **`bars_fetch_watchdog.py` rewritten (v3.0) around GROUPS, not individual
  stages**: `GROUPS = {"5s": ["5s"], "1s+30s": ["1s", "30s"]}`. The weighted
  scheduler (§0i/§0j) now picks a *group* every `MIN_QUANTUM_S`, not a single
  stage — `GROUP_WEIGHTS = {"5s": 0.8, "1s+30s": 0.2}`. When `1s+30s` is
  picked, **both** processes run at once; 5s never runs alongside them
  (still exactly one *group* active at a time — just a group can now have 2
  members). Per-suffix `WEIGHTS` (0.8/0.1/0.1) is still derived and kept for
  `bars_overnight_report.py`'s reporting, even though scheduling itself only
  operates on groups now.
- `check_and_heal()` reworked to iterate per-active-member: starts/kills/
  stall-checks/bogus-checks each member of the current group independently,
  instead of assuming a single stage.

**Verified live**: `--once` run correctly launched both `bars1s_fetcher.py
--days 84 --pace-max 30` (1s) and `--bar-secs 30 --days 252 --pace-max 10`
(30s) simultaneously; both immediately showed real, independent progress
(1s: `[1/336]`, confirming the new 84×4=336-pair scope; 30s: real chunk
fetches on its own timeline). Restarted the supervisor afterward — it
correctly recognized both already-running processes as the current group's
members without killing/restarting either (`ok — group=1s+30s
running=['1s', '30s']`).

---

## 0k. UPDATE 2026-07-24 ~08:30 UTC — real overnight bottleneck found and fixed: not a crash, a self-inflicted throttle + a permanent IB gap nobody was allowed to accept

Woken up to "still looking very bad." Investigated properly instead of
re-trusting the previous night's "everything's stable" verdict — it was
stable (zero crashes, same watchdog/supervisor PIDs all night, scheduler hit
~79% actual vs 80% target for 5s), but throughput was still terrible. Two
real, distinct causes, both fixed:

**1. `_PACE_MAX_REQS=15` was massively too conservative.** Overnight log
analysis for 5s (the priority stage): **0 real pacing violations, 0 fatal
crashes, but 7.45 of its 8.25 allotted hours (90%) spent asleep** on this
self-imposed throttle. It was set to 15 back when 3 fetchers ran
concurrently and each needed a small slice of the shared 60-req/10-min IB
budget — but the scheduler (§0i) now runs exactly ONE at a time, so that
reasoning no longer applies. Raised to **40** (still real headroom under 60
for external IB consumers on this machine). Immediately visible: chunks
that took minutes between pacing sleeps now complete back-to-back.

**2. The tail-chunk "quirk" turned out to be a wall, not a pothole.**
Queried all three progress DBs directly: **every single day recorded so far
sits at exactly `total_chunks - 1` (or -2/-3)** — MES/MNQ/MYM/M2K, 1s/5s/30s,
dates from hours-old to days-old. Not "recent session still settling" as
first theorized (§0i) — sessions that closed *days* ago fail identically.
The logged "expected" window in every failure is verifiably correct (it's
our own code echoing what it asked for), so this isn't a request bug either
— IB itself consistently can't/won't serve the final settlement-adjacent
hour of a CME session via `reqHistoricalData`, for any of these bar sizes,
no matter how many times reconnected. Under the old rule (leave incomplete
forever, retry "on a future pass"), this meant **zero days could ever reach
`finished=1`**, which is why the dashboard showed 0/N finished everywhere
and looked broken even though it wasn't.

**Fix**: `_fetch_day` now treats a tail-chunk (last 2 positions) failure that
has exhausted its crash-restart budget as an **accepted permanent gap**
instead of leaving the day open forever: logs it clearly
(`[ACCEPTED GAP]` in the progress line, `ERROR`-level log entry naming §0k),
advances `chunks_done` past it with 0 bars for that specific chunk, and lets
the day reach `finished=1` once the rest is done. A chunk anywhere *else* in
a day still gets the original "leave incomplete, retry later" treatment —
this is specifically for the one now-confirmed-systemic position.
`_verify_chunks_done`'s self-heal (and `bars1s_repair.py`) were both patched
with a matching exception (`finished=1` + shortfall ≤2 chunks = accepted
gap, not corruption) so they don't fight this and re-open the same gap
forever.

**Verified live**: killed and let the watchdog relaunch the 5s fetcher with
both fixes. `MES 2026-07-23` hit the known chunk-24/24 failure, exhausted
its restart budget (persisted crash count carried over from earlier that
night), logged `accepting as a permanent gap ... continuing`, and the DB
shows `chunks_done=24/24, finished=1, bars_fetched=28,080`. Dashboard
immediately reflected it: 5s went from **0/84 to 1/84 finished**, and will
climb quickly now since most already-attempted days already have an
elevated crash count for this exact position and will resolve on the very
first encounter (no more restarts needed for those).

**What to expect now**: every day's file will legitimately be short by the
last ~1 hour (that session's 16:00–17:00 CT window) unless/until this proves
recoverable from IB some other way — this is now a *known, documented,
visible* gap (present in the log and flagged in the progress line), not a
silent one. If this is unacceptable for downstream use, the real fix would
need to come from IB support / a different data source for that specific
hour — not something fixable from this side by retrying differently.

**Also fixed**: `bars1s_repair.py`'s dry-run flagged several 5s days as
"DUPLICATE/CORRUPT TAIL" with large shortfalls (e.g. 23→1 chunks) — these
are *not* the accepted-gap pattern (that's ≤2 chunks with finished=1); they're
genuine leftover corruption from earlier tonight that the running fetcher's
own self-heal has already been correcting live as it revisits each day (seen
directly in the log: "progress DB said chunks_done=X but only Y chunks
verified clean on disk — truncating and re-fetching"). No separate repair
run was needed or performed while the fetcher was live, to avoid a
file-write race with the running process.

---

## 0j. UPDATE 2026-07-23 ~21:37 UTC — overnight stabilization pass: priority flip, evidence-based tail-chunk fix, verified crash-recovery, benchmark tooling

**Goal for tonight** (explicit ask): run unattended and reliably, self-heal
from any crash, and maximize real data throughput — with **5s bars now the
top priority (~80% of run time)**, 1s and 30s splitting the remaining ~20%.
This flips the priority set earlier the same day (§0i had 1s at 80%).

**1. Priority flip.** `WEIGHTS` in `bars_fetch_watchdog.py` changed from
`{"1s": 0.8, "5s": 0.1, "30s": 0.1}` to `{"5s": 0.8, "1s": 0.1, "30s": 0.1}`.
**Also reset `data/bars_watchdog_schedule.json`'s accumulated time_spent to
zero** — without this, ~7.8 hours of historical 1s run time (from when it
was priority) would have kept registering as "already way over its new 10%
share," starving 5s of turns for a very long time before the deficit
algorithm would naturally correct. Verified after reset: scheduler
immediately picked `5s` as the current stage.

**2. Confirmed the tail-chunk bug is systemic, not a fluke, and tuned around
it.** Overnight data showed the *exact same* failure — the last 1-2 chunks
of the **most recently completed session** — recurring across **all three
bar sizes** (1s, 5s, 30s) and **every symbol**, with cumulative failure
counts in the dozens for some (symbol, date) pairs. This is almost certainly
IB not having fully finalized/settled data for the last hour of the most
recent session yet (available again after some hours/days). Given this
strong evidence, `_MAX_CHUNK_CRASHES` (the number of full process restarts
tolerated before skipping a stuck chunk) was **lowered from 3 to 1, but only
for the last 2 chunks of a day** (`is_tail_chunk = i >= total_chunks - 2` in
`_fetch_day`). A chunk failing this hard anywhere *else* in a day still gets
the more patient budget (3), since that pattern looks like a real outage
rather than the known IB quirk and shouldn't cause an otherwise-good day to
be abandoned too quickly. This cuts the wasted-retry tax on the confirmed
pattern by ~3x without weakening resilience to genuine outages elsewhere.

**3. Verified the full crash-recovery chain end-to-end, deliberately.**
Force-killed the running watchdog process directly (`Stop-Process -Force`)
to test `bars_watchdog_supervisor.py` (added earlier today, §0i): it
detected the exit within ~16s and relaunched the watchdog with a new PID.
The fresh watchdog instance correctly re-derived the current stage (5s,
stateless design) without disturbing the still-running 5s fetcher. This is
the "if it crashes, it bounces back" guarantee, actually tested, not just
assumed.

**4. Robustness pass** — checked and confirmed:
- `bars_status_server.py`'s in-memory history is time-bounded (prunes by
  window, not unbounded), so long unattended runtimes don't leak memory.
- Disk space: 261 GB free — no concern for log/data growth overnight.
- Email alerting (`send_email.py`) is **not actually functional right now**
  — `secrets.ini` doesn't exist in this project's folder (the script's own
  docstring points at `Galgo2026/secrets.ini`, a stale reference). Alerts
  fail silently (30s timeout, `check=False`, never hangs or crashes the
  watchdog) — just don't expect an email if something goes wrong overnight;
  check the logs/report instead. Not fixed (would need real Gmail
  credentials, not something to fabricate).

**5. Built `trader/bars_overnight_report.py`** — the actual "benchmark for
next night" tool requested. Run it any time (intended for morning review):
```powershell
python trader/bars_overnight_report.py
```
Compares current state against a baseline snapshot
(`data/bars_overnight_baseline.json`, captured tonight at ~21:35 UTC) and
reports, per stage: bars/chunks added, actual vs. target time-share, watchdog
restart count, and which specific chunks got stuck/skipped (flagging the
known tail-chunk pattern by position). Ends with concrete tuning suggestions
(adjust `WEIGHTS`, `MIN_QUANTUM_S`, or the tail-chunk threshold) for
subsequent nights based on what actually happened.

**Verified running at handoff**: supervisor (alive) → watchdog (alive,
correctly picked stage=5s, steady `ok` cycles) → 5s fetcher (alive,
progressing cleanly through `MES 2026-07-22`, real bars every chunk, no
errors). Baseline captured for tomorrow's report:
`1s: 6,746,400 bars / 3,748 chunks`, `5s: 272,880 bars / 87 chunks`,
`30s: 5,760 bars / 6 chunks`.

**Known limitation carried forward**: the root cause of the tail-chunk
pattern itself is still not fixed (only worked around) — see §0i for the
open question of whether it's worth a dedicated IB-side investigation.
`broker.py`, `decider.py`, and `back-trading/trading_dashboard.py` remain
running and unaddressed, still a plausible source of pacing contention.

---

## 0i. UPDATE 2026-07-23 ~11:41 UTC — stabilization pass: crash-loop fix, weighted priority scheduler, watchdog-of-the-watchdog

**Why**: a full recheck found the system "looks very unstable" for a very
concrete reason — the fail-fast fix in §0g, while correct (no more silent
corruption), turned a specific unresolvable-by-retry chunk into an
**infinite crash-loop**: the 5s fetcher had been restarting every ~10-12
minutes for hours, always on the exact same chunk (`MES 2026-07-22 chunk
24/24` — the last chunk of that session). Separately, the strict serialized
queue (§0d/§0e: finish 5s, *then* 30s, *then* 1s) meant 1s — the actual
priority dataset — would get **zero run time** until both lower-priority
jobs fully completed, which for 30s (1 year, 4 symbols) could be a very long
wait. Three changes fix both problems:

**1. Crash-loop circuit breaker** (`bars1s_fetcher.py` v1.3). A per-chunk
failure count now persists across process restarts
(`data/bars{N}s_chunk_failures.json`). The fail-fast behavior from §0g is
kept for the first `_MAX_CHUNK_CRASHES` (3) restarts on the *exact same*
chunk — a fresh connection sometimes does succeed where in-process
reconnects didn't. But after 3 restarts still fail on that one chunk, it's
skipped instead of crash-looping forever: the day is left correctly
incomplete (never faked as done), and the fetcher moves on to the next
(symbol, date) pair. A later pass can retry once conditions change; the
count resets on any success for that chunk.

**2. Weighted priority scheduler** (`bars_fetch_watchdog.py` v2.0). Replaced
the strict "finish 5s → 30s → 1s" queue with a deficit-weighted scheduler:
`WEIGHTS = {"1s": 0.8, "5s": 0.1, "30s": 0.1}`. It tracks actual wall-clock
time spent running each stage in `data/bars_watchdog_schedule.json`
(persists across watchdog restarts) and always hands the next turn to
whichever *incomplete* stage is furthest below its target share — so the
80/10/10 ratio holds over the long run regardless of any single turn's
length. A stage only gets reconsidered every `MIN_QUANTUM_S` (15 min), not
every 60s check, so processes aren't thrashed (each restart costs a fresh
IB connection + contract resolution). 1s now gets first and most of the
running time; 5s/30s still make steady (slower) progress in the remaining
~20%, split evenly between them.

**3. Watchdog-of-the-watchdog** (`trader/bars_watchdog_supervisor.py`, new).
`bars_fetch_watchdog.py` keeps the fetchers up; nothing was keeping the
*watchdog* up if it ever crashed. The supervisor is a tiny always-on loop
(with its own PID lock) that relaunches `bars_fetch_watchdog.py` immediately
if it ever exits, for any reason. **This is now the one process to launch
for true 24/7 durability** — point Windows Task Scheduler (run-at-login /
on-schedule, restart-on-failure) at this to survive reboots too:
```powershell
python trader/bars_watchdog_supervisor.py
```
The chain is now: **supervisor → watchdog → fetcher (current priority
stage)**. Each layer only needs to keep the layer directly below it alive;
none of them need to know about the layer above.

**Also fixed while verifying this**: `bars_fetch_watchdog.py`'s
`_EMAIL_SCRIPT` path was wrong (`_ROOT.parent / "send_email.py"`, copied
verbatim from `fetch_watchdog.py` — but `send_email.py` actually lives in
`Fetcher2026/` itself, not its parent). Corrected to `_ROOT / "send_email.py"`.

**Verified**: with no history yet, the scheduler correctly picked 1s first
(highest weight). Confirmed the crash-loop breaker logs `restart 1/3`,
`2/3`, `3/3` then skips rather than crashing a 4th time on the same chunk.

**Known external risk factor, not fixed (not ours to fix)**: `broker.py` and
`decider.py --mode session` (a live-trading system) and
`back-trading/trading_dashboard.py` are all still running and likely still
sharing the same IB account's pacing budget — see §0e/§0g. The stale-window
issue on the last 1-2 chunks of the *most recent* session (§0b, recurring in
§0h and again here on `MES 2026-07-22 chunk 47/48`) looks systemic rather
than random — worth a dedicated root-cause dig if it keeps recurring, but
the circuit breaker above means it can no longer cause a stability problem
even without a root-cause fix.

---

## 0h. UPDATE 2026-07-23 ~10:21 UTC — priority override for CorrelationAnalyzer's Block-A research, then reverted

**Why**: the sibling `CorrellationAnalyzer` project needed one full day of
1-second bars for MES+MNQ to kick off a signal-research plan (see
`CorrellationAnalyzer/analysis/CORRELATION_TRADING_PLAN.md`). That's ahead of
where the serialized 5s→30s→1s queue (§0d/§0e) would naturally deliver it.

**What was done**:
1. Stopped `bars_fetch_watchdog.py` and the in-progress 5s fetcher
   (`--symbols MES,MNQ --days 42`) so a targeted 1s job could run without the
   watchdog reverting it (the watchdog kills any non-current-stage process —
   see §0e).
2. Ran `bars1s_fetcher.py --symbols MES,MNQ --days 1` targeting the most
   recent session (2026-07-22). It got to 46/48 chunks for MES, then hit the
   stale-cache bug from §0b deterministically on chunk 47/48 — same exact
   stale window returned on every attempt, across two full retry cycles (6
   attempts each, with IB reconnects in between) run ~25 minutes apart. This
   looks like a **new manifestation of the §0b root cause specifically on the
   most-recent completed session**, not a one-off transient failure — worth
   watching for whether it recurs on future "yesterday" fetches once they
   reach this stage naturally.
3. Abandoned chasing 2026-07-22 rather than keep retrying — **realized
   2026-07-21 was already fully fetched and clean** from the original run
   (before the stale-cache trouble started), so the actual need (one full
   clean day, 2 symbols) was already met without any new fetch. Directly
   validated: `MES_1s_20260721.csv` and `MNQ_1s_20260721.csv`, 82,800/82,800
   unique timestamps each, zero gaps, full 23h session.
4. Restarted `bars_fetch_watchdog.py` — it self-healed the stale lock from
   the killed 5s process and resumed the 5s stage automatically, no manual
   queue-state fixup needed (this is exactly the self-healing behavior §0e
   was built for).

**Net effect on the serialized plan**: none — 5s stage resumed from where it
was. No 1s-stage progress was gained or lost by this episode; the value was
in confirming 2026-07-21 is usable, not in new data.

**Follow-up worth doing later**: if the 47/48-chunk stale-window failure on
2026-07-22 recurs when the 1s stage naturally reaches recent dates, that's
worth its own root-cause dig — it reproduced identically across two
independent runs and reconnects, which is stronger evidence of a systematic
issue than the original §0b diagnosis had at the time.

---

## 0g. UPDATE 2026-07-23 ~08:56 UTC — connection-loss incident fixed at the root; unified "never silently accept an unverified chunk" rule

**What happened**: the 5s fetcher's IB connection died around 2026-07-22
~20:39–22:17 UTC and never reconnected (no reconnect logic existed). Every
chunk attempt then failed with "Not connected", and after exhausting
retries the old code fell through and marked the chunk/day **done anyway**
with 0 bars — because it only special-cased *pacing-violation* empty
results as "must not accept", not a dead connection. Result: 7 full days
(`MNQ 07-09/08/07/06`, `MES 07-08/07/06`, plus a partial `MES 07-09`) got
silently marked `finished=1` with zero or partial real bars, for ~12 hours,
while the chunk counter kept ticking forward — which is why it looked like
"no progress" rather than an obvious crash.

**While fixing this, found the SAME class of gap was still live for the
2026-07-22 stale-window issue** (§0b): if every one of the 6 retry attempts
for a chunk got rejected by the stale-window validator, the code *also* fell
through and marked that chunk done with 0 bars. This is why a fresh repair
scan today found duplicate-tail corruption on **106 of 108 rows across all
three bar sizes** — including 1s/5s days fetched entirely *after* yesterday's
validation fix, which only prevented bad *data* from being written, not bad
*progress* from being recorded when validation kept failing.

**Root fix in `bars1s_fetcher.py` (v1.2)** — one unified rule replaces the
two separate patches:
- A `got_valid_result` flag is the only way a chunk's progress is ever saved.
  Every rejection path (dead connection, pacing violation, stale/wrong-window
  response) leaves it `False`. If all 6 attempts are exhausted without it
  ever becoming `True`, `_fetch_day` now **raises** instead of falling
  through — the chunk is never marked done on an unverified result, full stop.
- Added real reconnect logic (`_IBSession` class): a dead connection is
  detected via `ib.isConnected()` and via known disconnect-flavored
  exception text (`_is_disconnect_msg`), and the fetcher reconnects
  in-process (fresh `_connect()`, new random clientId) before retrying.
- After 3 consecutive stale-window rejections for the *same* chunk, the
  fetcher also reconnects — repeating the identical request against the same
  connection kept re-hitting the same stale response; a fresh connection is
  a much more reliable way to break that than just waiting longer.
- The raise is deliberately fatal: `bars1s_fetcher.py` crashes, releases its
  lock, and exits — **`bars_fetch_watchdog.py` is what keeps it up now**
  (see §0e), restarting it fresh (new process, new connection) rather than
  the old fetcher trying to loop forever against a bad connection/cache
  state. This is the "watchdog responsible for this" the fix was asked for:
  the fetcher's job is now to fail loudly and immediately on anything
  unverified; staying up is the watchdog's job.
- Added a defense-in-depth check to the watchdog too
  (`_recent_row_is_bogus`): if the most-recently-updated row for the active
  stage is `finished=1` with `bars_fetched=0` (never legitimate for a full
  session on a liquid product), it kills the process to force a clean
  restart — belt-and-suspenders in case some other path ever reproduces the
  pattern, even though the root fix above should prevent it going forward.

**`bars1s_repair.py` v2** — generalized to scan *all* `bars{N}s_progress.db`
files (not just 1s) and fix both corruption signatures by recomputing
`chunks_done`/`bars_fetched`/`finished` directly from what's verified on
disk (`_verify_chunks_done`, shared with `_fetch_day`'s own self-heal —
extracted into `_build_chunks`/`_verify_chunks_done` so both use identical
logic). Ran across all 3 DBs: **106 of 108 rows corrected**. A bug in this
repair pass's own timestamp write (`datetime('now')`, naive/no-timezone —
didn't match `_save()`'s `datetime.now(UTC).isoformat()` format) briefly
broke `bars_status_server.py`'s health/ETA arithmetic; fixed in the script
and patched the ~106 already-written rows directly.

**Verified working**: after these fixes, the 5s fetcher (freshly restarted
by the watchdog) reconnected cleanly and is fetching real, uninterrupted
data again — no more "Not connected" or unresolved stale-window loops.

## 0e. UPDATE 2026-07-22 ~14:35 UTC — watchdog added, orchestrates the serialized queue

`trader/bars_fetch_watchdog.py` now runs the §0d serialized plan (5s → 30s →
1s) automatically — no more manually starting/stopping stages by hand.

**What it does every 60s:**
1. Figures out the "current stage" by checking each stage's progress DB —
   the first one in the queue that isn't yet complete. Stateless: no
   separate "which stage" file, so it self-heals from any inconsistency.
2. Kills any fetcher process that isn't the current stage — enforces
   "only one bar-size fetcher runs at a time" (the whole reason §0d exists;
   see below for how this almost failed).
3. If nothing is running for the current stage, (re)starts it —
   `bars1s_fetcher.py` resumes from its own progress DB, so this is always
   safe, whether recovering from a crash or advancing to a new stage.
4. Cleans stale lock files, alerts by email on repeated restart failure or a
   long stall (reuses `send_email.py`, same as `fetch_watchdog.py`).

Run it:
```powershell
python trader/bars_fetch_watchdog.py            # runs forever
python trader/bars_fetch_watchdog.py --once      # single check, for testing
```

**Bug caught and fixed while building this**: the first version's process
detection matched cmdlines against the project's *absolute* path — but the
fetchers are launched with a *relative* script path
(`trader/bars1s_fetcher.py` from cwd=repo root), so the absolute path never
appears in the command line and detection silently found nothing. On its
first `--once` run the watchdog concluded no 5s fetcher was running (one
already was) and launched a second one. **The per-bar-size PID lock added in
§0b caught it** — the new process hit the lock, refused to start, and exited
immediately; the original process was never disturbed. Fixed detection to
match on `proc.cwd()` instead, and to filter out non-Python wrapper
processes (git-bash's `nohup.exe` parent, which shares the same cmdline
substring and cwd as the real child it launches, and would otherwise look
like a second real instance). Verified clean after the fix — exactly one
process detected, no spurious kills. This is a good example of why the lock
file exists: it's the last line of defense when higher-level orchestration
has a bug.

**Also found while investigating**: two more unrelated live processes are
running on this machine — `broker.py` and `decider.py --mode session` —
which look like an active live-trading system, likely also connected to the
same IB Gateway. Combined with `back-trading/trading_dashboard.py` (§0b),
that's now 3 known external consumers of the same shared IB pacing budget,
on top of whichever of our own fetchers is active. None of these were
touched — not ours to manage — but it's useful context for why real pacing
pressure has been so persistent all session.

---

## 0d. UPDATE 2026-07-22 ~10:00 UTC — running all 3 concurrently didn't work; switched to serialized
## 0d. UPDATE 2026-07-22 ~10:00 UTC — running all 3 concurrently didn't work; switched to serialized

Within ~20 min of running 1s + 5s + 30s at once (§0c), the dashboard's ETA for
the 1s fetcher had climbed to **~160 days and rising**. Diagnosis: in the last
60 log lines for the 1s fetcher, **21 "stale window" + 22 "pacing violation"
retries vs only 6 successful chunk writes** — an ~88% failure rate per
attempt, real observed throughput collapsed to 0.19 chunks/min (vs. the
~5.5/min a single fetcher got originally). Root cause: 3 of our own processes
(each self-capped at 15 req/10min = up to 45/10min combined) plus the
still-running external `back-trading/trading_dashboard.py`, all sharing IB's
one real 60-req/10-min account-wide budget — chronically over budget, and a
vicious cycle (overload → IB returns stale data → we correctly reject it →
retry → that retry burns another real request slot → worse overload).

**Decision: serialize instead of running concurrently.** Give each fetcher
the full pacing budget in turn:

1. **5s (MES+MNQ, 2 months) — RUNNING NOW.** Smallest job (2,016 target
   chunks). Immediately went clean after pausing the other two — zero stale/
   pacing errors since, only its own self-throttle sleeps. As of ~10:00 UTC:
   1.7% done, recovering rate ~1.0 chunks/min and improving as the rolling
   ETA window clears out the old contended samples.
2. **30s (4 symbols, 1 year) — PAUSED, resume next.** Was at 0.4% when
   paused. PID: was Windows PID 22872 (now stopped) — relaunch with:
   ```powershell
   python trader/bars1s_fetcher.py --bar-secs 30 --days 252
   ```
3. **1s (4 symbols, 1 year) — PAUSED, resume last.** Was at 7.8% when paused
   (the most mature/valuable dataset, but also the biggest job — 48,384
   target chunks — so it goes last in the queue rather than first, to let the
   smaller jobs clear out of the way). PID: was Windows PID 19104 (now
   stopped) — relaunch with:
   ```powershell
   python trader/bars1s_fetcher.py
   ```

**UPDATE — this is now automated, see §0e.** `bars_fetch_watchdog.py` runs
the serialization above on its own: don't manually stop/start stages
anymore, just make sure the watchdog is running. The manual PID/relaunch
notes above are kept for reference (e.g. if you need to run a stage
one-off outside the watchdog) but the watchdog is the normal path now.

The status server (`bars_status_server.py`, port 5004) and the external
`back-trading/trading_dashboard.py` were left running — neither connects
with enough IB request volume on its own to matter here (the status server
doesn't touch IB at all, it only reads the local progress DBs).

---

## 0c. UPDATE 2026-07-22 ~09:40 UTC — generalized for multiple bar sizes, running 3 in parallel, new live dashboard

`bars1s_fetcher.py` (despite the name — kept for backward compat, see below)
is now a **generic OHLCV bar fetcher** taking `--bar-secs`. Each bar size gets
its own output dir / progress DB / lock file (`data/bars{N}s/`,
`data/bars{N}s_progress.db`, `data/bars{N}s_fetcher.lock`), so multiple bar
sizes run concurrently without ever colliding on files. Bare
`python trader/bars1s_fetcher.py` with no flags is **unchanged** — still 1s,
all 4 symbols, 252 days, same paths as before — so the already-running
process and existing data needed no migration.

**Currently running, all three in parallel:**

| Bar size | Symbols | Days back | Chunk duration (IB max) | Chunks/day | Output |
|---|---|---|---|---|---|
| 1 sec  | MES, MNQ, MYM, M2K | 252 (~1yr) | 1800 S (30 min) | 48 | `data/bars1s/` |
| 5 sec  | MES, MNQ           | 42 (~2mo)  | 3600 S (1 hr)   | 24 | `data/bars5s/` |
| 30 sec | MES, MNQ, MYM, M2K | 252 (~1yr) | 28800 S (8 hr)  | 3  | `data/bars30s/` |

IB duration-per-bar-size limits confirmed via IB's own docs
([tws-api historical_limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html)).
**Caveat for the 30-sec/1-year fetch**: IB does not retain bars of 30 seconds
or finer older than ~6 months — expect the older half of that year (roughly
dates before ~2026-01) to come back empty for the 30s fetcher. That's
expected behavior, not a bug; the existing empty-vs-pacing-violation
validation (§0b) already distinguishes a real "no data available" empty
result from a pacing-violation-caused empty result.

**Pacing tuned down for concurrency**: `_PACE_MAX_REQS` lowered from 55 to
**15 per process** (in `bars1s_fetcher.py`'s new default) — since IB's real
60-req/10-min historical-data pacing limit is shared account-wide, not
per-client, and we now expect 3 of our own processes plus the external
`back-trading/trading_dashboard.py` (see §0b) all drawing from the same
budget. The already-running 1s process is still using the old in-memory
55 value until its next restart; the new 5s/30s processes use 15 from the
start.

**Live dashboard moved to a real server**: `trader/bars_status_server.py`
replaces the old `bars1s_viewer.py --watch` + static-HTML approach. Run it:
```powershell
python trader/bars_status_server.py            # http://localhost:5004/
```
It shows all three fetchers' progress (bars/month, per-symbol completion,
health badge, ETA) and auto-refreshes every 10s. The old
`data/bars1s_viewer.html` now just redirects here.

**Why the ETA used to look "stuck" and how this fixes it**: the old viewer's
ETA fell back to a fixed *theoretical* pacing rate whenever a before/after
snapshot between two separate CLI invocations happened to show no progress
— increasingly common now that real IB pacing pressure means a single day
can spend several minutes retrying. `bars_status_server.py` instead runs as
a persistent background sampler, recording actual DB progress every 10s into
a rolling ~20-minute history, and computes ETA from that *observed*
throughput. If there isn't yet enough history to trust, it says
"warming up…" or "no recent progress" instead of showing a misleading
number — never a frozen guess.

`trader/bars1s_viewer.py` (the old CLI script) still works standalone for a
quick one-shot console printout of the 1s fetcher only; it's no longer the
primary way to check status.

---

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
