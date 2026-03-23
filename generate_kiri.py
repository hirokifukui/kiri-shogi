#!/usr/bin/env python3
"""generate_kiri.py — AI対戦ビューア HTML生成スクリプト

JSONLログから対局一覧ページ + 対局詳細ページを生成する。
再解析は行わず、対局中のスコアをそのまま表示する。
"""

import argparse
import glob
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIVERGENCE_THRESHOLD = 500

ENGINE_COLORS = {
    "水匠5": "#6aacdc",
    "tanuki": "#dc8a6a",
    "Kristallweizen": "#8adc9a",
    "dlshogi": "#c88adc",
}

ENGINE_ORDER = ["水匠5", "tanuki", "Kristallweizen", "dlshogi"]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_games(log_dir: str) -> dict[str, list[dict]]:
    """Load all positions_v0_*.jsonl files, grouped by game_id."""
    games: dict[str, list[dict]] = defaultdict(list)
    pattern = os.path.join(log_dir, "positions_v0_*.jsonl")
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                games[rec["game_id"]].append(rec)
    # Sort each game by move_number
    for gid in games:
        games[gid].sort(key=lambda r: r["move_number"])
    return dict(games)


def extract_date(game_id: str) -> str:
    """Extract date string from game_id like 'ensemble_max_d12_20260322_203524_game1'."""
    m = re.search(r"(\d{8})_(\d{6})", game_id)
    if m:
        d = m.group(1)
        t = m.group(2)
        return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}"
    return ""


def extract_params(records: list[dict]) -> dict:
    """Extract strategy/depth from first record."""
    r = records[0]
    return {
        "strategy": r.get("strategy", "?"),
        "depth": r.get("depth", "?"),
        "game_result": r.get("game_result", "?"),
    }


# ---------------------------------------------------------------------------
# HTML: shared parts
# ---------------------------------------------------------------------------

CSS_INLINE = """\
:root {
  --bg: #111010;
  --bg-card: #1a1918;
  --bg-card-hover: #222020;
  --border: rgba(255,255,255,0.07);
  --text-primary: #e8e4dc;
  --text-secondary: #9a9490;
  --text-muted: #5a5652;
  --accent-gold: #c8a96e;
  --accent-gold-dim: rgba(200,169,110,0.15);
  --engine-mizusho: #6aacdc;
  --engine-tanuki: #dc8a6a;
  --engine-kristall: #8adc9a;
  --engine-dlshogi: #c88adc;
  --font-serif-en: 'Cormorant Garamond', 'EB Garamond', Georgia, serif;
  --font-serif-jp: 'Yu Mincho', 'YuMincho', '游明朝', serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }

body {
  background: var(--bg);
  color: var(--text-primary);
  font-family: var(--font-serif-jp);
  font-size: 16px;
  line-height: 1.8;
  -webkit-font-smoothing: antialiased;
}

a { color: var(--accent-gold); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ---- NAV ---- */
.topnav {
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 2rem; height: 56px;
  background: rgba(17,16,16,0.92);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
}
.topnav-logo {
  font-family: var(--font-serif-en);
  font-size: 1.3rem; font-weight: 300;
  letter-spacing: 0.1em;
  color: var(--accent-gold);
  text-decoration: none;
}
.topnav-logo:hover { text-decoration: none; }
.topnav-sub {
  font-size: 0.72rem; color: var(--text-muted);
  letter-spacing: 0.12em;
}
.breadcrumb {
  font-family: var(--font-mono);
  font-size: 0.7rem; color: var(--text-secondary);
  letter-spacing: 0.06em;
}
.breadcrumb a { color: var(--text-secondary); }
.breadcrumb a:hover { color: var(--accent-gold); }

/* ---- CONTAINER ---- */
.container {
  max-width: 960px;
  margin: 0 auto;
  padding: 2rem 1.5rem 4rem;
}

/* ---- PAGE HEADER ---- */
.page-header {
  margin-bottom: 2rem;
  padding-bottom: 1.5rem;
  border-bottom: 1px solid var(--border);
}
.page-header h1 {
  font-family: var(--font-serif-en);
  font-size: clamp(1.4rem, 3vw, 2rem);
  font-weight: 300;
  letter-spacing: 0.05em;
  margin-bottom: 0.3rem;
}
.page-header .subtitle {
  font-family: var(--font-mono);
  font-size: 0.72rem;
  color: var(--text-muted);
  letter-spacing: 0.15em;
}

/* ---- SUMMARY CARDS ---- */
.summary-row {
  display: flex; flex-wrap: wrap; gap: 0.8rem;
  margin-bottom: 2rem;
}
.summary-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  padding: 0.8rem 1.2rem;
  flex: 1 1 120px;
  min-width: 100px;
}
.summary-card .label {
  font-family: var(--font-mono);
  font-size: 0.6rem;
  color: var(--text-muted);
  letter-spacing: 0.2em;
  text-transform: uppercase;
  margin-bottom: 0.2rem;
}
.summary-card .value {
  font-family: var(--font-mono);
  font-size: 1.1rem;
  color: var(--text-primary);
}
.summary-card .value.win { color: #6adc8a; }
.summary-card .value.loss { color: #dc6a6a; }
.summary-card .value.draw { color: var(--text-secondary); }

/* ---- SECTION ---- */
.section-label {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  letter-spacing: 0.25em;
  color: var(--accent-gold);
  text-transform: uppercase;
  margin-bottom: 0.6rem;
}

/* ---- TABLE ---- */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  margin-bottom: 2rem;
}
.data-table th {
  text-align: left;
  font-size: 0.62rem;
  letter-spacing: 0.18em;
  color: var(--text-muted);
  text-transform: uppercase;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
.data-table td {
  padding: 0.45rem 0.6rem;
  border-bottom: 1px solid var(--border);
  color: var(--text-secondary);
  vertical-align: top;
}
.data-table tr:hover td {
  background: var(--bg-card-hover);
}
.data-table .num { text-align: right; }

/* Game list specific */
.game-link { color: var(--text-primary); }
.game-link:hover { color: var(--accent-gold); }

/* ---- DIVERGENCE HIGHLIGHT ---- */
.row-divergent td {
  background: rgba(220,106,106,0.08);
}
.row-divergent:hover td {
  background: rgba(220,106,106,0.14);
}
.div-high {
  color: #dc6a6a;
  font-weight: 600;
}

/* ---- ENGINE DETAIL (expandable) ---- */
.engine-detail {
  margin-top: 0.5rem;
  padding: 0.5rem 0.8rem;
  background: rgba(255,255,255,0.02);
  border: 1px solid var(--border);
  font-size: 0.72rem;
}
.engine-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.2rem 0;
}
.engine-name {
  width: 90px;
  flex-shrink: 0;
  font-size: 0.65rem;
  letter-spacing: 0.08em;
}
.engine-score {
  width: 60px;
  text-align: right;
  flex-shrink: 0;
}
.engine-move {
  color: var(--text-secondary);
  font-size: 0.68rem;
}

/* ---- CLICKABLE ROW ---- */
.clickable { cursor: pointer; }

/* ---- RESPONSIVE ---- */
@media (max-width: 640px) {
  .topnav { padding: 0 1rem; }
  .container { padding: 1.2rem 0.8rem 3rem; }
  .summary-card { flex: 1 1 100%; }
  .data-table { font-size: 0.7rem; }
  .data-table th, .data-table td { padding: 0.35rem 0.4rem; }
  .engine-detail { font-size: 0.65rem; }
  .engine-name { width: 70px; }
}
"""


def html_head(title: str, extra_css: str = "") -> str:
    return f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300&family=JetBrains+Mono:wght@300;400&display=swap" rel="stylesheet">
  <style>
{CSS_INLINE}
{extra_css}
  </style>
</head>
"""


def topnav(breadcrumbs: list[tuple[str, str]] | None = None) -> str:
    bc = ""
    if breadcrumbs:
        parts = []
        for label, href in breadcrumbs:
            if href:
                parts.append(f'<a href="{href}">{escape(label)}</a>')
            else:
                parts.append(escape(label))
        bc = f'<span class="breadcrumb">{" / ".join(parts)}</span>'
    return f"""\
<nav class="topnav">
  <div>
    <a href="index.html" class="topnav-logo">Kiri</a>
    <span class="topnav-sub">AI対戦ビューア</span>
  </div>
  {bc}
</nav>
"""


# ---------------------------------------------------------------------------
# HTML: index page
# ---------------------------------------------------------------------------

def generate_index(games: dict[str, list[dict]], out_dir: str, threshold: int = DIVERGENCE_THRESHOLD) -> None:
    """Generate kiri_games/index.html."""

    rows = []
    for gid, records in games.items():
        params = extract_params(records)
        date_str = extract_date(gid)
        total_moves = max(r["move_number"] for r in records)
        div_count = sum(1 for r in records if r.get("divergence", 0) >= threshold)
        max_div = max((r.get("divergence", 0) for r in records), default=0)
        rows.append({
            "game_id": gid,
            "date": date_str,
            "total_moves": total_moves,
            "result": params["game_result"],
            "strategy": params["strategy"],
            "depth": params["depth"],
            "div_count": div_count,
            "max_div": max_div,
        })

    # Sort by divergence count descending, then max divergence descending
    rows.sort(key=lambda r: (-r["div_count"], -r["max_div"]))

    table_rows = []
    for r in rows:
        result_cls = {"win": "win", "loss": "loss", "draw": "draw"}.get(r["result"], "")
        result_label = {"win": "勝ち", "loss": "負け", "draw": "引分"}.get(r["result"], r["result"])
        table_rows.append(f"""\
    <tr>
      <td><a class="game-link" href="{r['game_id']}.html">{escape(r['game_id'])}</a></td>
      <td>{escape(r['date'])}</td>
      <td class="num">{r['total_moves']}</td>
      <td class="{result_cls}">{result_label}</td>
      <td class="num">{r['div_count']}</td>
      <td class="num">{r['max_div']}</td>
    </tr>""")

    html = html_head("Kiri — AI対戦一覧")
    html += "<body>\n"
    html += topnav([("Kiri", "../index.html"), ("AI対戦", "")])
    html += '<div class="container">\n'
    html += """\
<div class="page-header">
  <h1>AI Battle Viewer</h1>
  <p class="subtitle">AI対戦ログ — 乖離局面の多い順</p>
</div>
"""
    html += f"""\
<table class="data-table">
  <thead>
    <tr>
      <th>Game ID</th>
      <th>Date</th>
      <th class="num">Moves</th>
      <th>Result</th>
      <th class="num">乖離局面</th>
      <th class="num">最大乖離</th>
    </tr>
  </thead>
  <tbody>
{"".join(table_rows)}
  </tbody>
</table>
"""
    html += "</div>\n</body>\n</html>\n"

    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {index_path}")


# ---------------------------------------------------------------------------
# HTML: game detail page
# ---------------------------------------------------------------------------

def format_score(score: int | None) -> str:
    if score is None:
        return "—"
    if score >= 0:
        return f"+{score}"
    return str(score)


def generate_game_page(game_id: str, records: list[dict], out_dir: str, threshold: int = DIVERGENCE_THRESHOLD) -> None:
    """Generate kiri_games/{game_id}.html."""

    params = extract_params(records)
    date_str = extract_date(game_id)
    total_moves = max(r["move_number"] for r in records)
    div_positions = [r for r in records if r.get("divergence", 0) >= threshold]
    div_count = len(div_positions)

    result_cls = {"win": "win", "loss": "loss", "draw": "draw"}.get(params["game_result"], "")
    result_label = {"win": "勝ち", "loss": "負け", "draw": "引分"}.get(params["game_result"], params["game_result"])

    # --- Build divergence highlight table ---
    div_rows = []
    for r in div_positions:
        div_rows.append(f"""\
    <tr class="clickable" onclick="document.getElementById('move-{r['move_number']}').scrollIntoView({{behavior:'smooth',block:'center'}})">
      <td class="num">{r['move_number']}</td>
      <td class="num div-high">{r['divergence']}</td>
      <td>{escape(r.get('selected_move', ''))}</td>
    </tr>""")

    # --- Build full move list ---
    move_rows = []
    for r in records:
        is_div = r.get("divergence", 0) >= threshold
        row_cls = ' class="row-divergent"' if is_div else ""
        div_cell = f'<span class="div-high">{r["divergence"]}</span>' if is_div else str(r.get("divergence", 0))

        engine_detail = ""
        if is_div:
            engine_lines = []
            for eng in ENGINE_ORDER:
                er = r.get("engine_results", {}).get(eng)
                if er:
                    color = ENGINE_COLORS.get(eng, "#9a9490")
                    score_str = format_score(er.get("score"))
                    move_str = er.get("best_move", "—")
                    sd = er.get("seldepth")
                    sd_str = f" (d={sd})" if sd is not None else ""
                    engine_lines.append(
                        f'<div class="engine-row">'
                        f'<span class="engine-name" style="color:{color}">{escape(eng)}</span>'
                        f'<span class="engine-score" style="color:{color}">{score_str}</span>'
                        f'<span class="engine-move">{escape(move_str)}{sd_str}</span>'
                        f'</div>'
                    )
            engine_detail = f'\n      <tr{row_cls}><td colspan="5"><div class="engine-detail">{"".join(engine_lines)}</div></td></tr>'

        move_rows.append(f"""\
    <tr id="move-{r['move_number']}"{row_cls}>
      <td class="num">{r['move_number']}</td>
      <td>{escape(r.get('selected_move', ''))}</td>
      <td class="num">{format_score(r.get('nnue_mean'))}</td>
      <td class="num">{format_score(r.get('dl_score'))}</td>
      <td class="num">{div_cell}</td>
    </tr>{engine_detail}""")

    extra_css = """\
.highlight-section {
  margin-bottom: 2.5rem;
  padding: 1.2rem;
  background: var(--bg-card);
  border: 1px solid var(--border);
}
.highlight-section h3 {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  letter-spacing: 0.2em;
  color: var(--accent-gold);
  text-transform: uppercase;
  margin-bottom: 0.8rem;
}
"""

    html = html_head(f"Kiri — {game_id}", extra_css)
    html += "<body>\n"
    html += topnav([("Kiri", "../index.html"), ("AI対戦", "index.html"), (game_id, "")])
    html += '<div class="container">\n'

    # Page header
    html += f"""\
<div class="page-header">
  <h1>{escape(game_id)}</h1>
  <p class="subtitle">{escape(date_str)}</p>
</div>
"""

    # Summary cards
    html += f"""\
<div class="summary-row">
  <div class="summary-card">
    <div class="label">RESULT</div>
    <div class="value {result_cls}">{result_label}</div>
  </div>
  <div class="summary-card">
    <div class="label">MOVES</div>
    <div class="value">{total_moves}</div>
  </div>
  <div class="summary-card">
    <div class="label">STRATEGY</div>
    <div class="value">{escape(str(params['strategy']))}</div>
  </div>
  <div class="summary-card">
    <div class="label">DEPTH</div>
    <div class="value">{params['depth']}</div>
  </div>
  <div class="summary-card">
    <div class="label">乖離局面</div>
    <div class="value div-high">{div_count}</div>
  </div>
</div>
"""

    # Divergence highlight section
    if div_rows:
        html += f"""\
<div class="highlight-section">
  <h3>Divergence Highlights (≥ {threshold}cp)</h3>
  <table class="data-table">
    <thead>
      <tr>
        <th class="num">手数</th>
        <th class="num">乖離 (cp)</th>
        <th>採用手</th>
      </tr>
    </thead>
    <tbody>
{"".join(div_rows)}
    </tbody>
  </table>
</div>
"""

    # Full move list
    html += f"""\
<p class="section-label">全局面</p>
<table class="data-table">
  <thead>
    <tr>
      <th class="num">手数</th>
      <th>採用手</th>
      <th class="num">NNUE平均</th>
      <th class="num">DL Score</th>
      <th class="num">乖離</th>
    </tr>
  </thead>
  <tbody>
{"".join(move_rows)}
  </tbody>
</table>
"""

    html += "</div>\n</body>\n</html>\n"

    page_path = os.path.join(out_dir, f"{game_id}.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {page_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AI対戦ビューア HTML生成")
    parser.add_argument(
        "--log-dir",
        default=os.path.join(os.path.dirname(__file__), "..", "logs"),
        help="JSONLログディレクトリ (default: ../logs)",
    )
    parser.add_argument(
        "--out-dir",
        default=os.path.join(os.path.dirname(__file__), "docs", "kiri_games"),
        help="出力ディレクトリ (default: docs/kiri_games/)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DIVERGENCE_THRESHOLD,
        help=f"乖離閾値 (default: {DIVERGENCE_THRESHOLD})",
    )
    args = parser.parse_args()

    threshold = args.threshold

    log_dir = os.path.abspath(args.log_dir)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Loading games from {log_dir}")
    games = load_games(log_dir)
    print(f"Found {len(games)} game(s)")

    if not games:
        print("No games found. Exiting.")
        return

    print("Generating index...")
    generate_index(games, out_dir, threshold)

    print("Generating game pages...")
    for gid, records in games.items():
        generate_game_page(gid, records, out_dir, threshold)

    print(f"\nDone! Output in {out_dir}")


if __name__ == "__main__":
    main()
