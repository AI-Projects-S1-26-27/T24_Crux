"""C1: AC-3 preprocessing (offline)."""

import itertools

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Config, Constraint, Problem, solve
from crux.resolver.ac3 import ACTimeout, ac3
from crux.resolver.families import chain, pinned_pair, thrash
from crux.resolver.gen import random_problem
from crux.resolver.oracle import all_plans, brute_force

C0, C1 = Config.named("C0"), Config.named("C1")


def prob(d, *reqs, disabled=()):
    return Problem.from_requirements(Graph.from_dict(d), reqs,
                                     disabled=frozenset(disabled))


def V(*xs):
    return {Version(x) for x in xs}


# ── hand-made fixed points ───────────────────────────────────────────

SUPPORT = {"x": {"1": [["y", ">=1"]], "2": [["y", ">=2"]], "3": [["y", ">=3"]]},
           "y": {"1": [], "2": [], "3": []}}


def test_unsupported_versions_are_pruned():
    r = ac3(prob(SUPPORT, "x", "y<=1"))
    assert r.domains == {"x": V("1"), "y": V("1")}
    assert r.pruned == 2 and r.versions == 4 and not r.wiped
    assert r.used == {Constraint.edge("x", "2", "y", ">=2"),
                      Constraint.edge("x", "3", "y", ">=3")}


def test_root_restriction_is_not_counted_as_ac3_work():
    d = {"a": {"1": [], "2": [], "3": []}}
    r = ac3(prob(d, "a>=2"))
    assert r.domains == {"a": V("2", "3")}
    assert (r.pruned, r.versions) == (0, 2)


def test_pruning_cascades_and_can_wipe_out_a_root():
    d = {"a": {"1": [["b", ">=2"]]},
         "b": {"1": [["c", ">=1"]], "2": [["c", ">=3"]]},
         "c": {"1": [], "2": []}}
    p = prob(d, "a")
    r = ac3(p)
    assert r.wiped and r.pruned == 2
    assert r.domains["a"] == set() and r.domains["b"] == V("1")
    sol = solve(p, C1)
    assert sol.status == "UNSAT" and sol.stats.nodes == 0     # no search at all
    assert set(sol.conflict) == {Constraint.root("a")} | r.used
    assert brute_force(p).status == "UNSAT"


def test_a_dependency_with_no_releases_prunes_its_dependents():
    d = {"a": {"2": [["ghost", ""]], "1": []}}     # newest release is dead
    r = ac3(prob(d, "a"))
    assert r.domains["a"] == V("1") and r.pruned == 1
    assert solve(prob(d, "a"), C1).stats.nodes == 1           # C0 needs 2
    assert solve(prob(d, "a"), C0).stats.nodes == 2


def test_unknown_root_package_wipes_out_immediately():
    p = prob({"a": {"1": []}}, "nope")
    assert ac3(p).wiped
    assert solve(p, C1).status == "UNSAT"


def test_disabled_constraints_do_not_prune():
    d = {"a": {"1": [["b", ">=2"]]},
         "b": {"1": [["c", ">=1"]], "2": [["c", ">=3"]]},
         "c": {"1": [], "2": []}}
    off = Constraint.edge("b", "2", "c", ">=3")
    p = prob(d, "a", disabled={off})
    r = ac3(p)
    assert not r.wiped and r.pruned == 0
    sol = solve(p, C1)
    assert sol.status == "SAT"
    assert sol.plan == {"a": Version("1"), "b": Version("2")}   # c not needed


def test_timeout_inside_propagation():
    p = prob(SUPPORT, "x", "y<=1")
    with pytest.raises(ACTimeout):
        ac3(p, deadline=0.0)
    r = solve(p, Config.named("C1", timeout_s=0.0))
    assert r.status == "TIMEOUT" and r.plan is None


def test_the_deadline_is_honoured_promptly_while_building_the_closure():
    """Regression.  On the real pilot graph a cold AC-3 run overran a 10 s
    timeout to 18.5 s, because the deadline was only checked in the
    propagation loop, not while the closure (and its Graph.allowed() sets)
    was being built.  pinned_pair(3000) makes that closure expensive: 3,000
    distinct exact pins, each filtered over 3,000 versions."""
    import time
    p = pinned_pair(3000).problem
    t0 = time.perf_counter()
    r = solve(p, Config.named("C1", timeout_s=0.2))
    elapsed = time.perf_counter() - t0
    assert r.status == "TIMEOUT"
    # generous for a busy machine; the unfixed code took the whole closure
    # build (several seconds) before it noticed
    assert elapsed < 2.0, elapsed


# ── what it does and does not buy, on the families ───────────────────

def test_pinned_pair_collapses_to_two_nodes():
    for m in (10, 50, 200):
        r = solve(pinned_pair(m).problem, C1)
        assert r.status == "SAT"
        assert r.stats.nodes == 2
        assert (r.stats.ac3_pruned, r.stats.ac3_versions) == (m - 1, m + 1)
        assert solve(pinned_pair(m).problem, C0).stats.nodes == m + 1


def test_thrash_and_chain_are_not_helped():
    # AC-3 cannot see a conflict among unrelated choices; a documented limit
    for case in (thrash(4), chain(30)):
        a, b = solve(case.problem, C0), solve(case.problem, C1)
        assert b.stats.nodes == a.stats.nodes
        assert b.stats.ac3_pruned == 0


# ── properties on random instances ───────────────────────────────────

def _naive_ac(problem):
    """Slow, independent fixed point straight from the definition."""
    g = problem.graph
    cons = problem.constraints()
    pkgs = {c.dep for c in cons}
    dom = {p: set(g.versions(p)) for p in pkgs}
    for r in problem.active_roots():
        dom[r.dep] &= g.allowed(r.dep, r.specifier)
    changed = True
    while changed:
        changed = False
        for c in cons:
            if c.kind != "edge":
                continue
            v = Version(c.version)
            if v in dom[c.pkg] and not (
                    dom[c.dep] & g.allowed(c.dep, c.specifier)):
                dom[c.pkg].discard(v)
                changed = True
    return dom


@pytest.mark.parametrize("seed", range(150))
def test_domains_equal_the_naive_fixed_point(seed):
    p = random_problem(seed)
    assert ac3(p).domains == _naive_ac(p)


def test_no_solution_is_ever_lost():
    checked = 0
    wiped = 0
    for seed in range(250):
        p = random_problem(seed, max_packages=5, max_versions=4,
                           max_states=3000, max_deps=1 + seed % 2)
        r = ac3(p)
        wiped += r.wiped
        plans = list(all_plans(p))
        if r.wiped:
            assert plans == [], f"seed {seed}"
        for plan in plans:
            for pkg, v in plan.items():
                assert v in r.domains[pkg], (seed, pkg, v)
            checked += 1
    # measured: 294 plans checked, 143 of 250 instances wiped out; the bounds
    # only guard against the generator drifting into something trivial
    assert checked > 250 and wiped > 50, (checked, wiped)


def test_survivors_all_have_support():
    for seed in range(150):
        p = random_problem(seed)
        r = ac3(p)
        g = p.graph
        for pkg, dom in r.domains.items():
            for v in dom:
                for dep, spec in g.deps(pkg, v):
                    if Constraint.edge(pkg, v, dep, spec) in p.disabled:
                        continue
                    assert r.domains[dep] & g.allowed(dep, spec), (seed, pkg, v)


def test_c1_never_explores_more_nodes_than_c0():
    for seed in range(300):
        p = random_problem(seed)
        a, b = solve(p, C0), solve(p, C1)
        assert a.status == b.status, seed
        assert b.stats.nodes <= a.stats.nodes, seed


def test_ac3_actually_prunes_on_random_instances():
    pruned = sum(ac3(random_problem(s)).pruned > 0 for s in range(300))
    assert pruned > 30, pruned          # otherwise these tests prove little


def test_c1_matches_c0_on_disabled_subsets():
    for seed in range(200):
        p = random_problem(seed + 7000, max_packages=5, max_versions=4,
                           max_states=3000)
        cons = p.constraints()
        for k in (2, 3):
            q = p.with_disabled(set(cons[seed % k::k]))
            assert solve(q, C1).status == brute_force(q).status
