"""Backfill historical reddit ticker mentions via pullpush.io.

Pullpush is a public mirror of the old Pushshift archive. We use its text
search to pull only items that match a candidate query (e.g. the ticker
symbol), then run the same extraction + whitelist + stopword filter as the
live scraper, so noise is filtered out the same way.

Usage:
  python backfill.py --ticker MU --since 2024-11-01
  python backfill.py --ticker MU --since 2024-11-01 --until 2025-06-01
  python backfill.py --ticker MU --subs wallstreetbets stocks
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from scraper import (
    DEFAULT_SUBS,
    extract_tickers,
    filter_unseen,
    http_get,
    init_db,
    load_ticker_whitelist,
    utc_day,
)

PULLPUSH_BASE = "https://api.pullpush.io/reddit/search"
PAGE_SIZE = 100
SLEEP = 1.0  # seconds between requests; pullpush asks for politeness


def _to_epoch(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


def pullpush_iter(endpoint: str, params: dict) -> list[dict]:
    """Walk pullpush results backwards in time using `before` pagination.
    endpoint: 'submission' or 'comment'. Returns all matched items."""
    out: list[dict] = []
    cursor_before = params.get("before")
    page = 0
    while True:
        page += 1
        q = dict(params)
        q["size"] = PAGE_SIZE
        q["sort"] = "desc"
        q["sort_type"] = "created_utc"
        if cursor_before is not None:
            q["before"] = cursor_before
        url = f"{PULLPUSH_BASE}/{endpoint}/?{urlencode(q)}"
        try:
            raw = http_get(url)
        except Exception as e:
            print(f"  ! pullpush {endpoint} page {page}: {e}", file=sys.stderr)
            break
        try:
            data = json.loads(raw)
        except Exception as e:
            print(f"  ! pullpush {endpoint} page {page}: bad json: {e}", file=sys.stderr)
            break
        items = data.get("data", [])
        if not items:
            break
        out.extend(items)
        # Walk backwards: set `before` to one less than the oldest item's
        # created_utc on this page.
        oldest = min(i.get("created_utc", 0) for i in items)
        if oldest <= 0:
            break
        cursor_before = int(oldest)
        # If we got less than a full page, we're done.
        if len(items) < PAGE_SIZE:
            break
        # Stop if we walked past the requested `after`.
        if "after" in params and cursor_before <= int(params["after"]):
            break
        time.sleep(SLEEP)
        if page % 10 == 0:
            print(f"    {endpoint}: {len(out)} so far (page {page})", file=sys.stderr)
    return out


def backfill_ticker(ticker: str, subs: list[str], since: str, until: str) -> None:
    whitelist = load_ticker_whitelist()
    if ticker.upper() not in whitelist:
        print(f"Warning: {ticker} not in ticker whitelist", file=sys.stderr)
    conn = init_db()

    after = _to_epoch(since)
    before = _to_epoch(until)
    # Query: cashtag OR bare symbol. Pullpush q is loose substring/word match
    # depending on backend; extraction filters out false positives.
    query = f"${ticker} OR {ticker}"

    bucket: dict[tuple[str, str, str], int] = defaultdict(int)
    to_mark: list[tuple[str, str, float]] = []
    now = time.time()

    for sub in subs:
        for kind, endpoint in (("post", "submission"), ("comment", "comment")):
            print(f"r/{sub} {endpoint}s matching '{query}'...", file=sys.stderr)
            params = {
                "subreddit": sub,
                "q": query,
                "after": after,
                "before": before,
            }
            items = pullpush_iter(endpoint, params)
            print(f"  got {len(items)} {endpoint}s", file=sys.stderr)

            # Dedup against anything already in the DB.
            prefix = "t3_" if kind == "post" else "t1_"
            ids = [f"{prefix}{it['id']}" for it in items if it.get("id")]
            unseen = filter_unseen(conn, ids)

            for it in items:
                rid = it.get("id")
                if not rid:
                    continue
                full = f"{prefix}{rid}"
                if full not in unseen:
                    continue
                created = it.get("created_utc", now)
                if kind == "post":
                    text = f"{it.get('title','')}\n{it.get('selftext','') or ''}"
                else:
                    text = it.get("body", "") or ""
                day = utc_day(created)
                hits = extract_tickers(text, whitelist)
                for t in hits:
                    bucket[(day, sub, t)] += 1
                to_mark.append((full, kind, created))

    print(f"\nWriting {sum(bucket.values())} mentions from {len(to_mark)} items...", file=sys.stderr)
    with conn:
        for (day, sub, tk), count in bucket.items():
            conn.execute("""
                INSERT INTO mentions (day, subreddit, ticker, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, subreddit, ticker) DO UPDATE SET
                    count = mentions.count + excluded.count
            """, (day, sub, tk, count))
        conn.executemany(
            "INSERT OR IGNORE INTO seen_items (id, kind, created_utc, seen_at) VALUES (?, ?, ?, ?)",
            [(i, k, c, now) for (i, k, c) in to_mark],
        )
    print("Done.", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="Ticker symbol to backfill (e.g. MU)")
    ap.add_argument("--since", required=True, help="Start date UTC, YYYY-MM-DD")
    ap.add_argument("--until", default=date.today().isoformat(), help="End date UTC, YYYY-MM-DD")
    ap.add_argument("--subs", nargs="+", default=DEFAULT_SUBS, help="Subreddits to search")
    args = ap.parse_args()
    backfill_ticker(args.ticker.upper(), args.subs, args.since, args.until)


if __name__ == "__main__":
    main()
