"""Scoring an extracted core against a proven-unique planted core."""

import pytest

from crux.benchmark.score_cores import score
from crux.resolver.problem import Constraint

K = {Constraint.root("a", ">=1"), Constraint.edge("a", "1.0", "c", "<2")}
EXTRA = Constraint.edge("b", "1.0", "c", ">=3")


def test_exact_match():
    s = score(K, K)
    assert s == {"exact": True, "jaccard": 1.0, "missing": 0, "extra": 0,
                 "core_size": 2, "truth_size": 2}


def test_extra_constraint_is_not_exact():
    s = score(K | {EXTRA}, K)
    assert not s["exact"] and s["extra"] == 1 and s["missing"] == 0
    assert s["jaccard"] == pytest.approx(2 / 3)


def test_missing_constraint_is_not_exact():
    s = score({Constraint.root("a", ">=1")}, K)
    assert not s["exact"] and s["missing"] == 1 and s["jaccard"] == 0.5


def test_extractor_on_planted_instances_if_present():
    """Runs only where core extraction (crux.explain) is installed."""
    explain = pytest.importorskip("crux.explain").explain
    from crux.benchmark.planted import Params, generate
    for p in [Params("direct", 1, "none", 0), Params("deep", 3, "small", 1)]:
        inst = generate(p)
        res = explain(inst.problem(), method="quickxplain", budget_s=60)
        assert res.status == "UNSAT" and set(res.core) == set(inst.core)
