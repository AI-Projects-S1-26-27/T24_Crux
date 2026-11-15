"""Uniqueness and scope checks for cores of real conflicts, on tiny graphs."""

from crux.benchmark.instance import Instance
from crux.benchmark.real_cores import _job, core_packages, groups, scope_check, uniqueness
from crux.explain import explain
from crux.resolver.problem import Constraint

# a and b need disjoint ranges of c: exactly one minimal core.
UNIQUE = Instance(
    id="t/unique", expected="UNSAT", roots=(("a", ""), ("b", "")),
    graph={"a": {"1.0": [["c", "<2"]]}, "b": {"1.0": [["c", ">=3"]]},
    "c": {"1.0": [], "3.1": []}},
    meta={"created_at": "2026-01-01T00:00:00Z"})

# x's only release needs y==9 (no such release) AND z>=2 (none either):
# two minimal cores, and both rest on a version the snapshot lacks.
TWO_WAYS = Instance(
    id="t/two", expected="UNSAT", roots=(("x", ""),),
    graph={"x": {"1.0": [["y", "==9"], ["z", ">=2"]]},
           "y": {"1.0": []}, "z": {"1.0": []}},
    meta={"created_at": "2026-01-01T00:00:00Z"})


def test_unique_core_is_proven_unique_and_robust():
    p = UNIQUE.problem()
    core = explain(p).core
    assert set(core) == {Constraint.root("a", ""), Constraint.root("b", ""),
                         Constraint.edge("a", "1.0", "c", "<2"),
                         Constraint.edge("b", "1.0", "c", ">=3")}
    u = uniqueness(p, core, 10)
    assert u["unique"] == "unique" and u["in_every_core"] == 4
    s = scope_check(p, core, 10)
    assert s["robust"] is True and s["core_unavailable"] == 0


def test_second_core_is_detected_and_scope_flagged():
    p = TWO_WAYS.problem()
    core = explain(p).core
    assert len(core) == 2                      # the root and one of the edges
    u = uniqueness(p, core, 10)
    # the root is in every core; the edge is left out by the other core
    assert u["unique"] == "not_unique"
    assert u["in_every_core"] == 1 and u["left_out_by_another_core"] == 1
    s = scope_check(p, core, 10)
    assert s["robust"] is False and s["unavailable_edges"] == 2
    assert s["core_unavailable"] == 1


def test_groups_and_packages():
    core = [Constraint.root("six", "==1.16.0"), Constraint.root("six", "~=1.15.0")]
    assert groups(core) == 1 and core_packages(core) == ["six"]
    core = [Constraint.root("a", ""), Constraint.edge("a", "1.0", "c", "<2"),
            Constraint.edge("a", "2.0", "c", "<2.1"), Constraint.root("c", ">=3")]
    assert groups(core) == 3 and core_packages(core) == ["a", "c"]


def test_job_record_is_consistent():
    rows, rec = _job((UNIQUE.to_json(), 30, 10, 10, None))
    assert [r["method"] for r in rows] == ["deletion", "deletion+refine", "quickxplain"]
    assert all(r["claimed_minimal"] for r in rows)
    assert rec["methods_agree"] and rec["verify_unsat"] and rec["verify_minimal"]
    assert rec["unique"] == "unique" and rec["robust"] is True
    assert rec["core_size"] == 4 and rec["core_groups"] == 4
