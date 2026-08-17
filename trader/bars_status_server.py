"""
trader/bars_status_server.py  v1.0

Live status dashboard for all bars{N}s fetchers (1s/5s/30s/... — whatever is
configured in FETCHERS below), served on http://localhost:5004/.

Replaces the old bars1s_viewer.py static-HTML-plus-meta-refresh approach.
That approach's ETA looked "stuck" because it fell back to a fixed
theoretical pacing rate whenever the before/after snapshot between two CLI
invocations showed no progress (common now that 1s/5s/30s fetchers run
concurrently and each individual day can spend minutes retrying against real
IB pacing pressure). This server instead samples actual DB progress on a
fixed interval into a rolling in-memory history and computes ETA from real
observed throughput over that window — if there isn't enough history yet it
says so plainly instead of showing a misleading number.

Usage:
  python trader/bars_status_server.py            # serves on 0.0.0.0:5004
  python trader/bars_status_server.py --port 5005
"""

import argparse
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

_ROOT = Path(__file__).parent.parent
_DATA = _ROOT / "data"

# What's currently running. bar_secs -> (symbols, days, chunks_per_day).
# chunks_per_day = ceil(86400 / max_chunk_secs_for_that_bar_size) — see
# _BAR_SECS_TABLE in bars1s_fetcher.py for where these durations come from.
FETCHERS = {
    "1s":  {"label": "1-second bars",  "symbols": ["MES", "MNQ", "MYM", "M2K"], "days": 84,  "chunks_per_day": 48},
    "5s":  {"label": "5-second bars",  "symbols": ["MES", "MNQ"],               "days": 42,  "chunks_per_day": 24},
    "30s": {"label": "30-second bars", "symbols": ["MES", "MNQ", "MYM", "M2K"], "days": 252, "chunks_per_day": 3},
}

_STALE_S = 20 * 60   # flag stalled if no DB write in this long while pairs remain (widened vs
                     # the single-fetcher viewer — concurrent fetchers + real pacing pressure
                     # mean a single day can legitimately take much longer between writes now)
_HIST_WINDOW_S = 20 * 60   # how far back the rolling ETA window looks
_SAMPLE_EVERY_S = 10

_SERIES_COLORS = {
    "MES": {"light": "#2a78d6", "dark": "#3987e5"},
    "MNQ": {"light": "#eb6834", "dark": "#d95926"},
    "MYM": {"light": "#1baf7a", "dark": "#199e70"},
    "M2K": {"light": "#eda100", "dark": "#c98500"},
}

_history: dict[str, deque] = {k: deque() for k in FETCHERS}
_hist_lock = threading.Lock()

app = Flask(__name__)


# ── background sampler ─────────────────────────────────────────────────────

def _read_db(suffix: str):
    db = _DATA / f"bars{suffix}_progress.db"
    if not db.exists():
        return None
    try:
        uri = f"file:{db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        rows = conn.execute(
            "SELECT symbol, date, chunks_done, bars_fetched, finished, updated_at FROM progress"
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return None


def _sample_loop():
    while True:
        now = time.time()
        for suffix in FETCHERS:
            rows = _read_db(suffix)
            chunks_done_total = sum(r[2] or 0 for r in rows) if rows else 0
            with _hist_lock:
                hist = _history[suffix]
                hist.append((now, chunks_done_total))
                cutoff = now - _HIST_WINDOW_S - _SAMPLE_EVERY_S
                while hist and hist[0][0] < cutoff:
                    hist.popleft()
        time.sleep(_SAMPLE_EVERY_S)


# ── stats computation ───────────────────────────────────────────────────────

def _compute_stats(suffix: str):
    cfg = FETCHERS[suffix]
    rows = _read_db(suffix) or []
    symbols = cfg["symbols"]
    target_total_chunks = cfg["chunks_per_day"] * cfg["days"] * len(symbols)

    by_symbol_month: dict = {}
    per_symbol_totals: dict = {s: 0 for s in symbols}
    per_symbol_pairs_done: dict = {s: 0 for s in symbols}
    chunks_done_total = 0
    bars_total = 0
    pairs_finished = 0
    latest_update = None

    for sym, date_str, chunks_done, bars, finished, updated_at in rows:
        if updated_at and (latest_update is None or updated_at > latest_update):
            latest_update = updated_at
        chunks_done_total += chunks_done or 0
        bars_total += bars or 0
        if sym in per_symbol_totals:
            per_symbol_totals[sym] += bars or 0
            ym = date_str[:7]
            by_symbol_month.setdefault(sym, {}).setdefault(ym, 0)
            by_symbol_month[sym][ym] += bars or 0
        if finished:
            pairs_finished += 1
            if sym in per_symbol_pairs_done:
                per_symbol_pairs_done[sym] += 1

    months = sorted({ym for m in by_symbol_month.values() for ym in m})

    # health
    if latest_update:
        last = datetime.fromisoformat(latest_update)
        gap = (datetime.now(timezone.utc) - last).total_seconds()
    else:
        gap = None
    if target_total_chunks and chunks_done_total >= target_total_chunks:
        health = "complete"
    elif gap is None:
        health = "not started"
    elif gap > _STALE_S:
        health = "stalled"
    else:
        health = "running"

    # ETA from rolling observed throughput (NOT a fixed theoretical fallback —
    # if there isn't enough real history yet, say so instead of guessing)
    with _hist_lock:
        hist = list(_history[suffix])
    eta_seconds = None
    rate_per_min = None
    if len(hist) >= 2:
        (t0, c0), (t1, c1) = hist[0], hist[-1]
        dt = t1 - t0
        dc = c1 - c0
        if dt >= 60 and dc > 0:
            rate = dc / dt   # chunks/sec
            rate_per_min = rate * 60
            remaining = max(0, target_total_chunks - chunks_done_total)
            eta_seconds = remaining / rate if rate > 0 else None

    return {
        "suffix": suffix,
        "label": cfg["label"],
        "symbols": symbols,
        "months": months,
        "by_symbol_month": by_symbol_month,
        "per_symbol_totals": per_symbol_totals,
        "per_symbol_pairs_done": per_symbol_pairs_done,
        "days": cfg["days"],
        "pairs_finished": pairs_finished,
        "pairs_total": cfg["days"] * len(symbols),
        "bars_total": bars_total,
        "chunks_done_total": chunks_done_total,
        "target_total_chunks": target_total_chunks,
        "pct": round(100 * chunks_done_total / target_total_chunks, 1) if target_total_chunks else 0,
        "health": health,
        "gap_s": gap,
        "eta_seconds": eta_seconds,
        "rate_per_min": rate_per_min,
        "hist_samples": len(hist),
    }


def _fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if not d: parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


# ── HTML rendering ──────────────────────────────────────────────────────────

def _render_section(stats):
    symbols = stats["symbols"]
    months = stats["months"]
    max_month_val = max(
        (stats["by_symbol_month"].get(s, {}).get(ym, 0) for s in symbols for ym in months),
        default=1,
    ) or 1

    badge_class = {"running": "st-good", "complete": "st-good",
                   "stalled": "st-critical", "not started": "st-warn"}.get(stats["health"], "st-warn")
    badge_label = stats["health"].upper()

    legend_items = "".join(
        f'<span class="legend-item"><span class="swatch" style="background:var(--s-{s})"></span>{s}</span>'
        for s in symbols
    )

    groups_html = []
    for ym in months:
        bars = []
        for s in symbols:
            v = stats["by_symbol_month"].get(s, {}).get(ym, 0)
            h_pct = round(100 * v / max_month_val, 1)
            bars.append(f'<div class="bar" style="height:{h_pct}%;background:var(--s-{s})" '
                        f'title="{s} {ym}: {v:,} bars"></div>')
        groups_html.append(f'<div class="group"><div class="bars">{"".join(bars)}</div>'
                          f'<div class="month-label">{ym}</div></div>')
    chart_html = "".join(groups_html) or '<div class="empty-note">No data yet</div>'

    progress_rows = []
    per_symbol_total_days = stats["days"]
    for s in symbols:
        done = stats["per_symbol_pairs_done"].get(s, 0)
        spct = round(100 * done / per_symbol_total_days, 1) if per_symbol_total_days else 0
        progress_rows.append(f'''
        <div class="prow">
          <div class="prow-label"><span class="swatch" style="background:var(--s-{s})"></span>{s}</div>
          <div class="ptrack"><div class="pfill" style="width:{spct}%;background:var(--s-{s})"></div></div>
          <div class="prow-num">{done}/{per_symbol_total_days} days &middot; {stats['per_symbol_totals'].get(s,0):,} bars</div>
        </div>''')
    progress_html = "".join(progress_rows)

    gap_str = f"{stats['gap_s']/60:.1f} min ago" if stats["gap_s"] is not None else "n/a"
    eta_str = _fmt_duration(stats["eta_seconds"]) or (
        "warming up…" if stats["hist_samples"] < 2 else "no recent progress"
    )
    rate_str = f"{stats['rate_per_min']:.1f} chunks/min (observed)" if stats["rate_per_min"] else \
        f"gathering samples ({stats['hist_samples']} so far)"

    return f'''
<div class="card">
  <div class="section-title">{stats['label']} <span class="muted">({'+'.join(symbols)}, {stats['days']} days)</span></div>
  <div class="hero">
    <div class="stat"><div class="num">{stats['bars_total']:,}</div><div class="label">bars collected</div></div>
    <div class="stat"><div class="num">{stats['pct']}%</div><div class="label">chunks complete ({stats['chunks_done_total']:,}/{stats['target_total_chunks']:,})</div></div>
    <div class="stat"><div class="num">{stats['pairs_finished']:,}/{stats['pairs_total']:,}</div><div class="label">symbol-days finished</div></div>
    <div class="stat"><div class="num">{eta_str}</div><div class="label">estimated time remaining</div></div>
    <div class="stat">
      <span class="badge {badge_class}">{badge_label}</span>
      <div class="label" style="margin-top:6px">last write: {gap_str}</div>
    </div>
  </div>
  <div class="rate-line">{rate_str}</div>
  <div class="legend">{legend_items}</div>
  <div class="chart">{chart_html}</div>
  <div class="progress-block">{progress_html}</div>
</div>
'''


def _render_page():
    color_vars_light = []
    color_vars_dark = []
    for s, c in _SERIES_COLORS.items():
        color_vars_light.append(f"  --s-{s}: {c['light']};")
        color_vars_dark.append(f"  --s-{s}: {c['dark']};")
    color_vars_light = "\n".join(color_vars_light)
    color_vars_dark = "\n".join(color_vars_dark)

    sections = "".join(_render_section(_compute_stats(suffix)) for suffix in FETCHERS)
    now = datetime.now(timezone.utc).isoformat()

    return f"""<!doctype html>
<title>bars fetchers — live status</title>
<meta http-equiv="refresh" content="10">
<style>
:root {{
  color-scheme: light;
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
{color_vars_light}
  --st-good: #0ca30c; --st-warn: #fab219; --st-critical: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
{color_vars_dark}
    --st-good: #0ca30c; --st-warn: #fab219; --st-critical: #d03b3b;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-1: #1a1a19; --page: #0d0d0d;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
  --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
{color_vars_dark}
  --st-good: #0ca30c; --st-warn: #fab219; --st-critical: #d03b3b;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:24px; background:var(--page); color:var(--text-primary);
       font-family: system-ui,-apple-system,"Segoe UI",sans-serif; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.sub {{ color:var(--text-secondary); font-size:13px; margin-bottom:20px; }}
.card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:10px;
         padding:18px 20px; margin-bottom:20px; }}
.section-title {{ font-size:15px; font-weight:600; margin-bottom:14px; }}
.muted {{ color:var(--text-secondary); font-weight:400; font-size:13px; }}
.hero {{ display:flex; flex-wrap:wrap; gap:24px; align-items:center; }}
.stat {{ min-width:140px; }}
.stat .num {{ font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; }}
.stat .label {{ font-size:12px; color:var(--text-secondary); }}
.badge {{ display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px;
          font-size:12px; font-weight:600; color:white; }}
.st-good {{ background:var(--st-good); }}
.st-warn {{ background:var(--st-warn); color:#2a1e00; }}
.st-critical {{ background:var(--st-critical); }}
.rate-line {{ font-size:12px; color:var(--text-secondary); margin:10px 0 6px; }}
.legend {{ display:flex; gap:16px; margin-bottom:10px; font-size:12px; color:var(--text-secondary); }}
.legend-item {{ display:flex; align-items:center; gap:5px; }}
.swatch {{ width:10px; height:10px; border-radius:2px; display:inline-block; }}
.chart {{ display:flex; align-items:flex-end; gap:14px; height:160px; overflow-x:auto;
          padding-top:8px; border-bottom:1px solid var(--grid); }}
.group {{ display:flex; flex-direction:column; align-items:center; min-width:56px; height:100%; justify-content:flex-end; }}
.bars {{ display:flex; align-items:flex-end; gap:3px; height:100%; }}
.bar {{ width:10px; border-radius:3px 3px 0 0; min-height:2px; }}
.month-label {{ font-size:11px; color:var(--text-muted); margin-top:6px; white-space:nowrap; }}
.empty-note {{ color:var(--text-muted); font-size:13px; padding:20px 0; }}
.progress-block {{ margin-top:12px; }}
.prow {{ display:grid; grid-template-columns:90px 1fr 220px; align-items:center; gap:12px; margin:10px 0; }}
.prow-label {{ display:flex; align-items:center; gap:6px; font-weight:600; font-size:13px; }}
.ptrack {{ background:var(--grid); border-radius:6px; height:10px; overflow:hidden; }}
.pfill {{ height:100%; border-radius:6px; }}
.prow-num {{ font-size:12px; color:var(--text-secondary); text-align:right; font-variant-numeric:tabular-nums; }}
.footer {{ font-size:12px; color:var(--text-muted); margin-top:8px; }}
</style>
<h1>bars fetchers — live status</h1>
<div class="sub">Generated {now} &middot; auto-refreshes every 10s &middot; ETA uses real observed throughput over a rolling {_HIST_WINDOW_S//60}-min window, not a fixed theoretical rate</div>
{sections}
<div class="footer">Served by trader/bars_status_server.py on port {app.config.get('PORT', 5004)} &middot; JSON: <code>/api/status</code></div>
"""


@app.route("/")
def index():
    return _render_page()


@app.route("/api/status")
def api_status():
    return jsonify({suffix: _compute_stats(suffix) for suffix in FETCHERS})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live bars-fetcher status server")
    parser.add_argument("--port", type=int, default=5004)
    args = parser.parse_args()
    app.config["PORT"] = args.port

    t = threading.Thread(target=_sample_loop, daemon=True)
    t.start()

    print(f"bars_status_server: http://localhost:{args.port}/  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
