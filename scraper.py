"""Scrape Reddit for stock ticker mentions and flag daily spikes."""
from __future__ import annotations

import argparse
import csv
import io
import re
import os
import ssl
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
DB_PATH = ROOT / "mentions.db"
TICKERS_CACHE = ROOT / "tickers.txt"
USER_AGENT = "reddit-stock-flagger/0.1 (by /u/anon)"

DEFAULT_SUBS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "StockMarket",
    "options",
    "pennystocks",
]

# Uppercase words that look like tickers but almost always aren't.
STOPWORD_TICKERS = {
    "A", "I", "AM", "PM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN",
    "IS", "IT", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "ALL", "AND", "ANY", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "GOT",
    "HAD", "HAS", "HER", "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NEW",
    "NOT", "NOW", "OFF", "ONE", "OUR", "OUT", "OWN", "PUT", "SAW", "SAY",
    "SEE", "SHE", "THE", "TOO", "TWO", "USE", "WAS", "WAY", "WHO", "WHY",
    "YES", "YET", "YOU", "EDIT", "TLDR", "DD", "OP", "USA", "CEO", "CFO",
    "IPO", "ATH", "ATL", "EPS", "ETF", "FED", "FOMC", "FOMO", "FUD", "GDP",
    "IMO", "IRA", "LOL", "NYSE", "OTM", "ITM", "PE", "PR", "PS", "PT", "RH",
    "ROI", "SEC", "SP", "TA", "WSB", "YOLO", "EOD", "EOW", "EOM", "EOY",
    "USD", "EUR", "CAD", "GBP", "JPY", "AUD", "CHF", "CNY",
    "AI", "API", "AR", "VR", "EV", "ICE", "OIL", "GAS",
    "CALL", "CALLS", "PUT", "PUTS", "BUY", "SELL", "HOLD", "MOON", "DUMP",
    "PUMP", "GAIN", "LOSS", "BULL", "BEAR", "LONG", "SHORT",
    "WILL", "JUST", "LIKE", "GOOD", "MAKE", "OVER", "SOME", "TIME", "THAN",
    "THEN", "WHEN", "WITH", "FROM", "INTO", "ONLY", "WHAT", "ALSO", "BEEN",
    "MUCH", "MORE", "MOST", "VERY", "WELL", "EVEN", "TAKE", "WANT", "NEED",
    "BACK", "REAL", "SURE", "NEXT", "LAST", "HIGH", "LOW", "BIG", "OLD",
}


def _build_ssl_context() -> ssl.SSLContext:
    # Python.org's macOS build ships without system CAs. Try common locations
    # before giving up. Last resort: unverified (data we fetch is public and
    # not security-sensitive — ticker lists and reddit json).
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/usr/local/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ssl.create_default_context(cafile=path)
            except Exception:
                continue
    try:
        return ssl.create_default_context()
    except Exception:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


_SSL_CTX = _build_ssl_context()


def http_get(url: str, retries: int = 3, sleep: float = 2.0) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


def load_ticker_whitelist() -> set[str]:
    """Cache and return the set of US-listed tickers from NASDAQ Trader."""
    if not TICKERS_CACHE.exists():
        print("Fetching ticker list from NASDAQ Trader...", file=sys.stderr)
        symbols: set[str] = set()
        for name in ("nasdaqlisted.txt", "otherlisted.txt"):
            data = http_get(f"https://www.nasdaqtrader.com/dynamic/SymDir/{name}").decode("utf-8", "replace")
            reader = csv.reader(io.StringIO(data), delimiter="|")
            header = next(reader, None)
            if not header:
                continue
            sym_idx = 0  # 'Symbol' or 'ACT Symbol' is always column 0
            for row in reader:
                if not row or row[0].startswith("File Creation Time"):
                    continue
                sym = row[sym_idx].strip().upper()
                # skip test issues, warrants, units, preferreds (contain dots)
                if not sym or not sym.isalpha() or len(sym) > 5:
                    continue
                symbols.add(sym)
        TICKERS_CACHE.write_text("\n".join(sorted(symbols)))
    return set(TICKERS_CACHE.read_text().split())


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mentions (
            day TEXT NOT NULL,
            subreddit TEXT NOT NULL,
            ticker TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (day, subreddit, ticker)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_ticker_day ON mentions(ticker, day)")
    conn.commit()
    return conn


CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
WORD_RE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str, whitelist: set[str]) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    # Cashtags: trust shape, still validate against whitelist to drop noise.
    for m in CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t in whitelist and t not in STOPWORD_TICKERS:
            found.append(t)
    # Bare uppercase words: whitelist-gated and stopword-filtered.
    for m in WORD_RE.finditer(text):
        t = m.group(1)
        if t in STOPWORD_TICKERS:
            continue
        if t in whitelist:
            found.append(t)
    return found


def fetch_subreddit_posts(sub: str, listing: str = "new", limit: int = 100) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/{listing}.json?limit={limit}"
    raw = http_get(url)
    import json
    data = json.loads(raw)
    return [c["data"] for c in data.get("data", {}).get("children", [])]


def utc_day(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).date().isoformat()


def scrape(subs: list[str], whitelist: set[str], conn: sqlite3.Connection) -> None:
    # (day, sub, ticker) -> count
    bucket: dict[tuple[str, str, str], int] = defaultdict(int)
    for sub in subs:
        try:
            posts = fetch_subreddit_posts(sub, "new", 100)
        except Exception as e:
            print(f"  ! {sub}: {e}", file=sys.stderr)
            continue
        print(f"  {sub}: {len(posts)} posts", file=sys.stderr)
        for p in posts:
            day = utc_day(p.get("created_utc", time.time()))
            text = f"{p.get('title','')}\n{p.get('selftext','')}"
            for t in extract_tickers(text, whitelist):
                bucket[(day, sub, t)] += 1
        time.sleep(1.5)  # be polite to reddit

    # Upsert: add to existing counts so multiple runs in a day accumulate
    # without double-counting the same post-day-ticker combo. We use MAX so
    # repeated runs of the same window don't inflate — counts are monotonic
    # within a day as the /new feed only adds posts.
    with conn:
        for (day, sub, ticker), count in bucket.items():
            conn.execute("""
                INSERT INTO mentions (day, subreddit, ticker, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, subreddit, ticker) DO UPDATE SET
                    count = MAX(count, excluded.count)
            """, (day, sub, ticker, count))


def report(conn: sqlite3.Connection, target_day: str, baseline_days: int = 7, top_n: int = 25) -> None:
    start = (date.fromisoformat(target_day) - timedelta(days=baseline_days)).isoformat()
    end_excl = target_day

    today_rows = conn.execute(
        "SELECT ticker, SUM(count) FROM mentions WHERE day = ? GROUP BY ticker",
        (target_day,),
    ).fetchall()
    today = {t: c for t, c in today_rows}

    base_rows = conn.execute("""
        SELECT ticker, SUM(count) * 1.0 / ? AS avg
        FROM mentions
        WHERE day >= ? AND day < ?
        GROUP BY ticker
    """, (baseline_days, start, end_excl)).fetchall()
    baseline = {t: a for t, a in base_rows}

    print(f"\n=== Reddit ticker mentions for {target_day} (UTC) ===\n")
    if not today:
        print("No mentions recorded for that day. Run with --scrape first.")
        return

    print(f"Top {top_n} by raw mentions:")
    print(f"  {'TICKER':<8} {'TODAY':>6} {'AVG':>7}")
    for ticker, c in sorted(today.items(), key=lambda x: -x[1])[:top_n]:
        avg = baseline.get(ticker, 0.0)
        print(f"  {ticker:<8} {c:>6} {avg:>7.1f}")

    # Spikes: ratio of today vs trailing average, requiring a floor on both.
    spikes = []
    for ticker, c in today.items():
        if c < 5:
            continue
        avg = baseline.get(ticker, 0.0)
        ratio = c / avg if avg >= 1.0 else (c if avg == 0 else c / avg)
        spikes.append((ticker, c, avg, ratio))
    spikes.sort(key=lambda x: -x[3])

    print(f"\nTop spikes vs {baseline_days}-day avg (min 5 mentions today):")
    print(f"  {'TICKER':<8} {'TODAY':>6} {'AVG':>7} {'RATIO':>7}")
    for ticker, c, avg, ratio in spikes[:top_n]:
        tag = "  NEW" if avg == 0 else ""
        print(f"  {ticker:<8} {c:>6} {avg:>7.1f} {ratio:>7.1f}x{tag}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scrape", action="store_true", help="Pull fresh data from Reddit")
    ap.add_argument("--report", action="store_true", help="Print today's report")
    ap.add_argument("--day", default=date.today().isoformat(), help="Report day (UTC, YYYY-MM-DD)")
    ap.add_argument("--baseline", type=int, default=7, help="Trailing baseline window in days")
    ap.add_argument("--top", type=int, default=25, help="How many rows per section")
    ap.add_argument("--subs", nargs="+", default=DEFAULT_SUBS, help="Subreddits to scrape")
    args = ap.parse_args()

    if not args.scrape and not args.report:
        args.scrape = args.report = True

    conn = init_db()
    if args.scrape:
        whitelist = load_ticker_whitelist()
        print(f"Loaded {len(whitelist)} tickers. Scraping {len(args.subs)} subs...", file=sys.stderr)
        scrape(args.subs, whitelist, conn)
    if args.report:
        report(conn, args.day, args.baseline, args.top)


if __name__ == "__main__":
    main()
