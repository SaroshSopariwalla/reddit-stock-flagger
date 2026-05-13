#!/bin/bash
# Wrapper invoked by launchd. Scrapes, regenerates report, pushes to GitHub.
# All output goes to scrape.log so failures are debuggable.
set -u

cd "$(dirname "$0")" || exit 1

# launchd serializes StartInterval runs of the same agent automatically,
# so no explicit lock needed.

# launchd strips most of the user env. Ensure git/python/etc are findable.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$(command -v python3)"

{
    echo
    echo "=== run started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

    "$PY" scraper.py --scrape || {
        echo "scrape failed with exit $?"
        exit 0  # don't fail the launchd job; try again next interval
    }

    {
        echo "# Reddit Ticker Mentions"
        echo
        echo "_Last updated: $(date -u +%Y-%m-%dT%H:%MZ) (laptop)_"
        echo
        echo "**[Open the live dashboard](https://saroshsopariwalla.github.io/reddit-stock-flagger/)**"
        echo
        echo '```'
        "$PY" scraper.py --report --top 25
        echo '```'
    } > REPORT.md

    "$PY" dashboard.py || echo "dashboard generation failed"

    # Pull first in case GitHub has changes we don't (e.g. you edited code
    # from the web UI). Rebase keeps history linear.
    git pull --rebase --autostash >/dev/null 2>&1 || true

    git add mentions.db tickers.txt REPORT.md docs/index.html docs/data.json
    if git diff --cached --quiet; then
        echo "no changes to commit"
    else
        git commit -m "data: scrape $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]" >/dev/null
        git push >/dev/null 2>&1 || echo "push failed (will retry next run)"
    fi
    echo "=== run finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> scrape.log 2>&1
