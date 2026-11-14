"""Stage 1: brute-force oracle and random problem generator (offline)."""

import itertools
import json
import time
from pathlib import Path

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Constraint, Problem
from crux.resolver.checker import check_plan, is_valid
from crux.resolver.gen import random_problem
from crux.resolver.oracle import (
    OracleLimitExceeded, all_plans, brute_force, count_plans, search_space,
)

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


def prob(d, *reqs, disabled=()):
    return Problem.from_requirements(Graph.from_dict(d), reqs,
                                     disabled=frozenset(disabled))


# ── hand-made instances ──────────────────────────────────────────────

def test_toy_abc_is_unsat():
    r = brute_force(prob(TOY, "a", "b"))
    assert (r.status, r.plan) == ("UNSAT", None)
    assert r.states == search_space(prob(TOY, "a", "b"))   # looked at everything


def test_toy_single_root_plans_are_counted_exactly():
    # a: 5 versions, each needs c<2.0 -> c in {1.0, 1.1, 1.2}
    assert count_plans(prob(TOY, "a")) == 5 * 3
    # b: 4 versions, each needs c>=3.1 -> c in {3.1, 3.2}
    assert count_plans(prob(TOY, "b")) == 4 * 2


def test_first_plan_is_newest_and_valid():
    p = prob(TOY, "a")
    r = brute_force(p)
    assert r.status == "SAT"
    assert r.plan == {"a": Version("1.4"), "c": Version("1.2")}
    assert is_valid(p, r.plan)


def test_empty_problem_has_exactly_the_empty_plan():
    assert list(all_plans(prob(TOY))) == [{}]
    assert brute_force(prob(TOY)).plan == {}


def test_inactive_packages_do_not_multiply_plans():
    d = {"a": {"1": [], "2": []}, "z": {"1": [], "2": [], "3": []}}
    # z is not in the closure at all; and a plan never mentions it
    assert count_plans(prob(d, "a")) == 2


def test_package_reachable_only_through_some_versions():
    d = {"a": {"1": [], "2": [["x", ""]]}, "x": {"1": [], "2": []}}
    plans = list(all_plans(prob(d, "a")))
    # a=1 alone (x inactive, counted once), a=2 with x=1, a=2 with x=2
    assert len(plans) == 3
    assert {"a": Version("1")} in plans


def test_dangling_dependency_forces_other_versions():
    d = {"a": {"1": [["ghost", ""]], "2": []}}
    assert [p for p in all_plans(prob(d, "a"))] == [{"a": Version("2")}]
    assert brute_force(prob({"a": {"1": [["ghost", ""]]}}, "a")).status == \
        "UNSAT"


def test_unknown_root_package_is_unsat():
    assert brute_force(prob(TOY, "nope")).status == "UNSAT"


def test_cycle():
    d = {"a": {"1": [["b", ""]]}, "b": {"1": [["a", ""]]}}
    assert count_plans(prob(d, "a")) == 1


def test_every_constraint_on_a_shared_dependency_must_hold():
    d = {"app": {"1": [["l", ""], ["r", ""]]},
         "l": {"1": [["base", "<2"]]}, "r": {"1": [["base", ">=2"]]},
         "base": {"1": [], "2": []}}
    assert brute_force(prob(d, "app")).status == "UNSAT"


def test_disabled_constraints():
    p = prob(TOY, "a", "b", disabled={Constraint.root("b")})
    assert brute_force(p).status == "SAT"
    edge = Constraint.edge("a", "1.4", "c", "<2.0")
    q = prob(TOY, "a", disabled={edge})
    # a=1.4 needs nothing now and c stays inactive: plan {a: 1.4}
    assert {"a": Version("1.4")} in list(all_plans(q))


def test_disabling_either_toy_root_makes_it_satisfiable():
    for dropped in ("a", "b"):
        p = prob(TOY, "a", "b", disabled={Constraint.root(dropped)})
        assert brute_force(p).status == "SAT"


def test_prerelease_rule():
    d = {"p": {"1.0": [], "2.0b1": []}, "q": {"1.0b1": [], "1.0b2": []}}
    assert [x for x in all_plans(prob(d, "p"))] == [{"p": Version("1.0")}]
    assert count_plans(prob(d, "q")) == 2
    assert count_plans(prob(d, "p>=2.0b1")) == 1


def test_limit_is_enforced():
    p = prob(TOY, "a", "b")
    with pytest.raises(OracleLimitExceeded):
        brute_force(p, max_states=10)
    with pytest.raises(OracleLimitExceeded):
        list(all_plans(p, max_states=10))


# ── oracle vs checker: two independent definitions must agree ────────

def _checker_enumeration(problem):
    """All valid plans by trying every version-or-absent combination and
    asking the CHECKER.  Slow, uses none of the oracle's code."""
    g = problem.graph
    pkgs, seen = [], set()
    todo = [c.dep for c in problem.active_roots()]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        pkgs.append(p)
        for v in g.versions(p):
            for dep, spec in g.deps(p, v):
                if Constraint.edge(p, v, dep, spec) not in problem.disabled:
                    todo.append(dep)
    pkgs.sort()
    found = set()
    options = [[None] + list(g.versions(p)) for p in pkgs]
    for combo in itertools.product(*options):
        plan = {p: v for p, v in zip(pkgs, combo) if v is not None}
        if is_valid(problem, plan):
            found.add(tuple((p, str(v)) for p, v in sorted(plan.items())))
    return found


def _as_keys(plans):
    return {tuple((p, str(v)) for p, v in sorted(pl.items())) for pl in plans}


def _small(seed):
    # light dependencies so that many instances actually have plans
    return random_problem(seed, max_packages=4, max_versions=3,
                          max_states=400, max_deps=1)


def test_cross_check_instances_are_not_mostly_empty():
    counts = [count_plans(_small(s)) for s in range(240)]
    assert sum(c > 0 for c in counts) >= 100      # plans exist
    assert sum(c > 1 for c in counts) >= 40       # several plans, so dedupe
    assert sum(c == 0 for c in counts) >= 20      # and real UNSAT too


@pytest.mark.parametrize("seed", range(240))
def test_oracle_matches_checker_enumeration(seed):
    p = _small(seed)
    assert _as_keys(all_plans(p)) == _checker_enumeration(p)


@pytest.mark.parametrize("seed", range(240))
def test_oracle_matches_checker_enumeration_with_disabled(seed):
    p = _small(seed + 5000)
    edges = [Constraint.edge(pk, v, dep, spec)
             for pk in sorted({c.dep for c in p.roots})
             for v in p.graph.versions(pk)
             for dep, spec in p.graph.deps(pk, v)]
    q = p.with_disabled(set(edges[::2]) | set(p.roots[:1]))
    assert _as_keys(all_plans(q)) == _checker_enumeration(q)


def test_every_oracle_plan_passes_the_checker():
    for seed in range(150):
        p = random_problem(seed)
        for plan in itertools.islice(all_plans(p), 20):
            assert check_plan(p, plan) == [], (seed, plan)


# ── the generator ────────────────────────────────────────────────────

def test_generator_is_deterministic():
    a, b = random_problem(7), random_problem(7)
    assert a.roots == b.roots
    for pkg in {c.dep for c in a.roots}:
        assert a.graph.versions(pkg) == b.graph.versions(pkg)


def test_generator_respects_size_limits():
    for seed in range(100):
        p = random_problem(seed)
        assert search_space(p) <= 50_000
        assert p.roots


def test_generator_covers_sat_unsat_and_prereleases():
    verdicts = [brute_force(random_problem(s)).status for s in range(300)]
    sat = verdicts.count("SAT")
    assert 40 < sat < 260, f"degenerate mix: {sat} SAT of 300"
    pre = any(v.is_prerelease for s in range(100)
              for pk in {c.dep for c in random_problem(s).roots}
              for v in random_problem(s).graph.versions(pk))
    assert pre


def test_oracle_is_fast_enough_for_a_thousand_instances():
    t0 = time.perf_counter()
    for seed in range(1000):
        brute_force(random_problem(seed))
    # about 10 s on an idle machine; the bound is loose on purpose so that a
    # busy machine does not make it flake
    assert time.perf_counter() - t0 < 600
