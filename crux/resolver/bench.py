"""Benchmark harness: run configurations over instance families.

    python -m crux.resolver.bench --families thrash,pinned_pair \
        --configs C0,C1 --timeout 10 --repeats 5 --out results/bench.csv

One row per (case, configuration).  Node and backtrack counts are
deterministic, so they come from the first run; wall-clock is the median of
``repeats`` runs (the proposal asks for 5).  A configuration that is not
implemented yet is skipped and reported, never run with a flag ignored.

Correctness is checked as it goes:
  * ``correct``: status equals the family's known answer (None if unknown or
    timed out);
  * ``consistent``: every configuration that finished agrees on the status
    of that case.
The CLI exits non-zero if anything is wrong or inconsistent.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from crux.data.graph import Graph
from crux.resolver.checker import check_plan
from crux.resolver.families import FAMILIES, Case
from crux.resolver.problem import Config, Problem
from crux.resolver.solver import solve


@dataclass
class Row:
    family: str
    size: int
    seed: int
    config: str
    status: str                 # SAT / UNSAT / TIMEOUT
    nodes: int
    backtracks: int
    ac3_pruned: int
    ac3_versions: int           # domain total AC-3 started from (0 if no AC-3)
    wall_s: float
    expected: str               # "" if unknown
    correct: bool | None        # None: unknown answer or timeout
    consistent: bool            # agrees with the other configs on this case
    valid_plan: bool | None     # SAT only: plan passed the independent checker
    non_latest: int | None      # SAT only: pins that are not the newest release


def non_latest_pins(problem: Problem, plan) -> int:
    """How many packages in *plan* are pinned below their newest release.

    "Newest" is ``Graph.candidates(pkg, "")[0]``: the newest version that is
    admissible with no constraint (so a pre-release only counts when the
    package has no final release).  Plain search tries newest first, so this
    is 0 only when nothing forced it to give ground; LCV can trade it for
    fewer backtracks.
    """
    g = problem.graph
    return sum(1 for pkg, v in plan.items() if v != g.candidates(pkg, "")[0])


def implemented(config: Config) -> bool:
    """True if solve() can run this configuration."""
    try:
        solve(Problem(Graph.from_dict({}), ()), config)
        return True
    except NotImplementedError:
        return False


def run_case(case: Case, name: str, *, timeout_s: float = 60.0,
             repeats: int = 1, check_plans: bool = True) -> Row:
    cfg = Config.named(name, timeout_s=timeout_s)
    walls, first = [], None
    for _ in range(max(1, repeats)):
        r = solve(case.problem, cfg)
        walls.append(r.stats.wall_s)
        if first is None:
            first = r
        if r.status == "TIMEOUT":
            break                       # repeating a timeout adds nothing
    st = first.stats
    correct = None
    if case.expected and first.status != "TIMEOUT":
        correct = first.status == case.expected
    valid = None
    if check_plans and first.status == "SAT":
        valid = not check_plan(case.problem, first.plan)
    pins = non_latest_pins(case.problem, first.plan) \
        if first.status == "SAT" else None
    return Row(case.family, case.size, case.seed, name, first.status,
               st.nodes, st.backtracks, st.ac3_pruned, st.ac3_versions,
               statistics.median(walls), case.expected or "", correct,
               True, valid, pins)


def run_suite(cases, configs=("C0",), *, timeout_s: float = 60.0,
              repeats: int = 1, check_plans: bool = True):
    """Run every case under every implemented configuration.

    Returns ``(rows, skipped)`` where ``skipped`` lists configuration names
    that are not implemented yet.
    """
    runnable, skipped = [], []
    for name in configs:
        (runnable if implemented(Config.named(name)) else skipped).append(name)
    rows: list[Row] = []
    for case in cases:
        group = [run_case(case, n, timeout_s=timeout_s, repeats=repeats,
                          check_plans=check_plans) for n in runnable]
        finished = {r.status for r in group if r.status != "TIMEOUT"}
        if len(finished) > 1:
            for r in group:
                r.consistent = False
        rows.extend(group)
    return rows, skipped


def build_cases(families=None, sizes=None, seeds=(0,)):
    """Cases for the named families (default: all) at their default sizes,
    or at ``sizes`` if given.  Families that ignore the seed are built once
    per size, not once per seed."""
    cases = []
    for name in families or FAMILIES:
        fam = FAMILIES[name]
        for size in sizes or fam.sizes:
            for seed in (seeds if fam.seeded else [0]):
                cases.append(fam.build(size, seed))
    return cases


# ── summaries ────────────────────────────────────────────────────────

def summarize(rows):
    """One dict per (family, size, config), medians over seeds."""
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        groups.setdefault((r.family, r.size, r.config), []).append(r)
    out = []
    for (fam, size, cfg), rs in groups.items():
        solved = [r for r in rs if r.status != "TIMEOUT"]
        pruned, base = sum(r.ac3_pruned for r in rs), \
            sum(r.ac3_versions for r in rs)
        pins = [r.non_latest for r in rs if r.non_latest is not None]
        out.append({
            "median_non_latest": statistics.median(pins) if pins else None,
            # share of the versions AC-3 started from that it removed
            "ac3_share_pruned": pruned / base if base else None,
            "family": fam, "size": size, "config": cfg, "runs": len(rs),
            "solved": len(solved), "timeouts": len(rs) - len(solved),
            "median_nodes": statistics.median(r.nodes for r in rs),
            "median_backtracks": statistics.median(r.backtracks for r in rs),
            "median_wall_s": statistics.median(r.wall_s for r in rs),
        })
    return out


def node_reduction(rows, base: str = "C0", other: str = "C4"):
    """Median ratio base.nodes / other.nodes over cases both finished.

    Returns ``{"cases": n, "median": x, "base_timed_out_other_solved": m,
    "other_timed_out_base_solved": k}``.  Timed-out cases are kept out of the
    median but always counted, in both directions: a case the other
    configuration lost by timing out must not disappear silently.  For a
    median that accounts for timeouts, use ``ablation.reduction_stats``.
    """
    by_case: dict[tuple, dict[str, Row]] = {}
    for r in rows:
        by_case.setdefault((r.family, r.size, r.seed), {})[r.config] = r
    ratios, base_to, other_to = [], 0, 0
    for rs in by_case.values():
        if base not in rs or other not in rs:
            continue
        b, o = rs[base], rs[other]
        bt, ot = b.status == "TIMEOUT", o.status == "TIMEOUT"
        if bt and not ot:
            base_to += 1
        elif ot and not bt:
            other_to += 1
        elif not bt and not ot:
            ratios.append(b.nodes / max(o.nodes, 1))
    return {"cases": len(ratios),
            "median": statistics.median(ratios) if ratios else None,
            "base_timed_out_other_solved": base_to,
            "other_timed_out_base_solved": other_to}


def format_table(rows) -> str:
    head = ["family", "size", "config", "runs", "solved", "t/o",
            "med nodes", "med backtracks", "med wall s", "ac3 pruned",
            "non-latest"]
    lines = [head]
    for s in summarize(rows):
        share = s["ac3_share_pruned"]
        pins = s["median_non_latest"]
        lines.append([s["family"], str(s["size"]), s["config"],
                      str(s["runs"]), str(s["solved"]), str(s["timeouts"]),
                      f"{s['median_nodes']:.0f}",
                      f"{s['median_backtracks']:.0f}",
                      f"{s['median_wall_s']:.3f}",
                      "-" if share is None else f"{share:.1%}",
                      "-" if pins is None else f"{pins:g}"])
    widths = [max(len(r[i]) for r in lines) for i in range(len(head))]
    return "\n".join("  ".join(c.rjust(w) if i > 1 else c.ljust(w)
                               for i, (c, w) in enumerate(zip(r, widths)))
                     for r in lines)


def problems(rows):
    """Rows that are wrong, inconsistent or carry an invalid plan."""
    return [r for r in rows
            if r.correct is False or not r.consistent
            or r.valid_plan is False]


# ── csv ──────────────────────────────────────────────────────────────

def write_csv(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[f.name for f in fields(Row)])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def read_csv(path):
    def tri(s):
        return None if s == "" else s == "True"

    def opt_int(s):
        return None if s == "" else int(s)
    with Path(path).open(newline="") as fh:
        return [Row(r["family"], int(r["size"]), int(r["seed"]), r["config"],
                    r["status"], int(r["nodes"]), int(r["backtracks"]),
                    int(r["ac3_pruned"]), int(r["ac3_versions"]),
                    float(r["wall_s"]), r["expected"],
                    tri(r["correct"]), r["consistent"] == "True",
                    tri(r["valid_plan"]), opt_int(r["non_latest"]))
                for r in csv.DictReader(fh)]


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--families", default=",".join(FAMILIES),
                    help="comma-separated; default all: " + ",".join(FAMILIES))
    ap.add_argument("--configs", default="C0",
                    help="comma-separated, e.g. C0,C1,C2,C3,C4")
    ap.add_argument("--sizes", default=None,
                    help="comma-separated; default is each family's own")
    ap.add_argument("--seeds", type=int, default=1,
                    help="seeds 0..N-1 per size (default 1)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--repeats", type=int, default=1,
                    help="wall-clock runs per case, median reported")
    ap.add_argument("--out", default=None, help="write rows to this CSV")
    args = ap.parse_args(argv)

    chosen = [f for f in args.families.split(",") if f]
    unknown = [f for f in chosen if f not in FAMILIES]
    if unknown:
        ap.error(f"unknown family {unknown}; choose from {list(FAMILIES)}")
    cases = build_cases(
        chosen,
        [int(s) for s in args.sizes.split(",")] if args.sizes else None,
        range(args.seeds))
    rows, skipped = run_suite(cases, args.configs.split(","),
                              timeout_s=args.timeout, repeats=args.repeats)
    print(format_table(rows))
    if skipped:
        print(f"\nnot implemented yet, skipped: {', '.join(skipped)}")
    if args.out:
        write_csv(rows, args.out)
        print(f"\nwrote {len(rows)} rows to {args.out}")
    bad = problems(rows)
    for r in bad:
        print(f"PROBLEM {r.family}/{r.size}/s{r.seed} {r.config}: "
              f"status={r.status} expected={r.expected or '?'} "
              f"correct={r.correct} consistent={r.consistent} "
              f"valid_plan={r.valid_plan}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
