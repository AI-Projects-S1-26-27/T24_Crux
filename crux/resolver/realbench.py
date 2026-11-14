"""Benchmark the resolver on a REAL PyPI graph (the cleaned crux.db).

    python -m crux.resolver.realbench --db data/crux.db --timeout 20 \
        --out results/real

What it does, and what it cannot do
-----------------------------------
* Loads ``Graph.load(db)`` and reports load time and peak memory.
* Builds requirement sets that exist in the graph (single popular packages,
  pairs, an old pin that forces backtracking, and the boto3/botocore case),
  and runs every configuration C0..C4 on each under a timeout.
* The brute-force oracle is far too large here, so correctness is checked by
  (a) ``check_plan`` on every SAT plan, the independent checker, and
  (b) every configuration that finished agreeing on SAT/UNSAT.  That catches
  a wrong plan and a disagreement; it cannot prove an UNSAT answer correct.
* Writes ``real_rows.csv`` and ``real_report.md``.

The graph is the 30-seed pilot closure (76 packages), not the 5,000-seed
dataset, and it is easier than real resolution (extras dropped, sdist-only
releases dropped, unknown dependencies recorded as none).  Say so whenever a
number from here is quoted.

Plans are not all the same.  LCV (in C3 and C4) prefers the release that
constrains its dependencies least, and this graph records many old releases
with NO dependencies, so LCV picks very old releases and much smaller plans
(``requests`` 2.15.1 alone, instead of 2.34.2 and four dependencies).  Node
counts between LCV and newest-first configurations therefore compare
different answers, not the same answer found faster.  The report shows the
plan size and the number of non-latest pins for every configuration, and the
extra configuration ``C4-noLCV`` (AC-3, MRV, learning, newest first) shows
learning on a like-for-like footing.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from crux.data.graph import Graph
from crux.resolver.bench import non_latest_pins
from crux.resolver.checker import check_plan
from crux.resolver.problem import Config, Problem
from crux.resolver.solver import solve

CONFIG_SPECS = {
    **{f"C{i}": Config.named(f"C{i}") for i in range(5)},
    # the practical resolver: everything except LCV, so values stay newest
    # first and the plan is comparable with C0's
    "C4-noLCV": Config(ac3=True, mrv=True, learning=True),
}
CONFIGS = tuple(CONFIG_SPECS)


def _config(name: str, timeout_s: float) -> Config:
    return dataclasses.replace(CONFIG_SPECS[name], timeout_s=timeout_s)


@dataclass
class RealRow:
    case: str
    requirements: str
    config: str
    status: str
    nodes: int
    backtracks: int
    wall_s: float               # COLD: Graph caches cleared before the run
    warm_s: float               # same run again with the caches now filled
    plan_size: int | None
    non_latest: int | None      # SAT only: pins below the newest release
    valid_plan: bool | None
    consistent: bool


# ── building cases from the real graph ───────────────────────────────

def _packages(graph: Graph):
    return sorted(graph._versions)            # canonical names with releases


def pick_cases(graph: Graph, limit: int = 40):
    """Requirement sets drawn from what the graph actually contains.

    Returns ``[(name, [requirement strings])]``.  Deterministic.
    """
    pkgs = _packages(graph)
    by_size = sorted(pkgs, key=lambda p: -len(graph.versions(p)))
    cases = []

    def add(name, reqs):
        cases.append((name, list(reqs)))

    # the most widely released packages, one at a time (the hard ones)
    for p in by_size[:8]:
        add(f"single:{p}", [p])
    # well-known names if present
    for p in ("requests", "flask", "pandas", "numpy", "django", "boto3",
              "botocore", "urllib3", "jinja2", "click"):
        if p in pkgs and f"single:{p}" not in {c[0] for c in cases}:
            add(f"single:{p}", [p])
    # the pathological pair, if both exist
    if "boto3" in pkgs and "botocore" in pkgs:
        add("pair:boto3+botocore", ["boto3", "botocore"])
        old = graph.versions("botocore")[len(graph.versions("botocore")) // 2]
        add(f"pin:boto3+botocore<={old}", ["boto3", f"botocore<={old}"])
        older = graph.versions("botocore")[-1]
        add(f"pin:boto3+botocore=={older}", ["boto3", f"botocore=={older}"])
    # pairs of big packages
    for a, b in zip(by_size[:6], by_size[1:7]):
        add(f"pair:{a}+{b}", [a, b])
    # an old pin of a popular package: forces older dependency choices
    for p in by_size[:6]:
        vs = graph.versions(p)
        if len(vs) >= 4:
            add(f"old:{p}<={vs[len(vs) // 2]}", [f"{p}<={vs[len(vs) // 2]}"])
    # a real conflict that needs backtracking: an old urllib3 forces requests
    # back to a release that still allows it
    if "requests" in pkgs and "urllib3" in pkgs:
        vs = graph.versions("urllib3")
        old = vs[len(vs) * 2 // 3]
        add(f"backtrack:requests+urllib3<={old}",
            ["requests", f"urllib3<={old}"])
    # a real unsatisfiable pair: every boto3 release pins ONE botocore
    # release, so a new boto3 with an old botocore cannot exist
    if "boto3" in pkgs and "botocore" in pkgs:
        bo, bc = graph.versions("boto3"), graph.versions("botocore")
        if len(bo) >= 10 and len(bc) >= 10:
            new_boto3 = bo[len(bo) // 5]
            old_botocore = bc[-(len(bc) // 5)]
            add(f"conflict:boto3>={new_boto3}+botocore<={old_botocore}",
                [f"boto3>={new_boto3}", f"botocore<={old_botocore}"])
    # drop duplicates, keep order.  The key ignores requirement order: the
    # first version kept "boto3 botocore" and "botocore boto3" as two cases.
    seen, out = set(), []
    for name, reqs in cases:
        key = tuple(sorted(reqs))
        if key not in seen:
            seen.add(key)
            out.append((name, reqs))
    return out[:limit]


# ── running ──────────────────────────────────────────────────────────

def run_case(graph: Graph, name: str, reqs, configs=CONFIGS, *,
             timeout_s: float = 20.0):
    problem = Problem.from_requirements(graph, reqs)
    rows = []
    for cfg_name in configs:
        cfg = _config(cfg_name, timeout_s)
        # Each run starts with empty Graph caches.  Without this the first
        # configuration to need a requirement pays for filling the cache and
        # every later one gets it free, which made AC-3 look 25x slower than
        # MRV (both run the same AC-3) in the first version of this benchmark.
        reset_graph_caches(graph)
        r = solve(problem, cfg)
        warm = r.stats.wall_s
        if r.status != "TIMEOUT":
            warm = solve(problem, cfg).stats.wall_s     # caches now filled
        valid = pins = None
        if r.status == "SAT":
            valid = not check_plan(problem, r.plan)
            pins = non_latest_pins(problem, r.plan)
        rows.append(RealRow(name, " ".join(reqs), cfg_name, r.status,
                            r.stats.nodes, r.stats.backtracks,
                            r.stats.wall_s, warm,
                            len(r.plan) if r.plan is not None else None,
                            pins, valid, True))
    finished = {r.status for r in rows if r.status != "TIMEOUT"}
    if len(finished) > 1:
        for r in rows:
            r.consistent = False
    return rows


def reset_graph_caches(graph: Graph) -> None:
    """Empty the Graph's lazily filled caches so a timing starts cold.

    Touches Graph's private caches on purpose: a benchmark has to, and it
    changes no data, only what has been memoised so far.
    """
    graph._allowed.clear()
    graph._candidates.clear()
    graph._reverse = None
    graph.allowed_misses = 0


def load_graph(db_path):
    """Load the graph, measuring wall time and peak memory."""
    tracemalloc.start()
    t0 = time.perf_counter()
    g = Graph.load(db_path)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return g, dt, peak


def graph_facts(g: Graph) -> dict:
    n_rel = sum(len(v) for v in g._versions.values())
    n_edge = sum(len(e) for e in g._deps.values())
    sizes = sorted((len(v), p) for p, v in g._versions.items())
    return {"packages": len(g._versions), "releases": n_rel, "edges": n_edge,
            "dangling": len(g.dangling()), "collisions": len(g.collisions),
            "largest": [(p, n) for n, p in sizes[-5:][::-1]]}


def problems(rows):
    return [r for r in rows if r.valid_plan is False or not r.consistent]


# ── report ───────────────────────────────────────────────────────────

def _md(head, body):
    return "\n".join(["| " + " | ".join(head) + " |",
                      "|" + "|".join("---" for _ in head) + "|"]
                     + ["| " + " | ".join(r) + " |" for r in body])


def build_report(rows, *, facts, load_s, peak_bytes, timeout_s, db) -> str:
    bad = problems(rows)
    L = ["# Resolver on real PyPI data", ""]
    L.append(f"Database: `{db}`. Generated {datetime.now():%Y-%m-%d %H:%M}. "
             f"Timeout {timeout_s:g} s per run.")
    L += ["", "## The graph", "",
          f"{facts['packages']} packages, {facts['releases']:,} releases, "
          f"{facts['edges']:,} dependency edges, {facts['dangling']} dangling "
          f"edges, {facts['collisions']} version collisions. "
          f"Loaded in {load_s:.2f} s, peak {peak_bytes / 2**20:.1f} MiB. "
          "Largest by releases: "
          + ", ".join(f"{p} ({n:,})" for p, n in facts["largest"]) + "."]
    L += ["", f"**Correctness check:** {len(rows)} runs; "
          + ("every SAT plan passed the independent checker and every "
             "configuration that finished agreed on SAT/UNSAT."
             if not bad else
             f"**{len(bad)} PROBLEM ROWS. Do not quote these numbers.**")]
    L.append("")
    L.append("The oracle is too large at this scale, so this cannot prove an "
             "UNSAT answer correct; it catches wrong plans and disagreement.")

    cases = []
    for r in rows:
        if r.case not in cases:
            cases.append(r.case)
    by = {(r.case, r.config): r for r in rows}
    cfgs = [k for k in CONFIGS if any(r.config == k for r in rows)]

    def table(title, note, cell):
        out = ["", f"## {title}", "", note, ""]
        head = ["case", "answer", "C0 plan"] + cfgs
        body = []
        for c in cases:
            rs = [by[(c, k)] for k in cfgs if (c, k) in by]
            done = [r for r in rs if r.status != "TIMEOUT"]
            ans = done[0].status if done else "t/o"
            size = next((r.plan_size for r in rs
                         if r.plan_size is not None), None)
            body.append([c, ans, "-" if size is None else str(size)]
                        + [cell(by.get((c, k))) for k in cfgs])
        return out + [_md(head, body)]

    L += table("Plan chosen: packages / pins below the newest release",
               "Configurations can return DIFFERENT valid plans. LCV (C3, C4) "
               "prefers releases that constrain their dependencies least, and "
               "old releases here are recorded with no dependencies, so it "
               "often picks a much older, smaller plan. Compare node counts "
               "only between configurations that chose the same plan "
               "(`C4-noLCV` keeps newest-first values).",
               lambda r: "-" if r is None or r.plan_size is None else
               f"{r.plan_size} / {r.non_latest}")
    L += table("Nodes explored",
               "`t/o` = timed out (the figure is where it stopped, a lower "
               "bound).",
               lambda r: "-" if r is None else
               f"{r.nodes:,}" + (" t/o" if r.status == "TIMEOUT" else ""))
    L += table("Time, cold (seconds)",
               "Every run starts with the Graph's caches empty: what a first "
               "query pays.",
               lambda r: "-" if r is None else f"{r.wall_s:.3f}")
    L += table("Time, warm (seconds)",
               "The same run repeated once the caches are filled: what a "
               "later query pays. Cold minus warm is mostly "
               "`Graph.allowed()` filling its cache.",
               lambda r: "-" if r is None else f"{r.warm_s:.3f}")

    L += ["", "## Summary per configuration", ""]
    head = ["config", "cases", "solved", "timeouts", "median nodes",
            "median cold s", "median warm s", "total cold s", "total warm s"]
    body = []
    for k in cfgs:
        rs = [r for r in rows if r.config == k]
        body.append([k, str(len(rs)),
                     str(sum(r.status != "TIMEOUT" for r in rs)),
                     str(sum(r.status == "TIMEOUT" for r in rs)),
                     f"{statistics.median(r.nodes for r in rs):,.0f}",
                     f"{statistics.median(r.wall_s for r in rs):.3f}",
                     f"{statistics.median(r.warm_s for r in rs):.3f}",
                     f"{sum(r.wall_s for r in rs):.2f}",
                     f"{sum(r.warm_s for r in rs):.2f}"])
    L.append(_md(head, body))

    L += ["", "## Caveats", "",
          "* A 30-seed pilot closure, not the 5,000-seed dataset.",
          "* Easier than real resolution: extras and sdist-only releases are "
          "dropped and unknown dependencies are recorded as none.",
          "* One machine, one run per case: wall times are indicative, not a "
          "benchmark of the resolver against pip or uv.",
          "* Most real requirement sets here are satisfiable and need only a "
          "handful of nodes, so total time is dominated by fixed costs "
          "(AC-3 setup, LCV cost computation), not by search.",
          "* LCV's plans (C3, C4) are often far older and smaller than the "
          "newest-first plans, partly because old releases are recorded with "
          "no dependencies (a known data-layer caveat). They are valid "
          "against this graph but not what a user would want, and their node "
          "counts are not comparable with the newest-first configurations."]
    return "\n".join(L) + "\n"


# ── csv / CLI ────────────────────────────────────────────────────────

def write_csv(rows, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[f.name for f in fields(RealRow)])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="data/crux.db")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=40, help="max cases")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--out", default="results/real")
    args = ap.parse_args(argv)

    if not Path(args.db).is_file():
        ap.error(f"{args.db} not found: build it with "
                 "python -m crux.data.run_ingest --limit 30 && "
                 "python -m crux.data.clean")
    g, load_s, peak = load_graph(args.db)
    facts = graph_facts(g)
    print(f"graph: {facts['packages']} packages, {facts['releases']:,} "
          f"releases, {facts['edges']:,} edges; load {load_s:.2f}s, "
          f"peak {peak / 2**20:.1f} MiB")
    configs = [c for c in args.configs.split(",") if c]
    rows = []
    cases = pick_cases(g, args.limit)
    for i, (name, reqs) in enumerate(cases, 1):
        got = run_case(g, name, reqs, configs, timeout_s=args.timeout)
        rows += got
        print(f"[{i}/{len(cases)}] {name:40s} "
              + "  ".join(f"{r.config}:{r.status[:3]} {r.nodes}n "
                          f"{r.wall_s:.2f}s" for r in got), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out / "real_rows.csv")
    (out / "real_report.md").write_text(
        build_report(rows, facts=facts, load_s=load_s, peak_bytes=peak,
                     timeout_s=args.timeout, db=args.db), encoding="utf-8")
    bad = problems(rows)
    for r in bad:
        print(f"PROBLEM {r.case} {r.config}: status={r.status} "
              f"valid_plan={r.valid_plan} consistent={r.consistent}",
              file=sys.stderr)
    print(f"wrote real_rows.csv, real_report.md to {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
