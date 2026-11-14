"""Plain backtracking (C0) and AC-3 (C1), judged by the oracle and checker.

Every implemented configuration runs the same tests through the ``cfg``
fixture.  When a new configuration lands, add its name to the fixture."""

import json
import time
from pathlib import Path

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Config, Constraint, Problem, solve
from crux.resolver.checker import check_plan
from crux.resolver.gen import random_problem
from crux.resolver.oracle import all_plans, brute_force

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())
_CONFIGS = {
    "C0": Config.named("C0"), "C1": Config.named("C1"),
    "C2": Config.named("C2"), "C3": Config.named("C3"),
    "C4": Config.named("C4"),
    # the flags are independent, so each must also work on its own
    "mrv-only": Config(mrv=True), "lcv-only": Config(lcv=True),
    "learning-only": Config(learning=True),
    "backjump-only": Config(learning=True, nogoods=False),
    "learning+ac3": Config(learning=True, ac3=True),
}


@pytest.fixture(params=list(_CONFIGS))
def cfg(request):
    """Every implemented configuration must pass the same correctness bar."""
    return _CONFIGS[request.param]


def prob(d, *reqs, disabled=()):
    return Problem.from_requirements(Graph.from_dict(d), reqs,
                                     disabled=frozenset(disabled))


# ── hand-made instances ──────────────────────────────────────────────

def test_toy_abc_unsat_with_both_roots_in_the_conflict(cfg):
    r = solve(prob(TOY, "a", "b"), cfg)
    assert r.status == "UNSAT" and r.plan is None
    assert {Constraint.root("a"), Constraint.root("b")} <= set(r.conflict)
    assert r.stats.nodes > 0 and r.stats.backtracks > 0


def test_toy_single_root_picks_newest_and_passes_checker(cfg):
    p = prob(TOY, "a")
    r = solve(p, cfg)
    assert r.status == "SAT" and r.conflict is None
    assert r.plan == {"a": Version("1.4"), "c": Version("1.2")}
    assert check_plan(p, r.plan) == []


def test_empty_problem_is_sat_with_empty_plan(cfg):
    r = solve(prob(TOY), cfg)
    assert (r.status, r.plan) == ("SAT", {})


def test_unknown_root_package_is_unsat(cfg):
    assert solve(prob(TOY, "nope"), cfg).status == "UNSAT"


def test_dangling_dependency_is_skipped_for_another_version(cfg):
    d = {"a": {"1": [["ghost", ""]], "2": []}}
    r = solve(prob(d, "a"), cfg)
    assert r.plan == {"a": Version("2")}


def test_cycle(cfg):
    d = {"a": {"1": [["b", ""]]}, "b": {"1": [["a", ""]]}}
    assert solve(prob(d, "a"), cfg).plan == {"a": Version("1"),
                                           "b": Version("1")}


def test_must_backtrack_to_find_the_solution(cfg):
    # newest a=2 needs x>=2, but b (also required) forces x<2: a=2 is a dead
    # end found only after b is assigned, so the search has to undo it.
    d = {"a": {"1": [["x", ""]], "2": [["x", ">=2"]]},
         "b": {"1": [["x", "<2"]]},
         "x": {"1": [], "2": []}}
    p = prob(d, "a", "b")
    r = solve(p, cfg)
    assert r.plan == {"a": Version("1"), "b": Version("1"),
                      "x": Version("1")}
    if cfg.lcv:
        # LCV sees that a=2 would rule out every option for x and tries a=1
        # first, so it finds the plan without ever undoing a choice
        assert r.stats.backtracks == 0
    else:
        assert r.stats.backtracks >= 1
    assert check_plan(p, r.plan) == []


def test_deep_chain_does_not_hit_the_recursion_limit(cfg):
    n = 3000
    d = {f"p{i}": {"1": [[f"p{i + 1}", ""]] if i < n - 1 else []}
         for i in range(n)}
    r = solve(prob(d, "p0"), cfg)
    assert r.status == "SAT" and len(r.plan) == n


def test_disabled_constraints_are_honoured(cfg):
    p = prob(TOY, "a", "b", disabled={Constraint.root("b")})
    r = solve(p, cfg)
    assert r.status == "SAT" and set(r.plan) == {"a", "c"}
    edge = Constraint.edge("a", "1.4", "c", "<2.0")
    r2 = solve(prob(TOY, "a", disabled={edge}), cfg)
    assert r2.plan == {"a": Version("1.4")}          # c is never activated


def test_timeout_returns_a_result_not_an_exception():
    r = solve(prob(TOY, "a", "b"), Config(timeout_s=0.0))
    assert r.status == "TIMEOUT"
    assert r.plan is None and r.conflict is None


def test_stats_are_recorded(cfg):
    r = solve(prob(TOY, "a"), cfg)
    assert r.stats.nodes == 2 and r.stats.backtracks == 0
    assert r.stats.wall_s > 0


def test_solver_is_deterministic(cfg):
    p = random_problem(11)
    a, b = solve(p, cfg), solve(p, cfg)
    assert (a.status, a.plan, a.conflict) == (b.status, b.plan, b.conflict)
    assert a.stats.nodes == b.stats.nodes


# ── the correctness target: agree with the oracle on 1,000 instances ─

def test_agrees_with_the_oracle_on_1000_random_instances(cfg):
    # no wall-clock assertion here: a correctness test must not flake when
    # the machine is busy
    sat = unsat = 0
    for seed in range(1000):
        p = random_problem(seed)
        r = solve(p, cfg)
        want = brute_force(p).status
        assert r.status == want, f"seed {seed}: cfg {r.status}, oracle {want}"
        if r.status == "SAT":
            sat += 1
            assert check_plan(p, r.plan) == [], f"seed {seed}"
        else:
            unsat += 1
    assert sat > 200 and unsat > 200, (sat, unsat)


def test_sat_plans_are_among_the_oracle_plans(cfg):
    for seed in range(200):
        p = random_problem(seed, max_packages=5, max_versions=4,
                           max_states=2000, max_deps=1)
        r = solve(p, cfg)
        if r.status == "SAT":
            keys = {tuple((k, str(v)) for k, v in pl.items())
                    for pl in all_plans(p)}
            assert tuple((k, str(v)) for k, v in sorted(r.plan.items())) \
                in keys


def test_agrees_with_the_oracle_when_constraints_are_disabled(cfg):
    n_sat = n_unsat = 0
    for seed in range(400):
        p = random_problem(seed + 9000, max_packages=5, max_versions=4,
                           max_states=3000)
        cons = p.constraints()
        q = p.with_disabled(set(cons[seed % 3::3]))
        r, want = solve(q, cfg), brute_force(q).status
        assert r.status == want, f"seed {seed}"
        if r.status == "SAT":
            n_sat += 1
            assert check_plan(q, r.plan) == []
        else:
            n_unsat += 1
    assert n_sat > 50 and n_unsat > 50, (n_sat, n_unsat)


# ── the UNSAT conflict is sound ──────────────────────────────────────

def test_constraint_universe_of_toy():
    cons = prob(TOY, "a").constraints()
    assert cons[0] == Constraint.root("a")
    assert len(cons) == 1 + 5                       # root + one edge per a
    assert len(set(cons)) == len(cons)


def test_conflict_alone_is_still_unsatisfiable(cfg):
    """Disable every constraint outside the conflict; the oracle must still
    say UNSAT.  That is what 'sound' means."""
    checked = 0
    for seed in range(400):
        p = random_problem(seed)
        r = solve(p, cfg)
        if r.status != "UNSAT":
            continue
        universe = set(p.constraints())
        assert set(r.conflict) <= universe, f"seed {seed}"
        restricted = p.with_disabled(universe - set(r.conflict))
        assert brute_force(restricted).status == "UNSAT", f"seed {seed}"
        checked += 1
    assert checked > 100
