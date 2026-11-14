"""Run Crux on one instance file in a fresh process and print a JSON result.

Used by the harness so Crux's end-to-end time (interpreter start, imports,
loading the instance, building the graph, solving) is measured the same way
as pip's and uv's.

    python -m crux.benchmark.run_crux INSTANCE.json --config C4-noLCV --timeout 60
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from crux.benchmark.instance import Instance, constraint_to_json
from crux.resolver.problem import Config
from crux.resolver.solver import solve

CONFIGS = {
    **{f"C{i}": Config.named(f"C{i}") for i in range(5)},
    # everything except LCV: values stay newest-first (docs/context.md 6.3)
    "C4-noLCV": Config(ac3=True, mrv=True, learning=True),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("instance")
    ap.add_argument("--config", default="C4-noLCV", choices=sorted(CONFIGS))
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)
    inst = Instance.from_json(json.loads(Path(a.instance).read_text()))
    cfg = dataclasses.replace(CONFIGS[a.config], timeout_s=a.timeout)
    res = solve(inst.problem(), cfg)
    print(json.dumps({
        "status": res.status,
        "nodes": res.stats.nodes,
        "solve_s": res.stats.wall_s,
        "plan": ({k: str(v) for k, v in res.plan.items()} if res.plan else None),
        "conflict": ([constraint_to_json(c) for c in res.conflict]
                     if res.conflict is not None else None),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
