"""Experiment: core extraction methods compared, and how much smaller the
minimal core is than the conflicts the solver configurations return.

    python -m crux.explain.corebench --out results/core              # synthetic
    python -m crux.explain.corebench --out results/core --db data/crux.db

Writes ``rows.csv`` (one row per case x starting set x method),
``conflicts.csv`` (one row per case: conflict sizes of C0, the C4 preset,
learning without AC-3, and the minimal core) and ``report.md``.

Cases
  random        UNSAT instances among ``random_problem`` seeds (oracle-sized)
  thrash        known core of 4 inside n free roots
  thrash_wide   known core of 8
  wide_pair     ``thrash_wide(0, k)``: no free packages, core of 2 + 2k that
                is almost the whole universe (QuickXplain's worst case)
  random_csp    UNSAT instances of the near-hard random family
  toy           tests/fixtures/toy_abc.json, known core of 11
  real          (with --db) the realbench UNSAT pair plus conflicts DERIVED
                from real metadata: the newest release of a package together
                with its direct or transitive dependency pinned to that
                dependency's oldest release.  These are constructed by us, not
                user-reported failures; the hard-case corpus is Arrya's.

Starting sets: ``learn`` (the default, learning without AC-3), ``C0`` (plain
backtracking's coarse conflict), ``C4`` (the C4 preset, with AC-3) and
``universe`` (every constraint, ``Problem.constraints()``; skipped when it is
larger than ``--max-universe``).

How to read the numbers (the team's convention, docs/context.md 6.2): medians,
not sums.  Every core is checked by ``verify_core``; the report says how each
verdict was reached (oracle, checked plan, or two solvers agreeing).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from crux.data.graph import Graph
from crux.explain.core import (
    DEFAULT_SOLVE, METHODS, default_oracle, extract_core,
)
from crux.explain.verify import verify_core
from crux.resolver.families import random_csp, thrash, thrash_wide
from crux.resolver.gen import random_problem
from crux.resolver.problem import Config, Constraint, Problem
from crux.resolver.solver import solve

TOY = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "toy_abc.json"
STARTS = ("learn", "C0", "C4", "universe")


@dataclass
class Case:
    group: str
    name: str
    problem: Problem
    known: frozenset | None = None      # the core, when known by construction


@dataclass
class Row:
    group: str
    case: str
    start: str
    start_size: int
    method: str
    core_size: int
    calls: int
    sat_calls: int
    unsat_calls: int
    timeouts: int
    wall_s: float
    solver_s: float
    minimal: bool                       # proven by the method itself
    verified: bool | None               # verify_core.ok (None: not run)
    unsat_by: str                       # oracle / solvers / ""
    necessary_by: str                   # e.g. "oracle" or "plan" or "mixed"
    exact: bool | None                  # equals the known core
    jaccard: float | None               # overlap with the known core


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


# ── cases ────────────────────────────────────────────────────────────

def _thrash_core():
    return frozenset({Constraint.root("x"), Constraint.root("z"),
                      Constraint.edge("x", "1", "y", "<2"),
                      Constraint.edge("z", "1", "y", ">=3")})


def _wide_core(k):
    return frozenset({Constraint.root("x"), Constraint.root("z")}
                     | {Constraint.edge("x", v, "y", "<2")
                        for v in map(str, range(1, k + 1))}
                     | {Constraint.edge("z", v, "y", ">=3")
                        for v in map(str, range(1, k + 1))})


def synthetic_cases(seeds: int = 300, csp_seeds: int = 10) -> list[Case]:
    out = []
    for s in range(seeds):
        p = random_problem(s)
        if solve(p, DEFAULT_SOLVE).status == "UNSAT":
            out.append(Case("random", f"random/s{s}", p))
    for n in (3, 9, 20, 40):
        out.append(Case("thrash", f"thrash/{n}", thrash(n).problem,
                        _thrash_core()))
    for n in (3, 9):
        out.append(Case("thrash_wide", f"thrash_wide/{n}",
                        thrash_wide(n).problem, _wide_core(3)))
    for k in (5, 20, 50):
        out.append(Case("wide_pair", f"wide_pair/{k}",
                        thrash_wide(0, k=k).problem, _wide_core(k)))
    for size in (12, 16):
        for s in range(csp_seeds):
            p = random_csp(size, s).problem
            if solve(p, DEFAULT_SOLVE).status == "UNSAT":
                out.append(Case("random_csp", f"random_csp/{size}/s{s}", p))
    toy = Problem.from_requirements(
        Graph.from_dict(json.loads(TOY.read_text())), ["a", "b"])
    known = frozenset({Constraint.root("a"), Constraint.root("b")}
                      | {Constraint.edge("a", f"1.{i}", "c", "<2.0")
                         for i in range(5)}
                      | {Constraint.edge("b", f"1.{i}", "c", ">=3.1")
                         for i in range(4)})
    out.append(Case("toy", "toy_abc", toy, known))
    return out


def real_cases(graph: Graph, limit: int = 20) -> list[Case]:
    """UNSAT requirement sets on the real graph (see module docstring)."""
    from crux.resolver.realbench import pick_cases
    out, seen = [], set()

    def add(name, reqs, known=None):
        key = tuple(sorted(reqs))
        if key in seen:
            return
        p = Problem.from_requirements(graph, reqs)
        if solve(p, Config(mrv=True, learning=True, timeout_s=30)).status \
                == "UNSAT":
            seen.add(key)
            out.append(Case("real", name, p, known))

    for name, reqs in pick_cases(graph):
        if name.startswith("conflict:"):
            add(name, reqs)

    # Derived conflicts, at most PER_PACKAGE per top package, transitive
    # dependencies first (their cores are longer), each also in a "ranged"
    # form (pkg >= its 6th newest release) whose core spans several releases.
    pkgs = sorted(graph._versions, key=lambda p: (-len(graph.versions(p)), p))
    for pkg in pkgs:
        if len(out) >= limit:
            break
        top = graph.candidates(pkg, "")
        if not top:
            continue
        newest = top[0]
        r = solve(Problem.from_requirements(graph, [f"{pkg}=={newest}"]),
                  Config(mrv=True, timeout_s=30))
        if r.status != "SAT":
            continue
        direct = {dep for dep, _ in graph.deps(pkg, newest)}
        deps = sorted((d for d in r.plan if d != pkg),
                      key=lambda d: (d in direct, d))     # transitive first
        taken = 0
        for dep in deps:
            if taken >= PER_PACKAGE or len(out) >= limit:
                break
            oldest = graph.candidates(dep, "")
            if not oldest or oldest[-1] == r.plan[dep]:
                continue
            pin = f"{dep}=={oldest[-1]}"
            kind = "direct" if dep in direct else "transitive"
            known = None
            if kind == "direct":
                # The known core is the two roots and THE edge that excludes
                # the pinned release; only defined when exactly one edge of
                # pkg==newest to dep does (a release can list a dependency
                # twice with different ranges).
                excl = [s for d, s in graph.deps(pkg, newest) if d == dep
                        and oldest[-1] not in graph.allowed(dep, s)]
                if len(excl) == 1:
                    known = frozenset({
                        Constraint.root(pkg, f"=={newest}"),
                        Constraint.root(dep, f"=={oldest[-1]}"),
                        Constraint.edge(pkg, newest, dep, excl[0])})
            before = len(out)
            add(f"derived-{kind}:{pkg}=={newest}+{pin}",
                [f"{pkg}=={newest}", pin], known)
            if len(top) > 6 and len(out) < limit:
                add(f"derived-{kind}-ranged:{pkg}>={top[5]}+{pin}",
                    [f"{pkg}>={top[5]}", pin])
            taken += len(out) > before
    return out[:limit]


PER_PACKAGE = 2


# ── running ──────────────────────────────────────────────────────────

def _start_sets(case: Case, max_universe: int, timeout_s: float):
    p = case.problem
    starts = {}
    for name, cfg in (("learn", Config(mrv=True, learning=True)),
                      ("C0", Config.named("C0")),
                      ("C4", Config.named("C4"))):
        r = solve(p, Config(cfg.ac3, cfg.mrv, cfg.lcv, cfg.learning,
                            timeout_s))
        if r.status == "UNSAT":
            starts[name] = r.conflict
    u = p.constraints()
    if len(u) <= max_universe:
        starts["universe"] = list(u)
    return starts, len(u)


def run_case(case: Case, *, starts=STARTS, methods=METHODS,
             max_universe: int = 300, budget_s: float | None = 60.0,
             timeout_s: float = 10.0, verify: bool = True,
             max_states: int = 200_000):
    """(rows, conflict-size record) for one case."""
    p = case.problem
    universe = p.constraints()
    sets, n_universe = _start_sets(case, max_universe, timeout_s)
    rows, cores = [], {}
    checked: dict[frozenset, object] = {}   # verify each distinct core once
    for start in starts:
        if start not in sets:
            continue
        for m in methods:
            r = extract_core(p, sets[start], method=m,
                             config=default_oracle(m, timeout_s),
                             budget_s=budget_s, check_start=False,
                             universe=universe)
            v = None
            if verify:
                key = frozenset(r.core)
                if key not in checked:
                    checked[key] = verify_core(p, r.core,
                                               max_states=max_states,
                                               timeout_s=timeout_s * 3,
                                               universe=universe)
                v = checked[key]
            hows = set(v.necessary_by.values()) if v else set()
            rows.append(Row(
                case.group, case.name, start, r.start_size, m, r.size,
                r.calls, r.sat_calls, r.unsat_calls, r.timeouts,
                round(r.wall_s, 4), round(r.solver_s, 4), r.minimal,
                v.ok if v else None, v.unsat_by if v else "",
                "/".join(sorted(hows)),
                None if case.known is None else set(r.core) == case.known,
                None if case.known is None
                else round(jaccard(r.core, case.known), 4)))
            if r.minimal:
                cores.setdefault(start, r.size)
    conflict = {"group": case.group, "case": case.name,
                "universe": n_universe,
                "C0": len(sets["C0"]) if "C0" in sets else None,
                "C4": len(sets["C4"]) if "C4" in sets else None,
                "learn": len(sets["learn"]) if "learn" in sets else None,
                "core": cores.get("learn", cores.get("C0"))}
    return rows, conflict


# ── report ───────────────────────────────────────────────────────────

def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _fmt(x, nd=1):
    if x is None:
        return "-"
    if isinstance(x, float) and not x.is_integer():
        return f"{x:.{nd}f}"
    return f"{int(x):,}"


def _table(head, body):
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(map(str, r)) + " |" for r in body]
    return "\n".join(out)


def build_report(rows: list[Row], conflicts: list[dict], *, args) -> str:
    groups = list(dict.fromkeys(r.group for r in rows))
    L = ["# Core extraction benchmark", "",
         f"Command: `python -m crux.explain.corebench {args}`", ""]

    # correctness
    unproven = [r for r in rows if not r.minimal]
    # A PROBLEM is a core called minimal that the verifier rejects.  An
    # unproven core (timeout / budget) that the verifier finds not minimal is
    # the honest outcome, reported separately.
    failed = [r for r in rows if r.minimal and r.verified is False]
    confirmed_unproven = [r for r in unproven if r.verified is False]
    known = [r for r in rows if r.exact is not None]
    L += ["## Correctness", "",
          f"- {len(rows)} extractions over "
          f"{len({r.case for r in rows})} UNSAT cases.",
          f"- Proven minimal by the method: {len(rows) - len(unproven)} of "
          f"{len(rows)} (not proven: timeouts or budget).",
          f"- Independent verifier on cores called minimal: "
          f"{sum(r.minimal and r.verified is True for r in rows)} pass, "
          f"{len(failed)} fail, "
          f"{sum(r.minimal and r.verified is None for r in rows)} undecided.",
          f"- Unproven cores the verifier confirms are not minimal: "
          f"{len(confirmed_unproven)} of {len(unproven)}"
          + "".join(f"; {r.case} {r.start} {r.method} ({r.timeouts} "
                    f"timeouts)" for r in unproven[:5]) + ".",
          f"- UNSAT verdicts by oracle: "
          f"{sum(r.unsat_by == 'oracle' for r in rows)}, by two solvers "
          f"agreeing: {sum(r.unsat_by == 'solvers' for r in rows)}.",
          f"- Known cores: exact match {sum(r.exact for r in known)} of "
          f"{len(known)}; mean Jaccard "
          f"{_fmt(statistics.mean(r.jaccard for r in known), 3) if known else '-'}.",
          ""]
    if failed:
        L += ["**PROBLEM ROWS: a core called minimal failed verification; "
              "do not quote.**",
              ""]
        L += [f"- {r.case} {r.start} {r.method}" for r in failed[:20]] + [""]

    # conflict sizes
    body = []
    for g in groups:
        cs = [c for c in conflicts if c["group"] == g]
        body.append([g, len(cs)] + [_fmt(_med([c[k] for c in cs]))
                                    for k in ("universe", "C0", "C4",
                                              "learn", "core")])
    L += ["## Conflict size before and after extraction (medians)", "",
          "Constraints in the conflict each configuration returns, and in "
          "the minimal core cut from it.  `C4` is the preset with AC-3, "
          "`learn` is learning + MRV without AC-3 (the default start).", "",
          _table(["group", "cases", "universe", "C0", "C4 preset", "learn",
                  "minimal core"], body), ""]
    shrink = [(c["C0"] / c["core"]) for c in conflicts
              if c["C0"] and c["core"]]
    if shrink:
        L += [f"Median C0 conflict / minimal core: "
              f"{_fmt(statistics.median(shrink), 2)}x "
              f"(max {_fmt(max(shrink), 1)}x).", ""]

    # cost
    body = []
    for g in groups:
        for st in STARTS:
            rs = [r for r in rows if r.group == g and r.start == st]
            if not rs:
                continue
            cell = []
            for m in METHODS:
                mr = [r for r in rs if r.method == m]
                cell.append(f"{_fmt(_med([r.calls for r in mr]))} / "
                            f"{_fmt(_med([r.wall_s * 1000 for r in mr]))}")
            body.append([g, st, _fmt(_med([r.start_size for r in rs])),
                         _fmt(_med([r.core_size for r in rs]))] + cell)
    L += ["## Cost: solver calls / wall ms (medians)", "",
          _table(["group", "start", "start size", "core size"]
                 + list(METHODS), body), ""]

    # head to head
    body = []
    for g in groups:
        for st in STARTS:
            pairs = {}
            for r in rows:
                if r.group == g and r.start == st:
                    pairs.setdefault(r.case, {})[r.method] = r
            ws = [(d["deletion"].calls, d["quickxplain"].calls)
                  for d in pairs.values()
                  if "deletion" in d and "quickxplain" in d]
            if not ws:
                continue
            qx_better = sum(q < d for d, q in ws)
            del_better = sum(d < q for d, q in ws)
            body.append([g, st, len(ws), qx_better, del_better,
                         len(ws) - qx_better - del_better])
    L += ["## QuickXplain against deletion (solver calls, per case)", "",
          _table(["group", "start", "cases", "QX fewer", "deletion fewer",
                  "tie"], body), "",
          "Different methods may return different minimal cores when a "
          "problem has several; each is minimal.", ""]
    differ = sum(1 for case in {r.case for r in rows}
                 if len({r.core_size for r in rows if r.case == case
                         and r.minimal}) > 1)
    L += [f"Cases where methods/starts found minimal cores of different "
          f"sizes: {differ}.", ""]
    return "\n".join(L)


def write_csv(rows, path: Path, cls=None):
    if not rows and cls is None:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        if cls is not None:
            w = csv.DictWriter(f, [x.name for x in fields(cls)])
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        else:
            w = csv.DictWriter(f, list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="results/core")
    ap.add_argument("--seeds", type=int, default=300,
                    help="random_problem seeds (UNSAT ones are used)")
    ap.add_argument("--csp-seeds", type=int, default=10)
    ap.add_argument("--db", help="also run real cases from this database")
    ap.add_argument("--real-limit", type=int, default=20)
    ap.add_argument("--no-synthetic", action="store_true")
    ap.add_argument("--max-universe", type=int, default=300)
    ap.add_argument("--budget", type=float, default=60.0,
                    help="seconds per extraction")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds per solver call")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--starts", default=",".join(STARTS),
                    help="comma-separated subset of " + ",".join(STARTS))
    ap.add_argument("--methods", default=",".join(METHODS),
                    help="comma-separated subset of " + ",".join(METHODS))
    ap.add_argument("--only", default="",
                    help="run only cases whose name contains this text")
    ap.add_argument("--skip", default="",
                    help="skip cases whose name contains this text")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = [] if args.no_synthetic else synthetic_cases(args.seeds,
                                                         args.csp_seeds)
    if args.db:
        t = time.perf_counter()
        g = Graph.load(args.db)
        real = real_cases(g, args.real_limit)
        print(f"real: {len(real)} UNSAT cases "
              f"({time.perf_counter() - t:.1f} s)", file=sys.stderr)
        cases += real
    if args.only:
        cases = [c for c in cases if args.only in c.name]
    if args.skip:
        cases = [c for c in cases if args.skip not in c.name]
    starts = tuple(x for x in args.starts.split(",") if x)
    methods = tuple(x for x in args.methods.split(",") if x)
    rows, conflicts = [], []
    for i, case in enumerate(cases):
        rs, c = run_case(case, starts=starts, methods=methods,
                         max_universe=args.max_universe,
                         budget_s=args.budget, timeout_s=args.timeout,
                         verify=not args.no_verify)
        rows += rs
        conflicts.append(c)
        if case.group != "random" or i % 50 == 0:
            print(f"[{i + 1}/{len(cases)}] {case.name}: core {c['core']}",
                  file=sys.stderr)
    write_csv(rows, out / "rows.csv", Row)
    write_csv(conflicts, out / "conflicts.csv")
    argtext = " ".join(argv if argv is not None else sys.argv[1:])
    report = build_report(rows, conflicts, args=argtext)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 1 if any(r.minimal and r.verified is False for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
