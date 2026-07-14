#!/usr/bin/env python3
"""dashboard.py — Fetcher2026  python dashboard.py --real"""

import csv, socket, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

import psutil
from flask import Flask, jsonify
from lib.config_loader import get_config

_cfg        = get_config()
HISTORY_DIR = Path(_cfg.paths.history)
GALAO_DB    = Path(_cfg.paths.db)
PROGRESS_DB = GALAO_DB.parent / "fetch_progress.db"

SYMBOLS  = ["MES", "MNQ", "MYM", "M2K"]
DTYPES   = ["TRADES", "BID_ASK"]
CT       = ZoneInfo("America/Chicago")
F2_ROOT  = str(_ROOT).replace("\\", "/").lower()
VERSION  = "v2.3"

app = Flask(__name__)

_rate_cache: dict = {}   # (sym, date, dtype) → (utc_ts, count)

# ── helpers ───────────────────────────────────────────────────────────────────

def _last_working_day():
    d = (datetime.now(CT) - timedelta(days=1)).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def _working_days_back(n):
    days, d = [], (datetime.now(CT) - timedelta(days=1)).date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days

def _csv_exists(sym, dtype, d):
    dt = "trades" if dtype == "TRADES" else "bidask"
    p  = HISTORY_DIR / f"{sym}_{dt}_{d.strftime('%Y%m%d')}.csv"
    return p.exists() and p.stat().st_size > 500

def _gw_up():
    try:
        s = socket.create_connection(("127.0.0.1", 4002), timeout=1.5)
        s.close(); return True
    except OSError:
        return False

def _fetcher_pid():
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if "fetch_scheduler" in cmd and F2_ROOT in cmd.replace("\\", "/").lower():
                return p.info["pid"]
        except Exception:
            pass
    return None

def _load_progress():
    out = {}
    try:
        conn = sqlite3.connect(str(PROGRESS_DB), timeout=3)
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT * FROM fetch_progress").fetchall():
            out[(r["symbol"], r["date"], r["data_type"])] = dict(r)
        conn.close()
    except Exception:
        pass
    return out

def _vt_counts():
    try:
        conn = sqlite3.connect(str(GALAO_DB), timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DATE(fill_time) as d, COUNT(*) as n FROM verified_trades GROUP BY d"
        ).fetchall()
        conn.close()
        return {r["d"]: r["n"] for r in rows}
    except Exception:
        return {}

def _throughput():
    now      = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    r = {"midnight": 0, "files_done": 0, "files_started": 0,
         "avg_rate": 0, "last_ts": None, "data_age": 9999,
         "active": None, "avg_per_min": 0}
    try:
        conn = sqlite3.connect(str(PROGRESS_DB), timeout=3)
        # ticks today — all rows touched since midnight (includes active job count)
        row = conn.execute(
            "SELECT SUM(records_fetched) FROM fetch_progress WHERE updated_at >= ?",
            (midnight.isoformat(),)
        ).fetchone()
        r["midnight"] = int(row[0] or 0)
        # files done today (finished=1, updated since midnight)
        row = conn.execute(
            "SELECT COUNT(*) FROM fetch_progress WHERE finished=1 AND updated_at >= ?",
            (midnight.isoformat(),)
        ).fetchone()
        r["files_done"] = int(row[0] or 0)
        # files started today (any row touched since midnight)
        row = conn.execute(
            "SELECT COUNT(*) FROM fetch_progress WHERE updated_at >= ?",
            (midnight.isoformat(),)
        ).fetchone()
        r["files_started"] = int(row[0] or 0)
        # most recent update — drives the LED
        ts_row = conn.execute("SELECT MAX(updated_at) FROM fetch_progress").fetchone()
        if ts_row and ts_row[0]:
            r["last_ts"] = ts_row[0]
            ts = datetime.fromisoformat(ts_row[0].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            r["data_age"] = max(0, (now - ts).total_seconds())
        # active job (most recently updated unfinished row)
        active = conn.execute(
            "SELECT symbol, date, data_type, records_fetched FROM fetch_progress "
            "WHERE finished=0 AND records_fetched>0 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if active:
            sym, dt_, dtype, cnt = active[0], active[1], active[2], int(active[3] or 0)
            r["active"] = {"sym": sym, "date": dt_, "dtype": dtype, "count": cnt}
        conn.close()
    except Exception:
        pass
    # avg ticks/min since midnight — smooth, never volatile
    elapsed_min = max(1, (now - midnight).total_seconds() / 60)
    r["avg_rate"]    = int(r["midnight"] / elapsed_min)
    r["avg_per_min"] = r["avg_rate"]
    return r

def _last_price(sym):
    files = sorted(HISTORY_DIR.glob(f"{sym}_trades_*.csv"), reverse=True)
    for f in files[:3]:
        try:
            with open(f, newline="") as fh:
                rows = list(csv.reader(fh))
                if len(rows) < 2:
                    continue
                header = [h.lower() for h in rows[0]]
                pcol = next((i for i, h in enumerate(header) if "price" in h), None)
                if pcol is not None:
                    return float(rows[-1][pcol])
        except Exception:
            pass
    return None

def _build_queue(progress, vt):
    seen, queue = set(), []
    lwd = _last_working_day()

    def _done(sym, d, dtype):
        row = progress.get((sym, d.isoformat(), dtype))
        return _csv_exists(sym, dtype, d) or bool(row and row.get("finished"))

    def _status(sym, d, dtype):
        if _done(sym, d, dtype):
            return "done"
        row = progress.get((sym, d.isoformat(), dtype))
        if row and not row.get("finished") and row.get("records_fetched", 0) > 0:
            try:
                ts = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - ts).total_seconds() < 90:
                    return "active"
            except Exception:
                pass
            return "resume"
        return "pending"

    def _add(sym, d, dtype, tier, reason):
        key = (sym, d.isoformat(), dtype)
        if key in seen:
            return
        seen.add(key)
        st = _status(sym, d, dtype)
        queue.append({
            "tier": tier,
            "date": d.isoformat(),
            "date_label": f"{d.month}/{d.day}",
            "sym": sym,
            "dtype": "TR" if dtype == "TRADES" else "BA",
            "reason": reason,
            "status": st,
        })

    # P0
    for sym in SYMBOLS:
        for dtype in DTYPES:
            _add(sym, lwd, dtype, "P0", "last day")
    # P1
    for d_str, n in sorted(vt.items(), key=lambda x: -x[1]):
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            continue
        if d == lwd:
            continue
        for sym in SYMBOLS:
            for dtype in DTYPES:
                _add(sym, d, dtype, "P1", f"{n}vt")
    # P2
    for (sym, d_str, dtype), row in progress.items():
        if not row.get("finished") and row.get("records_fetched", 0) > 0:
            try:
                _add(sym, date.fromisoformat(d_str), dtype, "P2", "resume")
            except Exception:
                pass
    # P3
    vt_dates = set(vt.keys())
    for d in _working_days_back(90):
        if d == lwd or d.isoformat() in vt_dates:
            continue
        for sym in SYMBOLS:
            for dtype in DTYPES:
                _add(sym, d, dtype, "P3", "backfill")
    return queue

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    thr = _throughput()
    now = time.time()
    return jsonify({
        "gateway":     _gw_up(),
        "fetcher_pid": _fetcher_pid(),
        "throughput":  thr,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/api/queue")
def api_queue():
    return jsonify(_build_queue(_load_progress(), _vt_counts()))

@app.route("/api/files")
def api_files():
    progress = _load_progress()
    vt       = _vt_counts()
    lwd      = _last_working_day()
    all_dates = set()
    for (_, d_str, _) in progress:
        all_dates.add(d_str)
    for f in HISTORY_DIR.glob("*_trades_????????.csv"):
        parts = f.stem.split("_")
        if len(parts) >= 3:
            dc = parts[-1]
            if len(dc) == 8:
                all_dates.add(f"{dc[:4]}-{dc[4:6]}-{dc[6:]}")
    for d_str in vt:
        all_dates.add(d_str)

    rows = []
    for d_str in sorted(all_dates, reverse=True):
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            continue
        cells = {}
        for sym in SYMBOLS:
            for dtype in DTYPES:
                row = progress.get((sym, d_str, dtype))
                ex  = _csv_exists(sym, dtype, d)
                if ex or (row and row.get("finished")):
                    cnt = int(row["records_fetched"]) if row and row.get("records_fetched") else 0
                    vs  = row.get("verify_status") if row else None
                    cells[f"{sym}_{dtype}"] = {"s": "done", "c": cnt, "v": vs}
                elif row and not row.get("finished") and row.get("records_fetched", 0) > 0:
                    cells[f"{sym}_{dtype}"] = {"s": "active", "c": int(row["records_fetched"])}
                else:
                    cells[f"{sym}_{dtype}"] = {"s": "miss", "c": 0}

        tags = []
        if d == lwd:       tags.append(("P0", "LAST DAY"))
        if d_str in vt:    tags.append(("P1", f"VT {vt[d_str]}"))
        rows.append({"date": d_str, "cells": cells, "tags": tags})
    return jsonify(rows)

@app.route("/api/prices")
def api_prices():
    return jsonify({s: _last_price(s) for s in SYMBOLS})

# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fetcher2026</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;font-family:'Segoe UI',system-ui,sans-serif;
  background:#0d1117;color:#c9d1d9;font-size:13px}

/* ── two-column layout ── */
.layout{display:flex;height:100vh;overflow:hidden}

/* ── LEFT PANEL ── */
.left{width:270px;min-width:220px;flex-shrink:0;border-right:1px solid #21262d;
  display:flex;flex-direction:column;overflow:hidden;background:#080d13}

.brand{padding:14px 14px 8px;border-bottom:1px solid #21262d}
.brand-name{font-size:1.5rem;font-weight:900;color:#e6edf3;letter-spacing:.06em;line-height:1.1}
.brand-ver{font-size:1.1rem;font-weight:700;color:#3fb950;margin-top:2px;letter-spacing:.05em}
.brand-sub{font-size:.6rem;color:#4d5566;margin-top:3px;letter-spacing:.1em;text-transform:uppercase}

.queue-title{padding:6px 14px 4px;font-size:.6rem;font-weight:700;color:#4d5566;
  letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid #21262d}
.queue-scroll{flex:1;overflow-y:auto}
.queue-scroll::-webkit-scrollbar{width:3px}
.queue-scroll::-webkit-scrollbar-thumb{background:#21262d;border-radius:2px}

.qtab{width:100%;border-collapse:collapse}
.qtab td{padding:3px 6px 3px 14px;border-bottom:1px solid #0d111755;vertical-align:middle;
  white-space:nowrap;font-size:.68rem}
.qtab tr:hover td{background:#ffffff05}
.tr-done td{opacity:.28}
.tier{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.59rem;
  font-weight:800;min-width:22px;text-align:center}
.t0{background:#c2410c44;color:#f0883e;border:1px solid #c2410c66}
.t1{background:#92400e33;color:#d29922;border:1px solid #92400e55}
.t2{background:#1e3a5f44;color:#60a5fa;border:1px solid #1e40af66}
.t3{background:#21262d;color:#4d5566;border:1px solid #30363d}
.q-date{color:#8b949e;font-family:monospace;font-size:.68rem}
.q-sym{font-weight:700;color:#c9d1d9;font-size:.7rem;width:32px;display:inline-block}
.q-dt{font-size:.6rem;color:#6e7681;width:18px;display:inline-block}
.q-rsn{font-size:.64rem;margin-left:2px}
.rsn-p0{color:#f0883e}.rsn-p1{color:#d29922}.rsn-p2{color:#60a5fa}.rsn-p3{color:#4d5566}
.sts-a{color:#f0883e;font-weight:700;font-size:.6rem;animation:pl 1.1s infinite}
.sts-r{color:#60a5fa;font-size:.6rem}.sts-p{color:#30363d;font-size:.6rem}.sts-d{color:#23863633;font-size:.6rem}
@keyframes pl{0%,100%{opacity:1}50%{opacity:.3}}

/* ── RIGHT PANEL ── */
.right{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}

/* health bar */
.hbar{display:flex;align-items:center;gap:10px;padding:6px 14px;
  border-bottom:1px solid #21262d;flex-wrap:wrap;flex-shrink:0}
.hpill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;
  font-size:.67rem;font-weight:600}
.hp-g{background:#14532d22;color:#3fb950;border:1px solid #23863633}
.hp-r{background:#3d100022;color:#f85149;border:1px solid #7d1c1c44}
.hp-y{background:#3d2f0022;color:#d29922;border:1px solid #7d5c1044}
.hdot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.hbar-time{margin-left:auto;font-size:.62rem;color:#4d5566;font-family:monospace}

/* LED + throughput row */
.led-row{display:flex;align-items:center;gap:14px;padding:10px 14px;
  border-bottom:1px solid #21262d;flex-shrink:0;flex-wrap:wrap}

.led{width:58px;height:58px;border-radius:50%;flex-shrink:0;transition:all .4s}
.led-g{background:radial-gradient(circle at 35% 35%,#5bea6e,#22863a);
  box-shadow:0 0 22px #3fb95099,0 0 44px #3fb95044;animation:lglow 1.4s ease-in-out infinite}
.led-y{background:radial-gradient(circle at 35% 35%,#fbbf24,#b45309);
  box-shadow:0 0 18px #d2992255}
.led-o{background:radial-gradient(circle at 35% 35%,#fb923c,#c2410c);
  box-shadow:0 0 18px #f0883e44}
.led-r{background:radial-gradient(circle at 35% 35%,#f87171,#991b1b);
  box-shadow:0 0 18px #f8514944}
@keyframes lglow{0%,100%{box-shadow:0 0 22px #3fb95099,0 0 44px #3fb95044}
  50%{box-shadow:0 0 36px #3fb950cc,0 0 64px #3fb95077}}

.thr-block{display:flex;flex-direction:column;gap:5px;min-width:160px}
.thr-row{display:flex;align-items:center;gap:7px}
.thr-lbl{font-size:.59rem;color:#4d5566;width:44px;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0}
.thr-bar{height:8px;border-radius:4px;background:#161b22;flex:1;overflow:hidden;position:relative;min-width:80px}
.thr-fill{height:100%;border-radius:4px;transition:width .8s ease}
.fill-g{background:linear-gradient(90deg,#14532d,#238636)}
.fill-y{background:linear-gradient(90deg,#3d2f00,#d29922)}
.fill-r{background:linear-gradient(90deg,#3d1010,#f85149)}
.thr-val{font-size:.68rem;font-weight:700;font-family:monospace;width:56px;text-align:right;flex-shrink:0}
.val-g{color:#3fb950}.val-y{color:#d29922}.val-r{color:#f85149}

.active-block{flex:1;min-width:180px}
.active-hdr{font-size:.59rem;color:#4d5566;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.active-sym{font-size:.95rem;font-weight:700;color:#e6edf3}
.active-detail{font-size:.64rem;color:#6e7681;margin-bottom:4px}
.pbar{height:7px;border-radius:4px;background:#161b22;overflow:hidden;border:1px solid #21262d;margin-bottom:3px}
.pbar-fill{height:100%;background:linear-gradient(90deg,#c2410c,#f0883e);border-radius:4px;transition:width .8s}
.active-meta{font-size:.62rem;color:#6e7681;font-family:monospace;display:flex;justify-content:space-between}
.no-active{font-size:.72rem;color:#30363d;font-style:italic}

.last-ts-block{display:flex;flex-direction:column;gap:3px;flex-shrink:0}
.last-ts-lbl{font-size:.59rem;color:#4d5566;text-transform:uppercase;letter-spacing:.06em}
.last-ts-val{font-size:.72rem;color:#8b949e;font-family:monospace}
.age-val{font-size:.65rem;font-family:monospace}
.age-g{color:#3fb950}.age-y{color:#d29922}.age-r{color:#f85149}

/* prices */
.prices{display:flex;gap:18px;padding:7px 14px;border-bottom:1px solid #21262d;
  flex-shrink:0;flex-wrap:wrap;align-items:baseline}
.sp{display:flex;flex-direction:column;gap:1px}
.sp-name{font-size:.58rem;color:#6e7681;font-weight:700;letter-spacing:.08em}
.sp-val{font-size:.88rem;font-weight:700;color:#e6edf3;font-family:monospace}

/* files grid */
.grid-outer{flex:1;overflow:hidden;display:flex;flex-direction:column;padding:0 0 0 0}
.grid-title{padding:5px 14px 3px;font-size:.6rem;font-weight:700;color:#4d5566;
  letter-spacing:.1em;text-transform:uppercase;flex-shrink:0}
.grid-scroll{flex:1;overflow:auto}
.grid-scroll::-webkit-scrollbar{width:4px;height:4px}
.grid-scroll::-webkit-scrollbar-thumb{background:#21262d;border-radius:2px}
.gtab{border-collapse:collapse;font-size:.65rem;white-space:nowrap;min-width:100%}
.gtab th{padding:4px 4px;text-align:center;font-weight:600;color:#6e7681;
  border-bottom:2px solid #21262d;position:sticky;top:0;background:#0d1117;z-index:2}
.gtab th.dh{text-align:left;min-width:78px;padding-left:14px;color:#8b949e}
.gtab th.sh{border-left:1px solid #21262d;color:#c9d1d9;font-size:.68rem}
.gtab td{padding:2px 3px;border-bottom:1px solid #0d1117;text-align:center;vertical-align:middle}
.gtab td.dc{font-family:monospace;color:#6e7681;font-size:.64rem;
  text-align:left;padding-left:14px;position:sticky;left:0;background:#0d1117;z-index:1}
.gtab tr:hover td{background:#ffffff04}
.gtab tr:hover td.dc{background:#0d1117}
.cd{background:#14532d55;color:#3fb950;border-radius:2px;padding:1px 5px;font-size:.6rem;font-weight:700}
.ca{background:#c2410c22;color:#f0883e;border-radius:2px;padding:1px 5px;font-size:.6rem;font-weight:700;animation:pl 1.1s infinite}
.cm{color:#30363d;font-size:.6rem}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.57rem;font-weight:700;margin-right:2px}
.tp0{background:#c2410c33;color:#f0883e}.tp1{background:#92400e33;color:#d29922}
</style>
</head>
<body>
<div class="layout">

<!-- ══ LEFT: brand + queue ══ -->
<div class="left">
  <div class="brand">
    <div class="brand-name">FETCHER<br>2026</div>
    <div class="brand-ver" id="ver-lbl">""" + VERSION + r"""</div>
    <div class="brand-sub">Tick Data Service</div>
  </div>
  <div class="queue-title">Priority Queue</div>
  <div class="queue-scroll">
    <table class="qtab"><tbody id="qbody"></tbody></table>
  </div>
</div>

<!-- ══ RIGHT ══ -->
<div class="right">

  <!-- health bar -->
  <div class="hbar">
    <span id="hp-gw" class="hpill hp-r"><span class="hdot"></span>Gateway</span>
    <span id="hp-ft" class="hpill hp-r"><span class="hdot"></span>Fetcher</span>
    <span class="hpill hp-g"><span class="hdot"></span>Dashboard</span>
    <span id="clean-lbl" style="font-size:.62rem;color:#4d5566;margin-left:4px"></span>
    <span class="hbar-time" id="hbar-time"></span>
  </div>

  <!-- LED + throughput + active -->
  <div class="led-row">
    <div class="led led-r" id="big-led" title="Green=fetching now, Red=stalled"></div>

    <div class="thr-block">
      <div class="thr-row">
        <span class="thr-lbl">files today</span>
        <div class="thr-bar"><div class="thr-fill fill-g" id="bar-files" style="width:0%"></div></div>
        <span class="thr-val val-g" id="val-files">—</span>
      </div>
      <div class="thr-row">
        <span class="thr-lbl">ticks today</span>
        <div class="thr-bar"><div class="thr-fill fill-g" id="bar-mid" style="width:0%"></div></div>
        <span class="thr-val val-g" id="val-mid">—</span>
      </div>
      <div class="thr-row">
        <span class="thr-lbl">avg/min</span>
        <div class="thr-bar"><div class="thr-fill fill-g" id="bar-avg" style="width:0%"></div></div>
        <span class="thr-val val-g" id="val-avg">—</span>
      </div>
    </div>

    <div class="active-block">
      <div class="active-hdr">Active Fetch</div>
      <div id="active-area"><span class="no-active">—</span></div>
    </div>

    <div class="last-ts-block">
      <span class="last-ts-lbl">Last data</span>
      <span class="last-ts-val" id="last-ts">—</span>
      <span class="age-val age-r" id="age-lbl">—</span>
    </div>
  </div>

  <!-- prices -->
  <div class="prices">
    <div class="sp"><span class="sp-name">MES</span><span class="sp-val" id="p-MES">—</span></div>
    <div class="sp"><span class="sp-name">MNQ</span><span class="sp-val" id="p-MNQ">—</span></div>
    <div class="sp"><span class="sp-name">MYM</span><span class="sp-val" id="p-MYM">—</span></div>
    <div class="sp"><span class="sp-name">M2K</span><span class="sp-val" id="p-M2K">—</span></div>
  </div>

  <!-- files grid -->
  <div class="grid-outer">
    <div class="grid-title">Fetched Files</div>
    <div class="grid-scroll">
      <table class="gtab">
        <thead id="ghead"></thead>
        <tbody id="gbody"></tbody>
      </table>
    </div>
  </div>

</div><!-- /right -->
</div><!-- /layout -->

<script>
const SYMS=['MES','MNQ','MYM','M2K'], DTS=['TRADES','BID_ASK'];
let _avgMin=0;

function fmtK(n){
  if(n==null)return'—';
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'K';
  return String(Math.round(n));
}
function fmtTs(ts){
  if(!ts)return'—';
  try{return new Date(ts).toLocaleTimeString('en-US',
    {timeZone:'America/Chicago',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' CT';}
  catch{return ts.slice(11,19)+' UTC';}
}
function ledClass(age){
  if(age<6)  return'led-g';
  if(age<60) return'led-y';
  if(age<300)return'led-o';
  return'led-r';
}
function barColor(val,avg){
  if(val===0)return'fill-r';
  if(avg>0&&val<avg*0.6)return'fill-y';
  return'fill-g';
}
function valColor(val,avg){
  if(val===0)return'val-r';
  if(avg>0&&val<avg*0.6)return'val-y';
  return'val-g';
}

async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    // health
    const gw=d.gateway, ft=!!d.fetcher_pid;
    document.getElementById('hp-gw').className='hpill '+(gw?'hp-g':'hp-r');
    document.getElementById('hp-ft').className='hpill '+(ft?'hp-g':'hp-r');
    document.getElementById('hbar-time').textContent=new Date().toLocaleTimeString();

    const t=d.throughput||{};
    const age=t.data_age??9999;
    _avgMin=t.avg_per_min||0;

    // big LED
    const led=document.getElementById('big-led');
    led.className='led '+ledClass(age);

    // bar 1: files done today  (e.g. "3 / 5")
    const done=t.files_done||0, started=t.files_started||0;
    const filePct=started>0?Math.min(100,Math.round(done/started*100)):0;
    const fileCol=done===0?'fill-r':done>=started?'fill-g':'fill-y';
    const fileVCol=done===0?'val-r':done>=started?'val-g':'val-y';
    document.getElementById('bar-files').className='thr-fill '+fileCol;
    document.getElementById('bar-files').style.width=filePct+'%';
    const fv=document.getElementById('val-files');
    fv.className='thr-val '+fileVCol;
    fv.textContent=done+' / '+started;

    // bar 2: ticks today — always grows, always green
    const mid=t.midnight||0;
    document.getElementById('bar-mid').className='thr-fill fill-g';
    document.getElementById('bar-mid').style.width='100%';
    const mv=document.getElementById('val-mid');
    mv.className='thr-val val-g';
    mv.textContent=fmtK(mid);

    // bar 3: avg ticks/min since midnight — smooth, never volatile
    const avgRate=t.avg_rate||0;
    _avgMin=avgRate;
    const avgPct=avgRate>0?Math.min(100,Math.round(avgRate/Math.max(avgRate,1)*100)):0;
    const avgCol=avgRate===0?'fill-r':avgRate>10000?'fill-g':'fill-y';
    const avgVCol=avgRate===0?'val-r':avgRate>10000?'val-g':'val-y';
    document.getElementById('bar-avg').className='thr-fill '+avgCol;
    document.getElementById('bar-avg').style.width=avgRate>0?'100%':'0%';
    const av=document.getElementById('val-avg');
    av.className='thr-val '+avgVCol;
    av.textContent=avgRate>0?fmtK(avgRate)+'/min':'—';

    // last data / age
    document.getElementById('last-ts').textContent=fmtTs(t.last_ts);
    const ageEl=document.getElementById('age-lbl');
    if(age<9999){
      const as=Math.round(age);
      ageEl.textContent=as<60?as+'s ago':Math.floor(as/60)+'m ago';
      ageEl.className='age-val '+(age<10?'age-g':age<120?'age-y':'age-r');
    } else {
      ageEl.textContent='no data'; ageEl.className='age-val age-r';
    }

    // active fetch
    const aa=document.getElementById('active-area');
    if(t.active){
      const a=t.active;
      const expected=a.dtype==='BID_ASK'?2000000:10000;
      const pct=a.count>0?Math.min(100,Math.round(a.count/expected*100)):0;
      aa.innerHTML=`<div class="active-sym">${a.sym} <span style="color:#6e7681;font-size:.72rem">${a.date}</span> <span style="color:#4d5566;font-size:.66rem">${a.dtype}</span></div>
<div class="pbar"><div class="pbar-fill" style="width:${pct}%"></div></div>
<div class="active-meta"><span>${fmtK(a.count)} ticks</span><span style="color:#4d5566">${pct>=100?'~done':pct+'%'}</span></div>`;
    } else {
      aa.innerHTML='<span class="no-active">—</span>';
    }
  }catch(e){console.error(e)}
}

// ── queue ──
async function pollQueue(){
  try{
    const items=await(await fetch('/api/queue')).json();
    const TCLS=['t0','t1','t2','t3'];
    const RCLS=['rsn-p0','rsn-p1','rsn-p2','rsn-p3'];
    const tbody=document.getElementById('qbody');
    tbody.innerHTML=items.map(r=>{
      const ti=parseInt(r.tier[1])||0;
      const sc=r.status==='active'?'sts-a':r.status==='resume'?'sts-r':
               r.status==='done'?'sts-d':'sts-p';
      const si=r.status==='active'?'▶':r.status==='done'?'✓':
               r.status==='resume'?'↺':'·';
      return`<tr class="${r.status==='done'?'tr-done':''}">
        <td><span class="tier ${TCLS[ti]}">${r.tier}</span></td>
        <td class="q-date">${r.date_label}</td>
        <td><span class="q-sym">${r.sym}</span><span class="q-dt">${r.dtype}</span></td>
        <td class="q-rsn ${RCLS[ti]}">${r.reason}</td>
        <td><span class="${sc}">${si}</span></td>
      </tr>`;
    }).join('');
  }catch(e){console.error(e)}
}

// ── grid ──
function buildHead(){
  let h='<tr><th class="dh">Date</th>';
  SYMS.forEach(s=>h+=`<th class="sh" colspan="2">${s}</th>`);
  h+='<th style="min-width:70px">Tags</th></tr><tr><th></th>';
  SYMS.forEach(()=>DTS.forEach(d=>h+=`<th style="font-size:.57rem;color:#4d5566">${d==='TRADES'?'TR':'BA'}</th>`));
  h+='<th></th></tr>';
  document.getElementById('ghead').innerHTML=h;
}

function cellHtml(c){
  if(!c||c.s==='miss')return'<span class="cm">—</span>';
  if(c.s==='active')return`<span class="ca">▶ ${fmtK(c.c)}</span>`;
  if(c.s==='done')  return`<span class="cd">✓ ${c.c>0?fmtK(c.c):''}</span>`;
  return'<span class="cm">—</span>';
}

async function pollFiles(){
  try{
    const rows=await(await fetch('/api/files')).json();
    let html='';
    for(const row of rows){
      html+=`<tr><td class="dc">${row.date}</td>`;
      SYMS.forEach(s=>DTS.forEach(dt=>{html+=`<td>${cellHtml(row.cells[s+'_'+dt])}</td>`;}));
      const tags=(row.tags||[]).map(([tier,lbl])=>`<span class="tag ${tier==='P0'?'tp0':'tp1'}">${lbl}</span>`).join('');
      html+=`<td>${tags}</td></tr>`;
    }
    document.getElementById('gbody').innerHTML=html;
  }catch(e){console.error(e)}
}

async function pollPrices(){
  try{
    const p=await(await fetch('/api/prices')).json();
    SYMS.forEach(s=>{
      const el=document.getElementById('p-'+s);
      if(el)el.textContent=p[s]!=null?p[s].toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
    });
  }catch(e){}
}

buildHead();
function tick(){pollStatus();pollQueue();pollFiles();}
tick();
setInterval(tick,10000);
pollPrices();
setInterval(pollPrices,60000);
</script>
</body></html>"""

@app.route("/")
def index():
    return _HTML

if __name__ == "__main__":
    import argparse as _ap
    p = _ap.ArgumentParser()
    p.add_argument("--real", action="store_true")
    p.add_argument("--port", type=int, default=5050)
    args = p.parse_args()
    if not args.real:
        print("[dashboard] --real flag required"); import sys; sys.exit(0)
    def _used(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0
    if _used(args.port):
        print(f"[dashboard] port {args.port} in use — exiting"); import sys; sys.exit(0)
    print(f"[dashboard] FETCHER2026 {VERSION} — http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
