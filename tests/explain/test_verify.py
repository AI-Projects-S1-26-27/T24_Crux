"""The minimality verifier (crux/explain/verify.py) must reject bad cores,
not only accept good ones, and must say how it reached each verdict."""

import json
from pathlib import Path

import pytest

from crux.data.graph import Graph
from crux.explain import explain, verify_core
from crux.resolver import Constraint, Problem
from crux.resolver.families import thrash, thrash_wide
from crux.resolver.gen import random_problem
from crux.resolver.oracle import brute_force

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


def toy(*reqs):
    return Problem.from_requirements(Graph.from_dict(TOY), reqs or ("a", "b"))


def test_a_minimal_core_passes():
    p = toy()
    v = verify_core(p, explain(p).core)
    assert v.ok and v.unsat_by == "oracle"
    assert set(v.necessary_by.values()) == {"oracle"}


def test_a_redundant_member_is_named():
    p = thrash(3).problem
    core = list(explain(p).core) + [Constraint.root("f0")]
    v = verify_core(p, core)
    assert v.unsat is True and v.minimal is False and not v.ok
    assert v.redundant == (Constraint.root("f0"),)


def test_a_satisfiable_set_is_not_unsat():
    p = toy()
    core = [c for c in explain(p).core if c != Constraint.root("b")]
    v = verify_core(p, core)
    assert v.unsat is False and not v.ok


def test_the_whole_universe_is_unsat_but_not_minimal():
    p = thrash_wide(2).problem
    v = verify_core(p, p.constraints())
    assert v.unsat is True and v.minimal is False
    assert {c.dep for c in v.redundant} == {"f0", "f1"}


def test_a_foreign_constraint_is_refused():
    with pytest.raises(ValueError):
        verify_core(toy(), [Constraint.root("zzz")])


def test_without_the_oracle_sat_needs_a_checked_plan_and_unsat_two_solvers():
    p = toy()
    v = verify_core(p, explain(p).core, max_states=0)   # force the fallback
    assert v.ok and v.unsat_by == "solvers"
    assert set(v.necessary_by.values()) == {"plan"}


def test_the_fallback_agrees_with_the_oracle_on_random_instances():
    for s in range(200):
        p = random_problem(s)
        if brute_force(p).status == "SAT":
            continue
        core = explain(p).core
        for extra in ([], list(p.constraints())[:2]):
            k = list(dict.fromkeys(list(core) + extra))
            a = verify_core(p, k)
            b = verify_core(p, k, max_states=0)
            assert (a.unsat, a.minimal, set(a.redundant)) == \
                   (b.unsat, b.minimal, set(b.redundant)), s


def test_a_solver_plan_is_checked_not_trusted(monkeypatch):
    """The plan fallback must run the independent checker: a solver that
    claims SAT with an invalid plan gives no verdict and an error.  (Found by
    the mutation check: 'trust the plan unchecked' survived every other
    test, because the real solver never returns a bad plan.)"""
    import crux.explain.verify as verify
    from crux.resolver.problem import Result
    from packaging.version import Version

    real = verify.solve

    def lying(problem, config):
        r = real(problem, config)
        if r.status == "SAT":
            return Result("SAT", plan={"nosuch": Version("1.0")})
        return r

    monkeypatch.setattr(verify, "solve", lying)
    p = toy()
    v = verify_core(p, explain(p).core, max_states=0)
    assert not v.ok and v.errors
    assert v.minimal is None and len(v.unknown) == 11
    assert "plan" not in v.necessary_by.values()


def test_a_timeout_gives_unknown_not_a_verdict():
    p = thrash(3).problem
    v = verify_core(p, p.constraints(), max_states=0, timeout_s=0)
    assert v.unsat is None and v.unsat_by == "" and v.minimal is None
    assert not v.ok and v.unknown


def test_the_quick_size_check_agrees_with_the_oracles_search_space():
    from crux.explain.verify import _small_enough
    from crux.resolver.oracle import search_space
    from tests.resolver.test_edge_fuzz import edge_problem
    for s in range(300):
        p = random_problem(s) if s % 2 else edge_problem(s)
        n = search_space(p)
        for bound in (n - 1, n, n + 1, 1, 10**9):
            assert _small_enough(p, bound) == (n <= bound), (s, bound, n)
