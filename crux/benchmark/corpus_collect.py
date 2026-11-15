"""Collect candidate real resolution failures from GitHub issues.

Network step of the corpus (the only part that needs the internet).  It runs
GitHub's issue search and saves what it returns, verbatim, so every later
decision can be traced to the text the reporter wrote.

Sources (each a GitHub issue search; only the issue's opening post is used):
  pip   repo:pypa/pip ResolutionImpossible, any date
  uv    repo:astral-sh/uv "No solution found when resolving",
        2024-01-01 .. 2026-10-31
  gh    ResolutionImpossible in issues of any repository,
        2024-01-01 .. 2026-10-31, queried month by month (search returns at
        most 1,000 results per query)
  uvgh  uv's "No solution found when resolving" in issues outside uv's own
        tracker, 2024-01-01 .. 2026-10-31, month by month

The proposal names "the pip issue tracker and public Q&A threads".  pip's
tracker alone has too few reports since 2024 (23 when checked) and the
snapshot models CPython 3.12 (released October 2023), so the search is
widened to uv's tracker and to other projects' issues that quote pip's
error.  Stack Overflow was not reachable from the environment this was built
in.  Report both facts.

Output: one JSON object per issue (deduplicated by URL) with the query that
found it, the fetch time, and the title and body as returned.

    python -m crux.benchmark.corpus_collect --out RAW.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com/search/issues"
SNAPSHOT_END = "2026-10-31"
PAUSE_S = 2.2                       # search API: 30 requests a minute


def _months(start: str, end: str):
    d = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    while d <= stop:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield d.isoformat(), min(nxt - dt.timedelta(days=1), stop).isoformat()
        d = nxt


def queries():
    yield "pip", "repo:pypa/pip ResolutionImpossible is:issue"
    yield "uv", (f'repo:astral-sh/uv "No solution found when resolving" '
                 f"is:issue created:2024-01-01..{SNAPSHOT_END}")
    for a, b in _months("2024-01-01", SNAPSHOT_END):
        yield "gh", f"ResolutionImpossible is:issue created:{a}..{b}"
    for a, b in _months("2024-01-01", SNAPSHOT_END):
        yield "uvgh", ('"No solution found when resolving" is:issue '
                       f"-repo:astral-sh/uv created:{a}..{b}")


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "crux-benchmark (university project)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def search(query: str, log=print):
    """Yield every item of one search (up to GitHub's 1,000-result cap)."""
    total = None
    for page in range(1, 11):
        url = f"{API}?{urllib.parse.urlencode({'q': query, 'per_page': 100, 'page': page})}"
        data = _get(url)
        time.sleep(PAUSE_S)
        total = data["total_count"]
        if data.get("incomplete_results"):
            log(f"  warning: GitHub marked results incomplete for {query!r}")
        yield from data["items"]
        if page * 100 >= min(total, 1000):
            break
    if total and total > 1000:
        log(f"  warning: {total} results for {query!r}; only 1,000 reachable")


def collect(out: Path, log=print, sources=None, append=False) -> int:
    """Run the searches (only ``sources`` if given).  With ``append``, add to
    an existing file and skip issues already in it."""
    seen, n = set(), 0
    if append and out.exists():
        seen = {json.loads(l)["url"] for l in out.open(encoding="utf-8")}
    fetched_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a" if append else "w", encoding="utf-8") as f:
        for source, q in queries():
            if sources and source not in sources:
                continue
            k = 0
            for it in search(q, log):
                if "pull_request" in it:          # issues only
                    continue
                url = it["html_url"]
                if url in seen:
                    continue
                seen.add(url)
                body = it.get("body") or ""
                repo = it["repository_url"].split("/repos/", 1)[1]
                f.write(json.dumps({
                    "url": url, "repo": repo, "number": it["number"],
                    "source": source, "query": q, "fetched_at": fetched_at,
                    "created_at": it["created_at"], "state": it["state"],
                    "title": it["title"], "body": body,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                }, sort_keys=True) + "\n")
                k += 1
                n += 1
            log(f"{source:3s} {k:4d} new  {q}")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="collect candidate issues")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", help="comma-separated subset of pip,uv,gh,uvgh")
    ap.add_argument("--append", action="store_true",
                    help="add to an existing file, skipping issues already in it")
    a = ap.parse_args(argv)
    n = collect(Path(a.out), log=lambda s: print(s, flush=True),
                sources=a.sources.split(",") if a.sources else None,
                append=a.append)
    print(f"saved {n} issues to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
