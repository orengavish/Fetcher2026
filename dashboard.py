#!/usr/bin/env python3
"""
dashboard.py — Fetcher2026 single-page dashboard
  python dashboard.py --real    # live mode (port 5050)
  python dashboard.py           # exits immediately if not --real
"""

import argparse, csv, socket, sqlite3, sys, threading, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

import psutil
from flask import Flask, jsonify

# ── Config & paths ────────────────────────────────────────────────────────────

from lib.config_loader import get_config
_cfg        = get_config()
HISTORY_DIR = Path(_cfg.paths.history)
GALAO_DB    = Path(_cfg.paths.db)
PROGRESS_DB = GALAO_DB.parent / "fetch_progress.db"

SYMBOLS  = ["MES", "MNQ", "MYM", "M2K"]
DTYPES   = ["TRADES", "BID_ASK"]
CT       = ZoneInfo("America/Chicago")
FETCHER2026_ROOT = str(_ROOT).replace("\\", "/").lower()

app = Flask(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_working_day() -> date:
    d = (datetime.now(CT) - timedelta(days=1)).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def _working_days_back(n: int) -> list:
    days, d = [], (datetime.now(CT) - timedelta(days=1)).date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days

def _csv_key(sym: str, dtype: str, d: date) -> str:
    dt = "trades" if dtype == "TRADES" else "bidask"
    return f"{sym}_{dt}_{d.strftime('%Y%m%d')}.csv"

def _csv_exists(sym: str, dtype: str, d: date) -> bool:
    p = HISTORY_DIR / _csv_key(sym, dtype, d)
    return p.exists() and p.stat().st_size > 500

def _gw_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 4002), timeout=1.5)
        s.close(); return True
    except OSError:
        return False

def _fetcher_pid() -> int | None:
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if "fetch_scheduler" in cmd and FETCHER2026_ROOT in cmd.replace("\\", "/").lower():
                return p.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def _load_progress() -> dict:
    """Return dict (sym, date_str, dtype) -> row dict."""
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

def _vt_counts() -> dict:
    """Return {date_str: count} from verified_trades."""
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

def _throughput() -> dict:
    now      = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_ago = now - timedelta(hours=1)
    min_ago  = now - timedelta(minutes=1)
    result   = {"midnight": 0, "hour": 0, "minute": 0, "last_ts": None, "active": None}
    try:
        conn = sqlite3.connect(str(PROGRESS_DB), timeout=3)
        def _sum(since):
            r = conn.execute(
                "SELECT SUM(records_fetched) FROM fetch_progress WHERE updated_at >= ?",
                (since.isoformat(),)
            ).fetchone()
            return int(r[0] or 0)
        result["midnight"] = _sum(midnight)
        result["hour"]     = _sum(hour_ago)
        result["minute"]   = _sum(min_ago)
        row = conn.execute(
            "SELECT symbol, date, data_type, records_fetched, updated_at "
            "FROM fetch_progress WHERE finished=0 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row:
            result["last_ts"] = row[4]
            result["active"]  = {
                "sym": row[0], "date": row[1], "dtype": row[2], "count": row[3]
            }
        conn.close()
    except Exception:
        pass
    return result

def _last_price(sym: str) -> float | None:
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

_start_ts = time.time()
_last_fail_ts = [0.0]  # mutable so we can update from health check

def _build_queue(progress: dict, vt: dict) -> list:
    """Build priority-ordered fetch queue."""
    seen = set()
    queue = []

    def _is_done(sym, d, dtype):
        key = (sym, d.isoformat(), dtype)
        row = progress.get(key)
        return _csv_exists(sym, dtype, d) or (row and row.get("finished"))

    def _status(sym, d, dtype):
        key = (sym, d.isoformat(), dtype)
        row = progress.get(key)
        if _is_done(sym, d, dtype):
            return "done"
        if row and not row.get("finished") and row.get("records_fetched", 0) > 0:
            # active if updated within 90s
            try:
                ts = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
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
            "date_label": d.strftime("%-m/%-d") if sys.platform != "win32" else f"{d.month}/{d.day}",
            "sym": sym,
            "dtype": dtype,
            "reason": reason,
            "status": st,
        })

    lwd = _last_working_day()

    # P0: last working day, all 4 symbols
    for sym in SYMBOLS:
        for dtype in DTYPES:
            _add(sym, lwd, dtype, "P0", "last day")

    # P1: verified trade dates ordered by count desc
    vt_dates = sorted(vt.items(), key=lambda x: -x[1])
    for d_str, n in vt_dates:
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            continue
        if d == lwd:
            continue  # already in P0
        for sym in SYMBOLS:
            for dtype in DTYPES:
                _add(sym, d, dtype, "P1", f"{n} vt")

    # P2: partial files (resume)
    for (sym, d_str, dtype), row in progress.items():
        if not row.get("finished") and row.get("records_fetched", 0) > 0:
            try:
                d = date.fromisoformat(d_str)
            except Exception:
                continue
            _add(sym, d, dtype, "P2", "resume")

    # P3: backfill last 90 working days
    for d in _working_days_back(90):
        if d == lwd:
            continue
        if any(d.isoformat() == ds for ds, _ in vt_dates):
            continue
        for sym in SYMBOLS:
            for dtype in DTYPES:
                _add(sym, d, dtype, "P3", "backfill")

    return queue

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    gw  = _gw_up()
    pid = _fetcher_pid()
    thr = _throughput()
    now = time.time()
    clean_min = int((now - _last_fail_ts[0]) / 60) if not _last_fail_ts[0] else int((now - _start_ts) / 60)
    return jsonify({
        "gateway": gw,
        "fetcher_pid": pid,
        "clean_minutes": clean_min,
        "throughput": thr,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/api/queue")
def api_queue():
    progress = _load_progress()
    vt       = _vt_counts()
    queue    = _build_queue(progress, vt)
    return jsonify(queue)

@app.route("/api/files")
def api_files():
    progress = _load_progress()
    vt       = _vt_counts()
    lwd      = _last_working_day()

    # Collect all relevant dates
    all_dates = set()
    for (_, d_str, _) in progress.keys():
        all_dates.add(d_str)
    for f in HISTORY_DIR.glob("*_trades_????????.csv"):
        parts = f.stem.split("_")
        if len(parts) >= 3:
            dc = parts[-1]
            try:
                all_dates.add(f"{dc[:4]}-{dc[4:6]}-{dc[6:]}")
            except Exception:
                pass
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
                key = (sym, d_str, dtype)
                row = progress.get(key)
                exists = _csv_exists(sym, dtype, d)
                if exists or (row and row.get("finished")):
                    vs = row.get("verify_status") if row else None
                    cells[f"{sym}_{dtype}"] = {"status": "done", "verify": vs, "count": row.get("records_fetched", 0) if row else 0}
                elif row and not row.get("finished") and row.get("records_fetched", 0) > 0:
                    cells[f"{sym}_{dtype}"] = {"status": "active", "count": row.get("records_fetched", 0)}
                else:
                    cells[f"{sym}_{dtype}"] = {"status": "missing", "count": 0}

        tags = []
        if d == lwd:
            tags.append("LAST DAY")
        if d_str in vt:
            tags.append(f"VT {vt[d_str]}")

        rows.append({"date": d_str, "cells": cells, "tags": tags})

    return jsonify(rows)

@app.route("/api/prices")
def api_prices():
    return jsonify({sym: _last_price(sym) for sym in SYMBOLS})

# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fetcher2026</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh;font-size:13px}
a{color:inherit;text-decoration:none}

/* ── header ── */
.hdr{display:flex;align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid #21262d;flex-wrap:wrap}
.hdr h1{font-size:.75rem;font-weight:700;color:#6e7681;letter-spacing:.12em;margin-right:4px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:3px;vertical-align:middle}
.dot-g{background:#3fb950}.dot-r{background:#f85149}.dot-y{background:#d29922}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.dot-a{background:#f0883e;animation:pulse 1.2s infinite}
.pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font-size:.68rem;font-weight:600}
.pill-g{background:#14532d22;color:#3fb950;border:1px solid #23863633}
.pill-r{background:#3d100022;color:#f85149;border:1px solid #7d1c1c44}
.pill-y{background:#3d2f0022;color:#d29922;border:1px solid #7d5c1044}
.hdr-time{margin-left:auto;font-size:.65rem;color:#4d5566;font-family:monospace}

/* ── section ── */
.sec{padding:8px 16px;border-bottom:1px solid #161b22}
.sec-title{font-size:.62rem;font-weight:700;color:#4d5566;letter-spacing:.1em;margin-bottom:6px;text-transform:uppercase}

/* ── metrics row ── */
.metrics{display:flex;gap:20px;flex-wrap:wrap;align-items:center}
.metric{display:flex;flex-direction:column;gap:1px}
.metric-val{font-size:1.1rem;font-weight:700;color:#e6edf3;font-family:monospace}
.metric-lbl{font-size:.59rem;color:#4d5566;text-transform:uppercase;letter-spacing:.06em}
.metric-sub{font-size:.62rem;color:#6e7681;font-family:monospace}

/* ── prices ── */
.prices{display:flex;gap:20px;flex-wrap:wrap;align-items:baseline}
.sym-price{display:flex;flex-direction:column;gap:1px;min-width:70px}
.sym-name{font-size:.59rem;color:#6e7681;font-weight:700;letter-spacing:.08em}
.sym-val{font-size:.95rem;font-weight:700;color:#e6edf3;font-family:monospace}

/* ── active fetch ── */
.active-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 14px;display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.active-info{display:flex;flex-direction:column;gap:3px;min-width:140px}
.active-sym{font-size:.9rem;font-weight:700;color:#e6edf3}
.active-detail{font-size:.65rem;color:#6e7681}
.pbar-wrap{flex:1;min-width:160px}
.pbar{background:#0d1117;border-radius:4px;height:7px;overflow:hidden;border:1px solid #21262d;margin-bottom:4px}
.pbar-fill{height:100%;background:linear-gradient(90deg,#c2410c,#f0883e);border-radius:4px;transition:width .8s ease}
.pbar-meta{font-size:.63rem;color:#6e7681;font-family:monospace;display:flex;justify-content:space-between}
.active-none{color:#4d5566;font-size:.75rem;font-style:italic}

/* ── queue ── */
.queue-wrap{max-height:260px;overflow-y:auto}
.queue-wrap::-webkit-scrollbar{width:4px}
.queue-wrap::-webkit-scrollbar-track{background:#0d1117}
.queue-wrap::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
.qtable{width:100%;border-collapse:collapse}
.qtable th{font-size:.59rem;color:#4d5566;letter-spacing:.07em;text-transform:uppercase;padding:3px 6px;border-bottom:1px solid #21262d;text-align:left;position:sticky;top:0;background:#0d1117;z-index:1}
.qtable td{padding:3px 6px;border-bottom:1px solid #0d111766;vertical-align:middle;white-space:nowrap}
.qtable tr:hover td{background:#ffffff04}
.tier{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700;min-width:24px;text-align:center}
.t0{background:#c2410c33;color:#f0883e;border:1px solid #c2410c55}
.t1{background:#92400e33;color:#d29922;border:1px solid #92400e55}
.t2{background:#1e3a5f33;color:#60a5fa;border:1px solid #1e40af55}
.t3{background:#21262d;color:#4d5566;border:1px solid #30363d}
.sts{font-size:.6rem;font-weight:600;padding:1px 5px;border-radius:3px}
.sts-active{color:#f0883e;animation:pulse 1.2s infinite}
.sts-done{color:#3fb95066}
.sts-resume{color:#60a5fa}
.sts-pending{color:#4d5566}
.q-date{font-family:monospace;color:#8b949e;font-size:.72rem}
.q-sym{font-weight:700;color:#c9d1d9;font-size:.72rem}
.q-dtype{font-size:.62rem;color:#6e7681}
.q-reason{font-size:.65rem}
.tr-done td{opacity:.35}

/* ── files grid ── */
.grid-wrap{overflow-x:auto;max-height:340px;overflow-y:auto}
.grid-wrap::-webkit-scrollbar{width:4px;height:4px}
.grid-wrap::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
.gtable{border-collapse:collapse;font-size:.67rem;white-space:nowrap}
.gtable th{padding:4px 5px;text-align:center;font-weight:600;color:#6e7681;border-bottom:2px solid #21262d;position:sticky;top:0;background:#0d1117;z-index:1}
.gtable th.date-h{text-align:left;min-width:70px;padding-left:0}
.gtable th.sym-h{border-left:1px solid #21262d;font-size:.7rem;color:#c9d1d9}
.gtable td{padding:2px 4px;border-bottom:1px solid #0d1117;vertical-align:middle}
.gtable td.date-c{font-family:monospace;color:#6e7681;font-size:.65rem;padding-left:0;position:sticky;left:0;background:#0d1117;z-index:1}
.gtable tr:hover td{background:#ffffff04}
.gtable tr:hover td.date-c{background:#0d1117}
.cell-done-v{background:#14532d44;color:#3fb950;border-radius:3px;padding:1px 4px;font-size:.58rem;font-weight:700}
.cell-done-u{background:#14532d22;color:#6e9e6e;border-radius:3px;padding:1px 4px;font-size:.58rem}
.cell-act{background:#c2410c22;color:#f0883e;border-radius:3px;padding:1px 4px;font-size:.58rem;animation:pulse 1.2s infinite}
.cell-miss{background:#21262d;color:#4d5566;border-radius:3px;padding:1px 4px;font-size:.58rem}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.57rem;font-weight:700;margin-right:2px}
.tag-ld{background:#c2410c33;color:#f0883e}
.tag-vt{background:#92400e33;color:#d29922}
</style>
</head>
<body>

<!-- header -->
<div class="hdr">
  <h1>FETCHER2026</h1>
  <span id="pill-gw" class="pill pill-r"><span class="dot dot-r" id="dot-gw"></span>Gateway</span>
  <span id="pill-ft" class="pill pill-r"><span class="dot dot-r" id="dot-ft"></span>Fetcher</span>
  <span class="pill pill-g"><span class="dot dot-g"></span>Dashboard</span>
  <span id="clean-lbl" style="font-size:.65rem;color:#4d5566"></span>
  <span class="hdr-time" id="hdr-time">—</span>
</div>

<!-- prices -->
<div class="sec">
  <div class="sec-title">Prices</div>
  <div class="prices" id="prices-row">
    <div class="sym-price"><span class="sym-name">MES</span><span class="sym-val" id="p-MES">—</span></div>
    <div class="sym-price"><span class="sym-name">MNQ</span><span class="sym-val" id="p-MNQ">—</span></div>
    <div class="sym-price"><span class="sym-name">MYM</span><span class="sym-val" id="p-MYM">—</span></div>
    <div class="sym-price"><span class="sym-name">M2K</span><span class="sym-val" id="p-M2K">—</span></div>
  </div>
</div>

<!-- throughput + active -->
<div class="sec">
  <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
    <div>
      <div class="sec-title">Throughput</div>
      <div class="metrics">
        <div class="metric"><span class="metric-val" id="thr-mid">—</span><span class="metric-lbl">Since midnight</span></div>
        <div class="metric"><span class="metric-val" id="thr-hr">—</span><span class="metric-lbl">Last hour</span></div>
        <div class="metric"><span class="metric-val" id="thr-min">—</span><span class="metric-lbl">Last minute</span></div>
        <div class="metric"><span class="metric-val" id="thr-ts" style="font-size:.75rem">—</span><span class="metric-lbl">Last data</span></div>
      </div>
    </div>
    <div style="flex:1;min-width:220px">
      <div class="sec-title">Active Fetch</div>
      <div id="active-card" class="active-card">
        <span class="active-none">No active fetch</span>
      </div>
    </div>
  </div>
</div>

<!-- priority queue -->
<div class="sec">
  <div class="sec-title">Priority Queue</div>
  <div class="queue-wrap">
    <table class="qtable">
      <thead><tr>
        <th>Tier</th><th>Date</th><th>Sym</th><th>Type</th><th>Reason</th><th>Status</th>
      </tr></thead>
      <tbody id="queue-body"></tbody>
    </table>
  </div>
</div>

<!-- files grid -->
<div class="sec" style="border-bottom:none">
  <div class="sec-title">Fetched Files</div>
  <div class="grid-wrap">
    <table class="gtable">
      <thead id="grid-head"></thead>
      <tbody id="grid-body"></tbody>
    </table>
  </div>
</div>

<script>
const SYMS=['MES','MNQ','MYM','M2K'], DTS=['TRADES','BID_ASK'];

function fmtK(n){
  if(n==null||n===undefined)return'—';
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'K';
  return String(Math.round(n));
}

function fmtTs(ts){
  if(!ts)return'—';
  try{
    const d=new Date(ts);
    return d.toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour:'2-digit',minute:'2-digit',second:'2-digit'})+" CT";
  }catch{return ts.slice(11,19)+' UTC';}
}

// ── Status ──
async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    // gateway
    const gwUp=d.gateway;
    document.getElementById('pill-gw').className='pill '+(gwUp?'pill-g':'pill-r');
    document.getElementById('dot-gw').className='dot '+(gwUp?'dot-g':'dot-r');
    // fetcher
    const ftUp=!!d.fetcher_pid;
    document.getElementById('pill-ft').className='pill '+(ftUp?'pill-g':'pill-r');
    document.getElementById('dot-ft').className='dot '+(ftUp?'dot-g':'dot-r');
    // clean uptime
    document.getElementById('clean-lbl').textContent=
      'Clean: '+(d.clean_minutes>=60?Math.floor(d.clean_minutes/60)+'h '+(d.clean_minutes%60)+'m':d.clean_minutes+'m');
    document.getElementById('hdr-time').textContent=new Date().toLocaleTimeString();
    // throughput
    const t=d.throughput||{};
    document.getElementById('thr-mid').textContent=fmtK(t.midnight);
    document.getElementById('thr-hr').textContent=fmtK(t.hour);
    document.getElementById('thr-min').textContent=fmtK(t.minute);
    document.getElementById('thr-ts').textContent=fmtTs(t.last_ts);
    // active fetch
    const ac=document.getElementById('active-card');
    if(t.active){
      const a=t.active;
      ac.innerHTML=`
        <div class="active-info">
          <div class="active-sym">${a.sym} <span style="color:#6e7681;font-size:.75rem">${a.date}</span></div>
          <div class="active-detail">${a.dtype}</div>
        </div>
        <div class="pbar-wrap">
          <div class="pbar"><div class="pbar-fill" style="width:${Math.min(100,Math.round(a.count/(a.target||62000)*100))}%"></div></div>
          <div class="pbar-meta"><span>${fmtK(a.count)} ticks</span></div>
        </div>`;
    } else {
      ac.innerHTML='<span class="active-none">No active fetch</span>';
    }
  }catch(e){console.error('status',e)}
}

// ── Queue ──
const TIER_CLS=['t0','t1','t2','t3'];
const STS_CLS={active:'sts-active',done:'sts-done',resume:'sts-resume',pending:'sts-pending'};

async function pollQueue(){
  try{
    const items=await(await fetch('/api/queue')).json();
    const tbody=document.getElementById('queue-body');
    tbody.innerHTML=items.map(r=>`
      <tr class="${r.status==='done'?'tr-done':''}">
        <td><span class="tier ${TIER_CLS[parseInt(r.tier[1])||3]}">${r.tier}</span></td>
        <td class="q-date">${r.date_label}</td>
        <td class="q-sym">${r.sym}</td>
        <td class="q-dtype">${r.dtype==='BID_ASK'?'BA':'TR'}</td>
        <td class="q-reason" style="color:${r.tier==='P0'?'#f0883e':r.tier==='P1'?'#d29922':r.tier==='P2'?'#60a5fa':'#4d5566'}">${r.reason}</td>
        <td><span class="sts ${STS_CLS[r.status]||'sts-pending'}">${r.status.toUpperCase()}</span></td>
      </tr>`).join('');
  }catch(e){console.error('queue',e)}
}

// ── Files grid ──
function buildGridHead(){
  let h='<tr><th class="date-h">Date</th>';
  SYMS.forEach(s=>h+=`<th class="sym-h" colspan="2">${s}</th>`);
  h+='<th>Tags</th></tr><tr><th></th>';
  SYMS.forEach(()=>DTS.forEach(d=>h+=`<th style="font-size:.58rem;color:#4d5566">${d==='TRADES'?'TR':'BA'}</th>`));
  h+='<th></th></tr>';
  document.getElementById('grid-head').innerHTML=h;
}

function cellHtml(c){
  if(!c)return`<span class="cell-miss">—</span>`;
  if(c.status==='active')return`<span class="cell-act">…${fmtK(c.count)}</span>`;
  if(c.status==='done'){
    const v=c.verify;
    if(v==='pass'||v==='warn')return`<span class="cell-done-v">✓${fmtK(c.count)}</span>`;
    return`<span class="cell-done-u">· ${fmtK(c.count)}</span>`;
  }
  return`<span class="cell-miss">—</span>`;
}

async function pollFiles(){
  try{
    const rows=await(await fetch('/api/files')).json();
    let html='';
    for(const row of rows){
      const d=row.date;
      html+=`<tr><td class="date-c">${d}</td>`;
      SYMS.forEach(s=>DTS.forEach(dt=>{
        html+=`<td style="text-align:center">${cellHtml(row.cells[s+'_'+dt])}</td>`;
      }));
      const tags=(row.tags||[]).map(t=>
        `<span class="tag ${t.startsWith('LAST')?'tag-ld':'tag-vt'}">${t}</span>`
      ).join('');
      html+=`<td style="white-space:nowrap">${tags}</td></tr>`;
    }
    document.getElementById('grid-body').innerHTML=html;
  }catch(e){console.error('files',e)}
}

// ── Prices ──
async function pollPrices(){
  try{
    const p=await(await fetch('/api/prices')).json();
    ['MES','MNQ','MYM','M2K'].forEach(s=>{
      const el=document.getElementById('p-'+s);
      if(el) el.textContent=p[s]!=null?p[s].toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
    });
  }catch(e){}
}

// ── Init ──
buildGridHead();
let _priceTimer=null;
function refresh(){
  pollStatus();
  pollQueue();
  pollFiles();
}
refresh();
setTimeout(function loop(){pollStatus();pollQueue();pollFiles();setTimeout(loop,10000);},10000);
pollPrices();
setTimeout(function ploop(){pollPrices();setTimeout(ploop,60000);},60000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return _HTML

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse as _ap
    parser = _ap.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    if not args.real:
        print("[dashboard] --real flag required. Exiting.")
        sys.exit(0)

    # Single-instance guard
    def _port_in_use(p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", p)) == 0
    if _port_in_use(args.port):
        print(f"[dashboard] port {args.port} already in use — exiting")
        sys.exit(0)

    print(f"[dashboard] FETCHER2026 — http://0.0.0.0:{args.port}")
    print(f"[dashboard] history: {HISTORY_DIR}")
    print(f"[dashboard] progress db: {PROGRESS_DB}")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
