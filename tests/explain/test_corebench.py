"""The core benchmark harness (crux/explain/corebench.py) runs, verifies
every core, and reports known-core accuracy correctly."""

import csv

from crux.data.graph import Graph
from crux.explain.corebench import (
    Case, jaccard, main, real_cases, run_case, synthetic_cases,
)
from crux.resolver.families import thrash


def test_jaccard():
    assert jaccard({1, 2}, {1, 2}) == 1.0
    assert jaccard({1, 2}, {2, 3}) == 1 / 3
    assert jaccard(set(), set()) == 1.0


def test_a_known_core_is_matched_exactly_for_every_start_and_method():
    case = next(c for c in synthetic_cases(seeds=0, csp_seeds=0)
                if c.name == "toy_abc")
    rows, conflict = run_case(case)
    assert {r.start for r in rows} == {"learn", "C0", "C4", "universe"}
    assert all(r.exact and r.jaccard == 1.0 for r in rows)
    assert all(r.minimal and r.verified for r in rows)
    assert conflict["core"] == 11


def test_a_wrong_known_core_is_reported_as_inexact():
    p = thrash(3).problem
    wrong = frozenset({next(iter(p.constraints()))})    # root f0: not in it
    rows, _ = run_case(Case("t", "t", p, known=wrong))
    assert not any(r.exact for r in rows)
    assert all(0 <= r.jaccard < 1 for r in rows)


def test_main_writes_the_three_outputs(tmp_path):
    assert main(["--out", str(tmp_path), "--seeds", "20",
                 "--csp-seeds", "0"]) == 0
    rows = list(csv.DictReader((tmp_path / "rows.csv").open()))
    assert rows and all(r["verified"] == "True" for r in rows)
    assert (tmp_path / "conflicts.csv").exists()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Correctness" in report and "PROBLEM ROWS" not in report


def test_derived_real_cases_on_a_small_graph():
    # app 2.0 needs lib>=2; pinning lib to its oldest release conflicts.
    g = Graph.from_dict({
        "app": {"1.0": [["lib", ""]], "2.0": [["lib", ">=2"]]},
        "lib": {"1.0": [], "2.0": [], "3.0": []},
    })
    cases = real_cases(g)
    names = [c.name for c in cases]
    assert "derived-direct:app==2.0+lib==1.0" in names
    case = cases[names.index("derived-direct:app==2.0+lib==1.0")]
    rows, _ = run_case(case)
    assert all(r.exact for r in rows)
