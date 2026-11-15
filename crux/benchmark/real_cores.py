"""Minimal cores of the reproduced real conflicts.

The real conflicts (``benchmarks/corpus/instances.jsonl.gz``) have no planted
ground truth, so this measures what can be proven instead.  Per instance:

1. Extraction.  ``crux.explain.explain`` (core extraction, Yash Kejriwal)
   with each method, from the solver's own conflict: core size, the
   extractor's ``minimal`` flag, solver calls, time, and whether the
   methods return the same core.
2. Independent check.  ``crux.explain.verify_core`` on the core of
   ``explain``'s default method (QuickXplain): is it UNSAT, is every member
   necessary.
3. Uniqueness.  K is the ONLY minimal core of the instance I if and only if
   I - {k} is satisfiable for every k in K: then every unsatisfiable subset
   of I contains every k, so every minimal core contains K and, K being
   unsatisfiable, equals it.  Each "I - {k} is satisfiable" is certified by
   a plan that the independent checker (``check_plan``) accepts.  If some
   I - {k} is unsatisfiable, a different minimal core leaves k out: the
   failure has more than one explanation.  When K is unique, any correct
   annotation of the minimal conflict must equal K.
4. What the messages name.  pip's and uv's messages recorded by the harness
   (``benchmarks/results/real/messages.jsonl``) are scored against the
   core's packages with the measure used for planted cores
   (``messages.measure``).  Only meaningful where the core is unique.
5. Scope check.  Replay already excluded failures that disappear when edges
   into packages with NO release are switched off.  This goes further:
   switch off every edge whose specifier no release in the snapshot meets
   (for example a pinned version that has no wheel).  ``robust`` means the
   instance is still unsatisfiable: a conflict among releases that exist.
   Otherwise the failure depends on a version our snapshot lacks, which may
   not be the user's failure.  ``core_unavailable`` counts core members
   that no snapshot release meets; with ``--db`` (the corpus database) the
   drop reasons of the PyPI versions they ask for are looked up.

Cores are written in full (``real_cores.jsonl``) for the renderer and the
user study.

    python -m crux.benchmark.real_cores \\
        --instances benchmarks/corpus/instances.jsonl.gz \\
        --messages benchmarks/results/real/messages.jsonl \\
        --out benchmarks/results/cores [--db corpus.db]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from packaging.utils import canonicalize_name

from crux.benchmark.instance import Instance, constraint_to_json, load_jsonl
from crux.benchmark.messages import measure

METHODS = ("deletion", "deletion+refine", "quickxplain")
PRIMARY = "quickxplain"          # explain()'s default method
TOOLS = ("pip", "pip-baseline", "uv")


def core_packages(core) -> list[str]:
    return sorted({c.dep for c in core} | {c.pkg for c in core if c.pkg})


def groups(core) -> int:
    """Statements a renderer that groups by (package, dependency) would
    need: one per root, one per (package, dependency) pair of edges
    (docs/EXPLAIN.md section 9)."""
    return len({("", c.dep) if c.kind == "root" else (c.pkg, c.dep) for c in core})


def packages_of(inst: Instance) -> set[str]:
    names = set(inst.graph) | {d for d, _ in inst.roots}
    for vs in inst.graph.values():
        for deps in vs.values():
            names.update(d for d, _ in deps)
    return names


def uniqueness(problem, core, timeout_s: float) -> dict:
    """For each k in *core*: is the whole problem without k satisfiable?"""
    from crux.resolver.checker import check_plan
    from crux.resolver.problem import Config
    from crux.resolver.solver import solve
    cfg = Config(ac3=True, mrv=True, learning=True, timeout_s=timeout_s)
    needed = alternative = unknown = 0
    errors = []
    for k in core:
        p = problem.with_disabled({k})
        r = solve(p, cfg)
        if r.status == "SAT":
            bad = check_plan(p, r.plan)
            if bad:
                errors.append(f"plan without {k} rejected: {bad[:1]}")
                unknown += 1
            else:
                needed += 1
        elif r.status == "UNSAT":
            alternative += 1
        else:
            unknown += 1
    verdict = ("unique" if needed == len(core) else
               "not_unique" if alternative else "unknown")
    return {"unique": verdict, "in_every_core": needed,
            "left_out_by_another_core": alternative, "unique_unknown": unknown,
            "unique_errors": errors}


def _job(args):
    inst_json, budget_s, verify_timeout, unique_timeout, db = args
    from crux.explain import explain, verify_core
    inst = Instance.from_json(inst_json)
    rows, cores = [], {}
    for m in METHODS:
        t0 = time.perf_counter()
        res = explain(inst.problem(), method=m, budget_s=budget_s)
        cores[m] = res.core
        rows.append({"instance": inst.id, "method": m, "status": res.status,
                     "core_size": len(res.core), "claimed_minimal": res.minimal,
                     "start_size": res.start_size, "calls": res.calls,
                     "timeouts": res.timeouts,
                     "budget_exhausted": res.budget_exhausted,
                     "wall_s": round(time.perf_counter() - t0, 4)})
    core = cores[PRIMARY]
    rec = {"instance": inst.id, "url": inst.meta.get("url"),
           "screening": inst.meta.get("method"),
           "requirements": inst.requirements(),
           "methods_agree": len({frozenset(c) for c in cores.values()}) == 1}
    if not core:
        return rows, rec | {"core": None}
    prob = inst.problem()
    t0 = time.perf_counter()
    chk = verify_core(prob, core, timeout_s=verify_timeout)
    rec.update(verify_unsat=chk.unsat, verify_unsat_by=chk.unsat_by,
               verify_minimal=chk.minimal,
               verify_by=sorted(set(chk.necessary_by.values())),
               verify_errors=list(chk.errors),
               verify_s=round(time.perf_counter() - t0, 2))
    t0 = time.perf_counter()
    rec.update(uniqueness(prob, core, unique_timeout))
    rec["unique_s"] = round(time.perf_counter() - t0, 2)
    pk = core_packages(core)
    rec.update(core=[constraint_to_json(c) for c in core], core_size=len(core),
               core_roots=sum(c.kind == "root" for c in core),
               core_groups=groups(core), core_packages=pk,
               core_dangling=sum(1 for c in core if c.kind == "edge"
                                 and not inst.graph.get(c.dep)),
               original_conflict=inst.meta.get("original_conflict") or [])
    rec.update(scope_check(prob, core, unique_timeout))
    if db:
        rec["unavailable_reasons"] = drop_reasons(
            db, [c for c in core if not prob.graph.allowed(c.dep, c.spec)],
            inst.meta["created_at"])
    return rows, rec


def scope_check(problem, core, timeout_s: float) -> dict:
    """Is the instance still UNSAT with every edge that no snapshot release
    meets switched off?  A plan, if found, is checked independently."""
    from crux.resolver.checker import check_plan
    from crux.resolver.problem import Config
    from crux.resolver.solver import solve
    g = problem.graph
    empty = {c for c in problem.constraints()
             if c.kind == "edge" and not g.allowed(c.dep, c.spec)}
    p = problem.with_disabled(empty)
    r = solve(p, Config(ac3=True, mrv=True, learning=True, timeout_s=timeout_s))
    robust = None
    if r.status == "UNSAT":
        robust = True
    elif r.status == "SAT" and not check_plan(p, r.plan):
        robust = False
    return {"robust": robust, "unavailable_edges": len(empty),
            "core_unavailable": sum(1 for c in core if not g.allowed(c.dep, c.spec))}


def drop_reasons(db: str, members, created_at: str) -> list[str]:
    """Why no snapshot release meets each of *members*: the drop reasons of
    the PyPI versions it asks for (corpus database, ``drops`` table),
    ``released_after_report`` for releases uploaded after the report date,
    ``no_version_found`` if the database knows no such version."""
    import sqlite3
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    def match(spec, v):
        try:
            return spec.contains(Version(v), prereleases=True)
        except InvalidVersion:
            return False

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = set()
    for c in members:
        spec, found = SpecifierSet(str(c.spec)), False
        for v, reason in con.execute(
                "SELECT version, reason FROM drops WHERE stage = 'ingest' AND name = ?",
                (c.dep,)):
            if match(spec, v):
                out.add(reason)
                found = True
        for v, up in con.execute(
                "SELECT version, upload_time FROM releases WHERE name = ?", (c.dep,)):
            if match(spec, v) and up and up > created_at:
                out.add("released_after_report")
                found = True
        if not found:
            out.add("no_version_found")
    con.close()
    return sorted(out)


def score_messages(insts: dict, recs: list[dict], messages_path: Path) -> list[dict]:
    """Core recall / precision of each recorded pip and uv message."""
    by_id = {r["instance"]: r for r in recs if r.get("core")}
    out = []
    with open(messages_path) as f:
        for line in f:
            m = json.loads(line)
            rec = by_id.get(m["instance"])
            if m["tool"] not in TOOLS or m["status"] != "UNSAT" or rec is None:
                continue
            inst = insts[m["instance"]]
            s = measure(m["tool"], m["stdout"], m["stderr"],
                        packages=packages_of(inst),
                        roots=[canonicalize_name(d) for d, _ in inst.roots],
                        core_packages=rec["core_packages"])
            out.append({"instance": m["instance"], "tool": m["tool"],
                        "unique": rec["unique"], "core_packages": len(rec["core_packages"]),
                        "named": s["named"], "core_recall": round(s["core_recall"], 4),
                        "core_precision": (None if s["core_precision"] is None
                                           else round(s["core_precision"], 4)),
                        "names_whole_core": s["core_recall"] == 1.0,
                        "msg_lines": s["msg_lines"]})
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="minimal cores of the real conflicts")
    ap.add_argument("--instances", required=True)
    ap.add_argument("--messages", required=True,
                    help="harness messages.jsonl for the same instances")
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget", type=float, default=120.0,
                    help="seconds per instance and method")
    ap.add_argument("--verify-timeout", type=float, default=30.0)
    ap.add_argument("--unique-timeout", type=float, default=60.0)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--db", help="corpus database, to look up why a core member "
                                 "has no release (optional; not committed)")
    a = ap.parse_args(argv)
    try:
        import crux.explain  # noqa: F401
    except ImportError:
        print("crux.explain (core extraction) is not installed", file=sys.stderr)
        return 2
    insts = load_jsonl(a.instances)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    db = str(Path(a.db).resolve()) if a.db else None
    t0 = time.perf_counter()
    rows, recs = [], []
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        for rs, rec in ex.map(_job, [(i.to_json(), a.budget, a.verify_timeout,
                                      a.unique_timeout, db) for i in insts]):
            rows += rs
            recs.append(rec)
    _write_csv(out / "real_cores.csv", rows)
    with open(out / "real_cores.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    _write_csv(out / "real_messages.csv",
               score_messages({i.id: i for i in insts}, recs, Path(a.messages)))

    here = Path(__file__).resolve().parent

    def git(*c):
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                             capture_output=True, text=True).stdout.strip()
        return subprocess.run(["git", *c], capture_output=True, text=True,
                              cwd=top or here).stdout.strip()

    (out / "env_real.json").write_text(json.dumps({
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_head": git("rev-parse", "HEAD"),
        "explain_last_commit": git("log", "-1", "--format=%H %an %s", "--",
                                   "crux/explain"),
        "machine": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "methods": list(METHODS), "primary": PRIMARY, "budget_s": a.budget,
        "drop_reasons_from_db": bool(db),
        "verify_timeout_s": a.verify_timeout, "unique_timeout_s": a.unique_timeout,
        "instances": len(insts),
        "seconds": round(time.perf_counter() - t0, 1)}, indent=2) + "\n")
    uniq = {v: sum(r.get("unique") == v for r in recs)
            for v in ("unique", "not_unique", "unknown")}
    print(f"{len(insts)} instances in {time.perf_counter() - t0:.0f}s; "
          f"uniqueness: {uniq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
