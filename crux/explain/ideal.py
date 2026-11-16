"""Hand-written ideal explanations, and how the renderer compares to them.

``benchmarks/ideal/ideal.jsonl`` holds 30 ideal messages: the toy example,
20 real conflicts and 9 planted ones (format in
``benchmarks/ideal/README.md``).  Each names the requirements that can be
changed (``change``) and, where the snapshot allows one, fixes that work
(``fixes``); both are checked against the data by the tests, so an ideal
can be wrong in wording but not in fact.

CLI (prints one row per case and the medians):

    python -m crux.explain.ideal [--file benchmarks/ideal/ideal.jsonl] [--csv OUT]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from crux.data.graph import Graph
from crux.resolver.problem import Problem

ROOT = Path(__file__).resolve().parent.parent.parent
IDEAL = ROOT / "benchmarks" / "ideal" / "ideal.jsonl"
TOY = ROOT / "tests" / "fixtures" / "toy_abc.json"
REAL = ROOT / "benchmarks" / "corpus" / "instances.jsonl.gz"
PLANTED = ROOT / "benchmarks" / "planted" / "instances.jsonl"

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*[a-z0-9]|[a-z0-9]")


def load(path=IDEAL) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]


@lru_cache(maxsize=None)
def _instances() -> dict:
    from crux.benchmark.instance import load_jsonl
    out = {}
    for path in (REAL, PLANTED):
        if path.exists():
            out.update({i.id: i for i in load_jsonl(path)})
    return out


def graph_for(instance: str) -> Graph:
    if instance == "toy_abc":
        return Graph.from_dict(json.loads(TOY.read_text()))
    inst = _instances()[instance]
    return Graph.from_dict(inst.graph, drops=inst.meta.get("drops"))


def problem_for(rec: dict, requirements=None) -> Problem:
    return Problem.from_requirements(graph_for(rec["instance"]),
                                     requirements or rec["requirements"])


def apply_fix(requirements: list[str], fix: dict) -> list[str]:
    """The requirement list with one fix applied ({"replace": {old: new}}
    and/or {"remove": [req, ...]})."""
    out = [r for r in requirements if r not in fix.get("remove", ())]
    swap = fix.get("replace", {})
    missing = [r for r in list(swap) + list(fix.get("remove", ()))
               if r not in requirements]
    if missing:
        raise ValueError(f"fix names requirements not in the case: {missing}")
    return [swap.get(r, r) for r in out]


def packages_named(text: str, packages) -> set[str]:
    """Which of *packages* the text mentions as a whole word."""
    words = set(_NAME.findall(text.lower()))
    return {p for p in packages if p in words}


@dataclass
class Row:
    id: str
    instance: str
    source: str
    ideal_lines: int
    ideal_chars: int
    crux_lines: int
    crux_chars: int
    crux_names_change: bool       # the headline names every changeable request
    ideal_packages: int           # core packages the ideal names
    crux_covers_ideal: float      # share of those the renderer also names
    crux_extra: int               # core packages only the renderer names


def compare(rec: dict) -> Row:
    from crux.explain.core import explain
    from crux.explain.render import render_core
    p = problem_for(rec)
    r = explain(p)
    m = render_core(p, r.core, minimal=r.minimal)
    core_pk = {c.dep for c in r.core} | {c.pkg for c in r.core if c.pkg}
    ideal_pk = packages_named(rec["ideal"], core_pk)
    crux_pk = packages_named(m.text, core_pk)
    head = m.lines[0]
    return Row(
        id=rec["id"], instance=rec["instance"], source=rec["source"],
        ideal_lines=len(rec["ideal"].splitlines()), ideal_chars=len(rec["ideal"]),
        crux_lines=len(m.lines), crux_chars=len(m.text),
        crux_names_change=all(c in head for c in rec["change"]),
        ideal_packages=len(ideal_pk),
        crux_covers_ideal=(len(ideal_pk & crux_pk) / len(ideal_pk)) if ideal_pk else 1.0,
        crux_extra=len(crux_pk - ideal_pk))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=str(IDEAL))
    ap.add_argument("--csv")
    a = ap.parse_args(argv)
    rows = [compare(rec) for rec in load(a.file)]
    print(f"{'id':9} {'source':8} {'ideal l/ch':>11} {'crux l/ch':>11} "
          f"{'names':>5} {'covers':>6} {'extra':>5}  instance")
    for x in rows:
        print(f"{x.id:9} {x.source:8} {x.ideal_lines:>3}/{x.ideal_chars:<7}"
              f" {x.crux_lines:>3}/{x.crux_chars:<7} {'yes' if x.crux_names_change else 'NO':>5}"
              f" {x.crux_covers_ideal:>6.2f} {x.crux_extra:>5}  {x.instance}")
    med = statistics.median
    print(f"\nmedian chars: ideal {med(x.ideal_chars for x in rows):.0f}, "
          f"crux {med(x.crux_chars for x in rows):.0f}; "
          f"crux names the requests to change in {sum(x.crux_names_change for x in rows)}"
          f"/{len(rows)}; mean share of the ideal's packages crux names "
          f"{statistics.mean(x.crux_covers_ideal for x in rows):.2f}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(Row.__dataclass_fields__))
            w.writeheader()
            w.writerows(x.__dict__ for x in rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
