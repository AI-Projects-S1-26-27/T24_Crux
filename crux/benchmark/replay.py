"""Replay screened real cases on the snapshot as it stood on the report date.

For each kept case: take the corpus database (``corpus_ingest``), keep only
releases uploaded on or before the issue's creation date (``Graph.load``'s
``as_of`` rule: a release with no upload time is kept), take the dependency
closure of the requested packages, and solve with Crux.

Outcomes
--------
not_on_pypi          a requested package does not exist on PyPI
ingest_error         the data layer could not fetch a requested package
root_unavailable     a requested package has no release meeting the request
                     in the snapshot at that date (most often: no CPython 3.12
                     wheel, the snapshot's target; or released later)
not_reproduced       Crux finds an install plan: no failure on this data
timeout              Crux did not finish within the limit
missing_dependency   unsatisfiable, but only because some dependency has no
                     usable release (e.g. sdist-only): with every edge into
                     such a package switched off, a plan exists.  A data-layer
                     artefact, not a dependency conflict.
reproduced           unsatisfiable, and still unsatisfiable with those edges
                     off: a genuine dependency conflict among the requested
                     packages on real metadata.  These form the corpus.

``same_packages`` (reproduced only): the replayed conflict involves at least
one package pip named in the reporter's own conflict lines; a rough check
that the replay is the same failure the user saw, not merely a failure.

    python -m crux.benchmark.replay --screened S.jsonl --db corpus.db \\
        --cases OUT_CASES.jsonl --instances OUT_INSTANCES.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from crux.benchmark.instance import Instance, dump_jsonl
from crux.benchmark.run_crux import CONFIGS
from crux.data.graph import Graph
from crux.resolver.problem import Constraint
from crux.resolver.solver import solve


class Snapshot:
    """The corpus database in memory: releases with upload times, edges."""

    def __init__(self, db_path):
        conn = sqlite3.connect(db_path)
        self.status = dict(conn.execute("SELECT name, status FROM projects"))
        self.releases = defaultdict(list)            # pkg -> [(ver, upload)]
        for name, ver, ut in conn.execute(
                "SELECT name, version, upload_time FROM releases"):
            self.releases[name].append(
                (ver, datetime.fromisoformat(ut) if ut else None))
        self.deps = defaultdict(list)                # (pkg, ver) -> [[d, s]]
        for name, ver, dep, spec in conn.execute(
                "SELECT name, version, dep_name, specifier FROM deps"):
            self.deps[(name, ver)].append([dep, spec])
        conn.close()

    def graph_as_of(self, names, as_of: datetime) -> dict:
        graph, todo = {}, list(names)
        while todo:
            pkg = todo.pop()
            if pkg in graph:
                continue
            kept = {v: self.deps.get((pkg, v), [])
                    for v, ut in self.releases.get(pkg, ())
                    if ut is None or ut <= as_of}
            if kept:
                graph[pkg] = kept
                todo.extend(d for deps in kept.values() for d, _ in deps)
        return graph


def _roots(requirements):
    out = []
    for r in requirements:
        req = Requirement(r)
        out.append((canonicalize_name(req.name), str(req.specifier)))
    return tuple(out)


def replay_case(snap: Snapshot, case: dict, *, timeout: float = 60.0):
    """Return (outcome dict, Instance or None)."""
    roots = _roots(case["requirements"])
    as_of = datetime.fromisoformat(case["created_at"].replace("Z", "+00:00"))
    out = {"url": case["url"], "created_at": case["created_at"],
           "requirements": case["requirements"], "method": case["method"]}
    for name, _ in roots:
        st = snap.status.get(name)
        if st == "not_found":
            return out | {"outcome": "not_on_pypi", "detail": name}, None
        if st != "done":
            return out | {"outcome": "ingest_error", "detail": name}, None
    graph_dict = snap.graph_as_of([n for n, _ in roots], as_of)
    graph = Graph.from_dict(graph_dict)
    for name, spec in roots:
        if not graph.candidates(name, spec):
            return out | {"outcome": "root_unavailable",
                          "detail": f"{name}{spec}"}, None
    inst = Instance(
        id=f"real/{case['repo']}#{case['url'].rsplit('/', 1)[1]}",
        graph=graph_dict, roots=roots, expected=None,
        meta={"source": "real", "url": case["url"], "created_at": case["created_at"],
              "method": case["method"], "snippet": case["snippet"],
              "original_conflict": case["original_conflict"],
              "reported_python": case["reported_python"],
              "packages": len(graph_dict),
              "versions": sum(len(v) for v in graph_dict.values()),
              "edges": sum(len(d) for v in graph_dict.values() for d in v.values())})
    cfg = dataclasses.replace(CONFIGS["C4-noLCV"], timeout_s=timeout)
    t0 = time.perf_counter()
    res = solve(inst.problem(graph), cfg)
    out |= {"crux_status": res.status, "crux_s": round(time.perf_counter() - t0, 3),
            "packages": inst.meta["packages"], "versions": inst.meta["versions"]}
    if res.status == "SAT":
        return out | {"outcome": "not_reproduced"}, None
    if res.status == "TIMEOUT":
        return out | {"outcome": "timeout"}, None
    # UNSAT: does it survive switching off edges into release-less packages?
    prob = inst.problem(Graph.from_dict(graph_dict))
    empty = {c for c in prob.constraints()
             if c.kind == "edge" and not graph_dict.get(c.dep)}
    if empty:
        res2 = solve(prob.with_disabled(empty), cfg)
        if res2.status != "UNSAT":
            return out | {"outcome": "missing_dependency",
                          "detail": sorted({c.dep for c in empty})[:5]}, None
    conflict_pkgs = ({c.dep for c in res.conflict}
                     | {c.pkg for c in res.conflict if c.pkg})
    original = set(case["original_conflict"])
    out |= {"outcome": "reproduced", "conflict_size": len(res.conflict),
            "conflict_packages": sorted(conflict_pkgs),
            "same_packages": (bool(conflict_pkgs & original) if original else None)}
    inst = dataclasses.replace(inst, expected="UNSAT")
    inst.meta.update(conflict_packages=sorted(conflict_pkgs),
                     same_packages=out["same_packages"])
    return out, inst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screened", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--cases", required=True, help="replay outcome per case (JSONL)")
    ap.add_argument("--instances", required=True, help="reproduced instances (JSONL)")
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)
    snap = Snapshot(a.db)
    screened = [json.loads(l) for l in open(a.screened)]
    kept = [c for c in screened if c["status"] == "kept"]
    rows, insts = [], []
    t0 = time.perf_counter()
    for k, case in enumerate(kept, 1):
        row, inst = replay_case(snap, case, timeout=a.timeout)
        rows.append(row)
        if inst is not None:
            insts.append(inst)
        if k % 50 == 0:
            print(f"  {k}/{len(kept)} ({time.perf_counter()-t0:.0f}s), "
                  f"{len(insts)} reproduced", flush=True)
    with open(a.cases, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    dump_jsonl(insts, a.instances)
    from collections import Counter
    print(Counter(r["outcome"] for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
