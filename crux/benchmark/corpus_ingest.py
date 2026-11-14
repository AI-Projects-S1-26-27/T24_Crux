"""Download the PyPI data the real-failure corpus needs (network step).

Uses the data layer unchanged: ``crux.data.run_ingest.run`` with the corpus's
requested packages as seeds (it follows their whole dependency closure) and
``crux.data.clean.clean``, so the same filters and snapshot cutoff apply as
for the team's dataset.  It writes to its own database, never data/crux.db.

    python -m crux.benchmark.corpus_ingest --cases CASES.jsonl --db corpus.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from crux.data import store
from crux.data.clean import clean
from crux.data.run_ingest import run


def seed_names(cases) -> list[str]:
    names = set()
    for c in cases:
        if c.get("status", "kept") != "kept":
            continue
        for r in c["requirements"]:
            names.add(canonicalize_name(Requirement(r).name))
    return sorted(names)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    cases = [json.loads(l) for l in open(a.cases)]
    names = seed_names(cases)
    print(f"{len(names)} requested packages; ingesting their closure", flush=True)
    conn = store.open_db(a.db)
    t0 = time.perf_counter()
    res = asyncio.run(run(conn=conn, _fetch_top_list=lambda: names))
    print(f"ingest {res} in {time.perf_counter() - t0:.0f}s", flush=True)
    t0 = time.perf_counter()
    drops = clean(conn)
    print(f"clean in {time.perf_counter() - t0:.0f}s; drops {drops}", flush=True)
    for t in ("projects", "releases", "deps"):
        print(t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
