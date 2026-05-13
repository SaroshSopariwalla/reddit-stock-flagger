"""Generate a static HTML dashboard from mentions.db.

Outputs docs/index.html (HTML with embedded Chart.js) and docs/data.json
(the data the page renders). Designed to be served via GitHub Pages
(configure repo: Settings -> Pages -> Source: main branch, /docs folder).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DOCS = Path(__file__).parent / "docs"


def build_data(conn: sqlite3.Connection, target_day: str | None = None) -> dict:
    today = target_day or date.today().isoformat()
    today_dt = date.fromisoformat(today)
    window_start = (today_dt - timedelta(days=13)).isoformat()
    baseline_start = (today_dt - timedelta(days=7)).isoformat()

    # Top mentions today.
    top_today = [
        {"ticker": t, "count": c}
        for t, c in conn.execute(
            "SELECT ticker, SUM(count) FROM mentions WHERE day = ? "
            "GROUP BY ticker ORDER BY 2 DESC LIMIT 25",
            (today,),
        ).fetchall()
    ]

    # Spikes: today vs 7-day trailing avg.
    baseline = dict(conn.execute(
        "SELECT ticker, SUM(count) * 1.0 / 7 FROM mentions "
        "WHERE day >= ? AND day < ? GROUP BY ticker",
        (baseline_start, today),
    ).fetchall())
    spikes = []
    for row in conn.execute(
        "SELECT ticker, SUM(count) FROM mentions WHERE day = ? GROUP BY ticker",
        (today,),
    ).fetchall():
        ticker, c = row
        if c < 5:
            continue
        avg = baseline.get(ticker, 0.0)
        ratio = c / avg if avg >= 1.0 else float(c)
        spikes.append({
            "ticker": ticker,
            "today": c,
            "avg": round(avg, 2),
            "ratio": round(ratio, 2),
            "is_new": avg == 0,
        })
    spikes.sort(key=lambda x: -x["ratio"])
    spikes = spikes[:15]

    # Trendlines: today's top 10 tickers. Show up to last 30 days; will
    # fill in over time as the scraper builds history.
    TREND_WINDOW = 30
    top_for_trend = [t["ticker"] for t in top_today[:10]]
    trend_start = (today_dt - timedelta(days=TREND_WINDOW - 1)).isoformat()
    day_axis = [(today_dt - timedelta(days=TREND_WINDOW - 1 - d)).isoformat()
                for d in range(TREND_WINDOW)]
    trends = {}
    for tk in top_for_trend:
        row_map = dict(conn.execute(
            "SELECT day, SUM(count) FROM mentions "
            "WHERE ticker = ? AND day >= ? GROUP BY day",
            (tk, trend_start),
        ).fetchall())
        trends[tk] = [row_map.get(d, 0) for d in day_axis]

    # Mentions today by subreddit.
    by_sub = [
        {"subreddit": s, "count": c}
        for s, c in conn.execute(
            "SELECT subreddit, SUM(count) FROM mentions WHERE day = ? "
            "GROUP BY subreddit ORDER BY 2 DESC",
            (today,),
        ).fetchall()
    ]

    # Stats for the header.
    item_count = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
    distinct_tickers_today = len(top_today)
    total_mentions_today = sum(t["count"] for t in top_today)

    return {
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "today": today,
        "stats": {
            "items_seen_total": item_count,
            "distinct_tickers_today": distinct_tickers_today,
            "mentions_today": total_mentions_today,
        },
        "top_today": top_today,
        "spikes": spikes,
        "trends": {
            "days": day_axis,
            "tickers": top_for_trend,
            "series": trends,
        },
        "by_subreddit": by_sub,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Reddit Ticker Pulse</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0d1117;
  --panel: #161b22;
  --panel-border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --accent: #58a6ff;
  --accent-2: #f78166;
  --good: #3fb950;
  --bad: #f85149;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  padding: 24px;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 24px;
  flex-wrap: wrap;
  gap: 12px;
}
h1 { font-size: 22px; margin: 0; font-weight: 600; }
h1 .accent { color: var(--accent); }
.meta { color: var(--text-dim); font-size: 12px; }
.stats {
  display: flex;
  gap: 16px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.stat {
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  padding: 12px 16px;
  min-width: 140px;
}
.stat .label { color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.stat .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 16px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  padding: 16px;
}
.card h2 { font-size: 14px; margin: 0 0 12px 0; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.chart-wrap { position: relative; height: 360px; }
.chart-wrap-tall { position: relative; height: 480px; }
.spike-list { display: flex; flex-direction: column; gap: 8px; }
.spike-row {
  display: grid;
  grid-template-columns: 60px 1fr 60px 70px;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--panel-border);
}
.spike-row:last-child { border-bottom: none; }
.spike-row .tk { font-weight: 600; color: var(--accent); }
.spike-row .bar { background: #21262d; height: 8px; border-radius: 4px; overflow: hidden; }
.spike-row .bar-fill { background: var(--accent-2); height: 100%; }
.spike-row .today-n { text-align: right; font-variant-numeric: tabular-nums; }
.spike-row .ratio { text-align: right; color: var(--good); font-variant-numeric: tabular-nums; font-weight: 600; }
.spike-row .ratio.new { color: var(--accent); }
footer { color: var(--text-dim); font-size: 11px; margin-top: 24px; text-align: center; }
footer a { color: var(--text-dim); }
</style>
</head>
<body>
<header>
  <div>
    <h1>Reddit <span class="accent">Ticker</span> Pulse</h1>
    <div class="meta" id="meta"></div>
  </div>
</header>

<div class="stats" id="stats"></div>

<div class="grid">
  <div class="card">
    <h2>Top spikes vs 7-day avg</h2>
    <div class="spike-list" id="spikes"></div>
  </div>
  <div class="card">
    <h2>Top mentions today</h2>
    <div class="chart-wrap-tall"><canvas id="topChart"></canvas></div>
  </div>
  <div class="card" style="grid-column: 1 / -1;">
    <h2>Daily history (last 30 days): today's top 10 tickers</h2>
    <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
  </div>
  <div class="card">
    <h2>Mentions by subreddit (today)</h2>
    <div class="chart-wrap"><canvas id="subChart"></canvas></div>
  </div>
</div>

<footer>
  Source: reddit /new, /hot, /rising + top-15 hot post comments across 10 finance subs &middot;
  <a href="https://github.com/SaroshSopariwalla/reddit-stock-flagger">repo</a>
</footer>

<script>
const PALETTE = ["#58a6ff", "#f78166", "#3fb950", "#d2a8ff", "#ffa657", "#79c0ff", "#ff7b72", "#a5d6ff", "#56d364", "#ffab70"];
Chart.defaults.color = "#8b949e";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

async function main() {
  const data = await (await fetch("data.json?t=" + Date.now())).json();

  document.getElementById("meta").textContent =
    `Reporting day: ${data.today} (UTC) &middot; Generated ${data.generated_at}`.replace(/&middot;/g, "·");

  const statsEl = document.getElementById("stats");
  const stats = [
    ["Mentions today", data.stats.mentions_today.toLocaleString()],
    ["Distinct tickers today", data.stats.distinct_tickers_today.toLocaleString()],
    ["Items processed (all-time)", data.stats.items_seen_total.toLocaleString()],
  ];
  for (const [label, value] of stats) {
    const el = document.createElement("div");
    el.className = "stat";
    el.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
    statsEl.appendChild(el);
  }

  // Spike list
  const spikesEl = document.getElementById("spikes");
  const maxRatio = data.spikes.length ? Math.max(...data.spikes.map(s => Math.min(s.ratio, 50))) : 1;
  if (!data.spikes.length) {
    spikesEl.innerHTML = '<div style="color: var(--text-dim); padding: 12px;">No spikes yet — need more baseline data.</div>';
  }
  for (const s of data.spikes) {
    const ratioCapped = Math.min(s.ratio, 50);
    const barPct = Math.max(2, (ratioCapped / maxRatio) * 100);
    const ratioLabel = s.is_new ? `NEW` : `${s.ratio.toFixed(1)}×`;
    const row = document.createElement("div");
    row.className = "spike-row";
    row.innerHTML = `
      <div class="tk">${s.ticker}</div>
      <div class="bar"><div class="bar-fill" style="width: ${barPct}%"></div></div>
      <div class="today-n">${s.today}</div>
      <div class="ratio ${s.is_new ? "new" : ""}">${ratioLabel}</div>
    `;
    spikesEl.appendChild(row);
  }

  // Top today bar chart
  new Chart(document.getElementById("topChart"), {
    type: "bar",
    data: {
      labels: data.top_today.map(t => t.ticker),
      datasets: [{
        data: data.top_today.map(t => t.count),
        backgroundColor: "#58a6ff",
        borderRadius: 3,
      }]
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: "#30363d" } },
        y: { grid: { display: false } },
      }
    }
  });

  // Trend lines
  const trendDatasets = data.trends.tickers.map((tk, i) => ({
    label: tk,
    data: data.trends.series[tk],
    borderColor: PALETTE[i % PALETTE.length],
    backgroundColor: PALETTE[i % PALETTE.length] + "20",
    tension: 0.3,
    borderWidth: 2,
    pointRadius: 2,
  }));
  new Chart(document.getElementById("trendChart"), {
    type: "line",
    data: { labels: data.trends.days, datasets: trendDatasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "top", labels: { boxWidth: 12 } } },
      scales: {
        x: { grid: { color: "#21262d" } },
        y: { grid: { color: "#30363d" }, beginAtZero: true },
      }
    }
  });

  // Subreddit doughnut
  new Chart(document.getElementById("subChart"), {
    type: "doughnut",
    data: {
      labels: data.by_subreddit.map(s => "r/" + s.subreddit),
      datasets: [{
        data: data.by_subreddit.map(s => s.count),
        backgroundColor: PALETTE,
        borderColor: "#161b22",
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "right", labels: { boxWidth: 12 } } },
    }
  });
}
main();
</script>
</body>
</html>
"""


def generate(target_day: str | None = None) -> None:
    from scraper import init_db
    DOCS.mkdir(exist_ok=True)
    conn = init_db()
    data = build_data(conn, target_day)
    (DOCS / "data.json").write_text(json.dumps(data, indent=2))
    (DOCS / "index.html").write_text(HTML_TEMPLATE)
    print(f"Wrote {DOCS}/index.html and {DOCS}/data.json")


if __name__ == "__main__":
    generate()
