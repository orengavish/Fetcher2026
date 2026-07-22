"""
trader/bars1s_viewer.py  v1.0

Status viewer for bars1s_fetcher. Reads data/bars1s_progress.db and reports:
  - bars collected per symbol, per month, per year
  - overall completion % and a health check (running / stalled / complete)
  - estimated time remaining, using the observed throughput since the last
    time this viewer was run (falls back to the fetcher's designed pacing
    ceiling — 55 req/10min — if no prior sample exists)

Usage:
  python trader/bars1s_viewer.py              # print summary, (re)write HTML report
  python trader/bars1s_viewer.py --watch 60   # regenerate every 60s until Ctrl+C
  python trader/bars1s_viewer.py --no-html    # console only, skip writing the report
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from trader.bars1s_fetcher import (
    _SYMBOLS, _working_days, _PROGRESS_DB, _PACE_MAX_REQS, _PACE_WINDOW_S,
)

_TARGET_DAYS  = 252   # keep in sync with bars1s_fetcher's default --days
_OUTPUT_HTML  = _ROOT / "data" / "bars1s_viewer.html"
_RATE_CACHE   = _ROOT / "data" / "bars1s_rate_cache.json"
_STALE_S      = 12 * 60   # worst-case legit gap is one ~10min pacing sleep; past this, flag stalled

_SERIES_COLORS = {   # fixed categorical order per skill guidance — never cycled/reassigned
    "MES": {"light": "#2a78d6", "dark": "#3987e5"},   # slot 1 blue
    "MNQ": {"light": "#eb6834", "dark": "#d95926"},   # slot 2 orange
    "MYM": {"light": "#1baf7a", "dark": "#199e70"},   # slot 3 aqua
    "M2K": {"light": "#eda100", "dark": "#c98500"},   # slot 4 yellow
}


def _load_rows():
    uri = f"file:{_PROGRESS_DB.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    rows = conn.execute(
        "SELECT symbol, date, chunks_done, total_chunks, bars_fetched, finished, updated_at "
        "FROM progress"
    ).fetchall()
    conn.close()
    return rows


def build_stats():
    days = _working_days(_TARGET_DAYS)
    target_dates = {d.isoformat() for d in days}
    symbols = _SYMBOLS
    rows = _load_rows()

    by_symbol_month = defaultdict(lambda: defaultdict(int))
    by_symbol_year  = defaultdict(lambda: defaultdict(int))
    per_symbol_totals      = defaultdict(int)
    per_symbol_pairs_done  = defaultdict(int)
    chunks_done_total = 0
    bars_total        = 0
    pairs_finished    = 0
    latest_update     = None

    for sym, date_str, chunks_done, total_chunks, bars, finished, updated_at in rows:
        if updated_at and (latest_update is None or updated_at > latest_update):
            latest_update = updated_at
        if date_str not in target_dates or sym not in symbols:
            continue   # stray row outside the current rolling target window
        ym = date_str[:7]
        yy = date_str[:4]
        by_symbol_month[sym][ym] += bars or 0
        by_symbol_year[sym][yy]  += bars or 0
        per_symbol_totals[sym]   += bars or 0
        chunks_done_total        += chunks_done or 0
        bars_total               += bars or 0
        if finished:
            pairs_finished += 1
            per_symbol_pairs_done[sym] += 1

    pairs_total = len(days) * len(symbols)
    expected_total_chunks = pairs_total * 48

    months = sorted({ym for m in by_symbol_month.values() for ym in m})
    years  = sorted({yy for y in by_symbol_year.values() for yy in y})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "months": months,
        "years": years,
        "by_symbol_month": {s: dict(m) for s, m in by_symbol_month.items()},
        "by_symbol_year": {s: dict(m) for s, m in by_symbol_year.items()},
        "per_symbol_totals": dict(per_symbol_totals),
        "per_symbol_pairs_done": dict(per_symbol_pairs_done),
        "pairs_finished": pairs_finished,
        "pairs_total": pairs_total,
        "bars_total": bars_total,
        "chunks_done_total": chunks_done_total,
        "expected_total_chunks": expected_total_chunks,
        "latest_update": latest_update,
    }


def health(stats):
    if not stats["latest_update"]:
        return "unknown", None
    last = datetime.fromisoformat(stats["latest_update"])
    gap = (datetime.now(timezone.utc) - last).total_seconds()
    if stats["chunks_done_total"] >= stats["expected_total_chunks"]:
        return "complete", gap
    if gap > _STALE_S:
        return "stalled", gap
    return "running", gap


def eta(stats):
    """Estimate time remaining using observed throughput since the last viewer
    run (cached to disk), falling back to the fetcher's designed pacing ceiling."""
    remaining_chunks = max(0, stats["expected_total_chunks"] - stats["chunks_done_total"])
    theoretical_rate = _PACE_MAX_REQS / _PACE_WINDOW_S   # chunks/sec design ceiling

    observed_rate = None
    now = time.time()
    cache = {}
    if _RATE_CACHE.exists():
        try:
            cache = json.loads(_RATE_CACHE.read_text())
        except Exception:
            cache = {}
    if cache:
        dt = now - cache.get("t", 0)
        if 30 < dt < 6 * 3600:
            d_chunks = stats["chunks_done_total"] - cache.get("chunks_done_total", stats["chunks_done_total"])
            if d_chunks > 0:
                observed_rate = d_chunks / dt

    _RATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _RATE_CACHE.write_text(json.dumps({"t": now, "chunks_done_total": stats["chunks_done_total"]}))

    rate = observed_rate or theoretical_rate
    eta_seconds = remaining_chunks / rate if rate > 0 else None
    return {
        "remaining_chunks": remaining_chunks,
        "theoretical_rate_per_min": round(theoretical_rate * 60, 2),
        "observed_rate_per_min": round(observed_rate * 60, 2) if observed_rate else None,
        "eta_seconds": eta_seconds,
        "using": "observed" if observed_rate else "theoretical (no recent sample yet)",
    }


def _fmt_duration(seconds):
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _   = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if not d: parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


def print_console(stats, h_status, gap, e):
    pct = 100 * stats["chunks_done_total"] / stats["expected_total_chunks"] if stats["expected_total_chunks"] else 0
    print(f"\n=== bars1s status @ {stats['generated_at']} ===")
    print(f"Pairs   : {stats['pairs_finished']:,} / {stats['pairs_total']:,} finished")
    print(f"Bars    : {stats['bars_total']:,}")
    print(f"Chunks  : {stats['chunks_done_total']:,} / {stats['expected_total_chunks']:,}  ({pct:.1f}%)")
    gap_str = f"{gap/60:.1f} min ago" if gap is not None else "n/a"
    print(f"Health  : {h_status.upper()}  (last DB write {gap_str})")
    print(f"ETA     : {_fmt_duration(e['eta_seconds'])}  "
          f"[{e['using']}, {e['observed_rate_per_min'] or e['theoretical_rate_per_min']:.1f} chunks/min, "
          f"{e['remaining_chunks']:,} chunks remaining]")

    print("\nBars per symbol per month:")
    header = "Month".ljust(9) + "".join(s.rjust(14) for s in stats["symbols"]) + "Total".rjust(14)
    print(header)
    for ym in stats["months"]:
        row_total = 0
        cells = []
        for s in stats["symbols"]:
            v = stats["by_symbol_month"].get(s, {}).get(ym, 0)
            row_total += v
            cells.append(f"{v:,}".rjust(14))
        print(ym.ljust(9) + "".join(cells) + f"{row_total:,}".rjust(14))

    print("\nBars per symbol per year:")
    for yy in stats["years"]:
        cells = [f"{stats['by_symbol_year'].get(s, {}).get(yy, 0):,}" for s in stats["symbols"]]
        print(f"  {yy}: " + "  ".join(f"{s}={c}" for s, c in zip(stats["symbols"], cells)))

    print("\nPer-symbol totals:")
    for s in stats["symbols"]:
        done = stats["per_symbol_pairs_done"].get(s, 0)
        tot  = stats["pairs_total"] // len(stats["symbols"])
        print(f"  {s}: {stats['per_symbol_totals'].get(s, 0):>12,} bars   "
              f"({done}/{tot} days done)")
    print()


def render_html(stats, h_status, gap, e):
    pct = 100 * stats["chunks_done_total"] / stats["expected_total_chunks"] if stats["expected_total_chunks"] else 0
    months = stats["months"]
    symbols = stats["symbols"]
    max_month_val = max(
        (stats["by_symbol_month"].get(s, {}).get(ym, 0) for s in symbols for ym in months),
        default=1,
    ) or 1

    badge_class = {"running": "st-good", "complete": "st-good",
                   "stalled": "st-critical", "unknown": "st-warn"}.get(h_status, "st-warn")
    badge_label = {"running": "RUNNING", "complete": "COMPLETE",
                   "stalled": "STALLED", "unknown": "UNKNOWN"}.get(h_status, h_status.upper())

    legend_items = "".join(
        f'<span class="legend-item"><span class="swatch" style="background:var(--s-{s})"></span>{s}</span>'
        for s in symbols
    )

    # grouped bar chart, one group per month, one bar per symbol
    groups_html = []
    for ym in months:
        bars = []
        for s in symbols:
            v = stats["by_symbol_month"].get(s, {}).get(ym, 0)
            h_pct = round(100 * v / max_month_val, 1)
            bars.append(
                f'<div class="bar" style="height:{h_pct}%;background:var(--s-{s})" '
                f'title="{s} {ym}: {v:,} bars"></div>'
            )
        groups_html.append(
            f'<div class="group"><div class="bars">{"".join(bars)}</div>'
            f'<div class="month-label">{ym}</div></div>'
        )
    chart_html = "".join(groups_html)

    # progress bars per symbol
    per_symbol_total_days = stats["pairs_total"] // max(len(symbols), 1)
    progress_rows = []
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

    # data table (accessibility: table view alongside chart)
    thead = "<th>Month</th>" + "".join(f"<th>{s}</th>" for s in symbols) + "<th>Total</th>"
    trows = []
    for ym in months:
        cells = []
        row_total = 0
        for s in symbols:
            v = stats["by_symbol_month"].get(s, {}).get(ym, 0)
            row_total += v
            cells.append(f"<td>{v:,}</td>")
        trows.append(f"<tr><td>{ym}</td>{''.join(cells)}<td><strong>{row_total:,}</strong></td></tr>")
    table_html = f"<table><thead><tr>{thead}</tr></thead><tbody>{''.join(trows)}</tbody></table>"

    gap_str = f"{gap/60:.1f} min ago" if gap is not None else "n/a"
    rate_val = e["observed_rate_per_min"] or e["theoretical_rate_per_min"]
    eta_str = _fmt_duration(e["eta_seconds"])

    color_vars_light = "\n".join(f"  --s-{s}: {_SERIES_COLORS[s]['light']};" for s in symbols)
    color_vars_dark  = "\n".join(f"  --s-{s}: {_SERIES_COLORS[s]['dark']};" for s in symbols)

    html = f"""<!doctype html>
<title>bars1s fetcher — live status</title>
<meta http-equiv="refresh" content="60">
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
body {{
  margin: 0; padding: 24px; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
.sub {{ color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }}
.card {{
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 20px; margin-bottom: 16px;
}}
.hero {{ display: flex; flex-wrap: wrap; gap: 24px; align-items: center; }}
.stat {{ min-width: 140px; }}
.stat .num {{ font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.stat .label {{ font-size: 12px; color: var(--text-secondary); }}
.badge {{
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
  border-radius: 999px; font-size: 12px; font-weight: 600; color: white;
}}
.st-good {{ background: var(--st-good); }}
.st-warn {{ background: var(--st-warn); color: #2a1e00; }}
.st-critical {{ background: var(--st-critical); }}
.legend {{ display: flex; gap: 16px; margin-bottom: 10px; font-size: 12px; color: var(--text-secondary); }}
.legend-item {{ display: flex; align-items: center; gap: 5px; }}
.swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
.chart {{ display: flex; align-items: flex-end; gap: 14px; height: 200px; overflow-x: auto; padding-top: 8px; border-bottom: 1px solid var(--grid); }}
.group {{ display: flex; flex-direction: column; align-items: center; min-width: 56px; height: 100%; justify-content: flex-end; }}
.bars {{ display: flex; align-items: flex-end; gap: 3px; height: 100%; }}
.bar {{ width: 10px; border-radius: 3px 3px 0 0; min-height: 2px; }}
.month-label {{ font-size: 11px; color: var(--text-muted); margin-top: 6px; white-space: nowrap; }}
.prow {{ display: grid; grid-template-columns: 90px 1fr 220px; align-items: center; gap: 12px; margin: 10px 0; }}
.prow-label {{ display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; }}
.ptrack {{ background: var(--grid); border-radius: 6px; height: 10px; overflow: hidden; }}
.pfill {{ height: 100%; border-radius: 6px; }}
.prow-num {{ font-size: 12px; color: var(--text-secondary); text-align: right; font-variant-numeric: tabular-nums; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ color: var(--text-secondary); font-weight: 600; }}
.footer {{ font-size: 12px; color: var(--text-muted); margin-top: 8px; }}
</style>
<h1>bars1s_fetcher — live status</h1>
<div class="sub">Generated {stats['generated_at']} &middot; auto-refreshes every 60s while this tab stays open (file must be rewritten by <code>--watch</code> or a manual re-run)</div>

<div class="card hero">
  <div class="stat"><div class="num">{stats['bars_total']:,}</div><div class="label">bars collected</div></div>
  <div class="stat"><div class="num">{pct:.1f}%</div><div class="label">chunks complete ({stats['chunks_done_total']:,}/{stats['expected_total_chunks']:,})</div></div>
  <div class="stat"><div class="num">{stats['pairs_finished']:,}/{stats['pairs_total']:,}</div><div class="label">symbol-days finished</div></div>
  <div class="stat"><div class="num">{eta_str}</div><div class="label">estimated time remaining</div></div>
  <div class="stat">
    <span class="badge {badge_class}">{badge_label}</span>
    <div class="label" style="margin-top:6px">last DB write: {gap_str}</div>
  </div>
</div>

<div class="card">
  <div class="label" style="color:var(--text-secondary);font-size:12px;margin-bottom:6px">
    Throughput: {rate_val:.1f} chunks/min ({e['using']}) &middot; {e['remaining_chunks']:,} chunks remaining
  </div>
</div>

<div class="card">
  <div class="legend">{legend_items}</div>
  <div class="chart">{chart_html}</div>
</div>

<div class="card">
  <h2 style="font-size:14px;margin:0 0 12px">Per-symbol progress</h2>
  {progress_html}
</div>

<div class="card">
  <h2 style="font-size:14px;margin:0 0 12px">Bars per symbol per month (table view)</h2>
  {table_html}
</div>

<div class="footer">Progress DB: data/bars1s_progress.db &middot; refresh manually with <code>python trader/bars1s_viewer.py</code>, or keep it live with <code>python trader/bars1s_viewer.py --watch 60</code></div>
"""
    _OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_HTML.write_text(html, encoding="utf-8")


def run_once(no_html: bool):
    stats = build_stats()
    h_status, gap = health(stats)
    e = eta(stats)
    print_console(stats, h_status, gap, e)
    if not no_html:
        render_html(stats, h_status, gap, e)
        print(f"HTML report: {_OUTPUT_HTML}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="bars1s_fetcher live status viewer")
    parser.add_argument("--watch", type=int, metavar="SECONDS",
                        help="Regenerate every SECONDS until Ctrl+C (default: run once)")
    parser.add_argument("--no-html", action="store_true", help="Console output only")
    args = parser.parse_args()

    if args.watch:
        print(f"Watching every {args.watch}s — Ctrl+C to stop")
        try:
            while True:
                run_once(args.no_html)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once(args.no_html)
