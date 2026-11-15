"""Score core extraction against the planted ground truth.

Every planted core K is proven to be the UNIQUE minimal unsatisfiable core of
its instance (``crux.benchmark.planted.prove``).  So an extracted core is
correct exactly when it equals K: any other minimal core cannot exist, and a
core that is not K is either not minimal (extra constraints) or not
unsatisfiable (missing ones).  Scores per instance and method:

exact       the extracted core equals K
jaccard     |core & K| / |core | K|
missing     constraints of K the core lacks (> 0 means the core is NOT
            unsatisfiable on its own: K is the only minimal core)
extra       constraints outside K (> 0 means the core is NOT minimal)
claimed_minimal   the extractor's own ``minimal`` flag;
            ``claimed_minimal and not exact`` would be a contradiction

Starting set (``--start``): ``solver`` uses ``explain``, which starts from
the solver's own conflict; if that conflict already equals K, extraction only
has to confirm minimality.  ``universe`` starts from every constraint of the
instance (``Problem.constraints()``), so the method must find K among all the
background constraints: the harder test of the reduction itself.

The extractor is ``crux.explain.explain`` (core extraction, Yash Kejriwal),
imported when this runs, so this module works on any branch and scores the
extractor once it is merged.

    python -m crux.benchmark.score_cores \\
        --instances benchmarks/planted/instances.jsonl --out benchmarks/results/cores
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

from crux.benchmark.instance import Instance, load_jsonl


def score(core, truth) -> dict:
    core, truth = set(core), set(truth)
    union = core | truth
    return {"exact": core == truth,
            "jaccard": len(core & truth) / len(union) if union else 1.0,
            "missing": len(truth - core), "extra": len(core - truth),
            "core_size": len(core), "truth_size": len(truth)}


def _job(args):
    inst_json, methods, budget_s, start = args
    from crux.explain import explain, extract_core   # imported in the worker
    inst = Instance.from_json(inst_json)
    rows = []
    for m in methods:
        t0 = time.perf_counter()
        prob = inst.problem()
        if start == "universe":
            universe = prob.constraints()
            res = extract_core(prob, universe, method=m, budget_s=budget_s,
                               universe=universe)
        else:
            res = explain(prob, method=m, budget_s=budget_s)
        row = {"instance": inst.id, "kind": inst.meta.get("kind"),
               "depth": inst.meta.get("depth"), "noise": inst.meta.get("noise"),
               "method": m, "start": start, "status": res.status,
               "claimed_minimal": res.minimal, "start_size": res.start_size,
               "calls": res.calls,
               "timeouts": res.timeouts, "budget_exhausted": res.budget_exhausted,
               "wall_s": round(time.perf_counter() - t0, 4)}
        if res.status == "UNSAT":
            row.update(score(res.core, inst.core))
        rows.append(row)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="score core extraction on planted cores")
    ap.add_argument("--instances", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--methods", default="deletion,deletion+refine,quickxplain")
    ap.add_argument("--budget", type=float, default=60.0,
                    help="seconds per instance and method")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--start", choices=("solver", "universe"), default="solver")
    a = ap.parse_args(argv)
    try:
        import crux.explain  # noqa: F401
    except ImportError:
        print("crux.explain (core extraction) is not installed on this branch",
              file=sys.stderr)
        return 2
    insts = [i for i in load_jsonl(a.instances) if i.core is not None]
    methods = a.methods.split(",")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    rows = []
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        for rs in ex.map(_job, [(i.to_json(), methods, a.budget, a.start)
                                for i in insts]):
            rows += rs
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(out / f"cores_{a.start}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)
    here = Path(__file__).resolve().parent

    def git(*c):
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                             capture_output=True, text=True).stdout.strip()
        return subprocess.run(["git", *c], capture_output=True, text=True,
                              cwd=top or here).stdout.strip()

    (out / f"env_{a.start}.json").write_text(json.dumps({
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_head": git("rev-parse", "HEAD"),
        "explain_last_commit": git("log", "-1", "--format=%H %an %s", "--",
                                   "crux/explain"),
        "machine": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "methods": methods, "start": a.start, "budget_s": a.budget,
        "instances": len(insts),
        "seconds": round(time.perf_counter() - t0, 1)}, indent=2) + "\n")
    exact = {m: sum(r.get("exact") is True for r in rows if r["method"] == m) for m in methods}
    print(f"scored {len(insts)} instances x {len(methods)} methods "
          f"in {time.perf_counter() - t0:.0f}s; exact matches: {exact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
