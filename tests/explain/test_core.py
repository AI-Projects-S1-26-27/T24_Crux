"""Core extraction (crux/explain/core.py), judged by the independent verifier
and the brute-force oracle.

What must hold for every method:
  * the core is a subset of the problem's enabled constraints;
  * it is UNSAT on its own and every member is necessary (verify_core, which
    uses the oracle on these sizes);
  * ``minimal`` is True only when that was proven, never on a timeout.
"""

import itertools
import json
import random
from pathlib import Path

import pytest

from crux.data.graph import Graph
from crux.explain import METHODS, explain, extract_core, is_unsat, verify_core
from crux.explain.core import DEFAULT_SOLVE
from crux.resolver import Config, Constraint, Problem, solve
from crux.resolver.families import pinned_pair, thrash, thrash_wide
from crux.resolver.gen import random_problem
from crux.resolver.oracle import brute_force
from tests.resolver.test_edge_fuzz import edge_problem

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


@pytest.fixture(params=METHODS)
def method(request):
    return request.param


def toy(*reqs, disabled=()):
    return Problem.from_requirements(Graph.from_dict(TOY), reqs or ("a", "b"),
                                     disabled)


def oracle_status(problem, subset, universe):
    p = problem.with_disabled(problem.disabled | (set(universe) - set(subset)))
    return brute_force(p).status


# ── known cores ──────────────────────────────────────────────────────

TOY_CORE = {Constraint.root("a"), Constraint.root("b")} \
    | {Constraint.edge("a", f"1.{i}", "c", "<2.0") for i in range(5)} \
    | {Constraint.edge("b", f"1.{i}", "c", ">=3.1") for i in range(4)}


def test_toy_core_is_both_roots_and_every_edge_to_c(method):
    # Every version of a must be pinned to c<2 (a version without that edge
    # would be a way out), so the minimal core has 2 + 5 + 4 = 11 members.
    # The proposal's two-line message is the renderer grouping these.
    r = explain(toy(), method=method)
    assert r.status == "UNSAT" and r.minimal
    assert set(r.core) == TOY_CORE


def test_toy_core_from_the_whole_universe(method):
    p = toy()
    r = extract_core(p, p.constraints(), method=method)
    assert set(r.core) == TOY_CORE and r.minimal


@pytest.mark.parametrize("n", [1, 3, 6])
def test_thrash_core_is_the_two_culprits_and_their_edges(method, n):
    expected = {Constraint.root("x"), Constraint.root("z"),
                Constraint.edge("x", "1", "y", "<2"),
                Constraint.edge("z", "1", "y", ">=3")}
    p = thrash(n).problem
    for start in (None, p.constraints()):        # the free roots must go
        r = extract_core(p, start, method=method)
        assert set(r.core) == expected and r.minimal


@pytest.mark.parametrize("n", [1, 4, 8])
def test_thrash_wide_core_is_eight_constraints_at_any_size(method, n):
    expected = {Constraint.root("x"), Constraint.root("z")} \
        | {Constraint.edge("x", v, "y", "<2") for v in "123"} \
        | {Constraint.edge("z", v, "y", ">=3") for v in "123"}
    p = thrash_wide(n).problem
    r = extract_core(p, p.constraints(), method=method)
    assert set(r.core) == expected and r.minimal


def test_a_root_on_a_missing_package_is_a_core_of_one(method):
    p = Problem.from_requirements(Graph.from_dict(TOY), ["a", "nosuch"])
    r = explain(p, method=method)
    assert r.core == (Constraint.root("nosuch"),) and r.minimal


def test_a_root_whose_own_range_is_empty_is_a_core_of_one(method):
    r = explain(toy("a", "c>=9"), method=method)
    assert r.core == (Constraint.root("c", ">=9"),)


def test_a_satisfiable_problem_has_no_core(method):
    r = explain(toy("a"), method=method)
    assert r.status == "SAT" and r.core == () and not r.minimal
    with pytest.raises(ValueError):
        extract_core(toy("a"), method=method)


# ── random instances, verified by the oracle ─────────────────────────

def _check(p, r):
    assert r.status == "UNSAT"
    assert r.minimal
    assert set(r.core) <= set(p.constraints())
    assert not (set(r.core) & p.disabled)
    v = verify_core(p, r.core)
    assert v.ok, (v.unsat, v.redundant, v.unknown, v.errors)
    assert v.unsat_by == "oracle"


def test_every_random_unsat_core_is_minimal_by_the_oracle(method):
    """The proposal's correctness bar: random small instances, seeds 0-999."""
    n = 0
    for s in range(1000):
        p = random_problem(s)
        r = explain(p, method=method)
        if r.status == "SAT":
            continue
        n += 1
        _check(p, r)
    assert n > 600, n                    # about 70% of seeds are UNSAT


@pytest.mark.parametrize("start", ["C0", "C4", "universe"])
def test_any_unsat_starting_set_gives_a_minimal_core(method, start):
    n = 0
    for s in range(200):
        p = random_problem(s)
        if start == "universe":
            st = p.constraints()
        else:
            st = solve(p, Config.named(start))
            if st.status != "UNSAT":
                continue
            st = st.conflict
        if brute_force(p).status == "SAT":
            continue
        n += 1
        _check(p, extract_core(p, st, method=method))
    assert n > 100


def test_edge_case_instances_including_disabled_constraints(method):
    """Pre-release-only packages, ``!=`` against pre-releases, self
    dependencies, duplicate roots, ghosts, and some constraints already
    disabled by the caller (the core must never contain those)."""
    n = 0
    for s in range(600):
        p = edge_problem(s)
        if brute_force(p).status == "SAT":
            continue
        n += 1
        _check(p, explain(p, method=method))
    assert n > 150, n


def test_the_core_is_one_of_the_minimal_cores_found_by_enumeration():
    """Enumerate EVERY subset of the universe on tiny instances with the
    oracle: the extracted core must be among the minimal UNSAT subsets."""
    checked = 0
    for s in range(400):
        p = random_problem(s, max_packages=3, max_versions=3)
        u = p.constraints()
        if len(u) > 10 or brute_force(p).status == "SAT":
            continue
        unsat = {frozenset(sub) for k in range(len(u) + 1)
                 for sub in itertools.combinations(u, k)
                 if oracle_status(p, sub, u) == "UNSAT"}
        muses = {m for m in unsat
                 if not any(o < m for o in unsat)}
        for m in METHODS:
            assert frozenset(explain(p, method=m).core) in muses, (s, m)
        checked += 1
    assert checked > 50, checked


def test_unsatisfiability_is_monotone():
    """The property both algorithms rest on: if S is a subset of S' and S'
    has a plan, S has one."""
    rng = random.Random(0)
    pairs = 0
    for s in range(300):
        p = random_problem(s)
        u = list(p.constraints())
        big = [c for c in u if rng.random() < 0.7]
        small = [c for c in big if rng.random() < 0.6]
        if oracle_status(p, big, u) == "SAT":
            assert oracle_status(p, small, u) == "SAT", s
            pairs += 1
    assert pairs > 50


# ── cost and the methods' differences ────────────────────────────────

def test_deletion_makes_exactly_one_call_per_starting_constraint():
    for s in range(100):
        p = random_problem(s)
        if brute_force(p).status == "SAT":
            continue
        u = p.constraints()
        r = extract_core(p, u, method="deletion", check_start=False)
        assert r.calls == len(u) == r.start_size


def test_refinement_never_costs_more_calls_than_plain_deletion():
    fewer = 0
    for s in range(200):
        p = random_problem(s)
        if brute_force(p).status == "SAT":
            continue
        u = p.constraints()
        a = extract_core(p, u, method="deletion", check_start=False)
        b = extract_core(p, u, method="deletion+refine", check_start=False)
        assert b.calls <= a.calls
        fewer += b.calls < a.calls
    assert fewer > 50, fewer


def test_quickxplain_needs_few_calls_for_a_small_core_in_a_big_set():
    p = thrash(40).problem                     # 44 constraints, core of 4
    u = p.constraints()
    d = extract_core(p, u, method="deletion", check_start=False)
    q = extract_core(p, u, method="quickxplain", check_start=False)
    assert set(d.core) == set(q.core)
    assert d.calls == len(u) == 44
    assert q.calls <= 25, q.calls              # measured 16; 2k log2(n/k) + 2k = 35


def test_calls_add_up():
    for m in METHODS:
        r = explain(toy(), method=m)
        assert r.calls == r.sat_calls + r.unsat_calls + r.timeouts
        assert r.timeouts == 0 and r.solver_s <= r.wall_s


def test_shuffled_starting_order_still_gives_a_minimal_core(method):
    for s in range(150):
        p = random_problem(s)
        if brute_force(p).status == "SAT":
            continue
        st = list(p.constraints())
        random.Random(s).shuffle(st)
        _check(p, extract_core(p, st, method=method))


def test_extraction_is_deterministic(method):
    for s in range(50):
        p = random_problem(s)
        a, b = explain(p, method=method), explain(p, method=method)
        assert a.core == b.core and a.calls == b.calls


# ── timeouts, budget and bad input ───────────────────────────────────

def test_a_per_call_timeout_keeps_everything_and_is_not_called_minimal(method):
    p = thrash(4).problem
    u = p.constraints()
    r = extract_core(p, u, method=method, check_start=False,
                     config=Config(timeout_s=0))
    assert not r.minimal and r.timeouts == r.calls > 0
    assert set(r.core) == set(u)               # nothing could be removed


def test_an_exhausted_budget_returns_an_unsat_set_not_called_minimal(method):
    p = thrash(4).problem
    u = p.constraints()
    r = extract_core(p, u, method=method, check_start=False, budget_s=0)
    assert r.budget_exhausted and not r.minimal and r.calls == 0
    assert set(r.core) == set(u)
    assert is_unsat(p, r.core) == "UNSAT"


def test_deletion_returns_its_progress_when_the_budget_runs_out(monkeypatch):
    # Let exactly three solver calls through, then exhaust the budget.
    import crux.explain.core as core
    real = core._Oracle.check
    calls = {"n": 0}

    def limited(self, subset):
        calls["n"] += 1
        if calls["n"] > 3:
            raise core._BudgetExhausted
        return real(self, subset)

    monkeypatch.setattr(core._Oracle, "check", limited)
    p = thrash(6).problem
    u = p.constraints()
    r = extract_core(p, u, method="deletion", check_start=False)
    assert r.budget_exhausted and not r.minimal
    assert len(r.core) == len(u) - 3           # the first 3 were free roots
    monkeypatch.undo()
    assert is_unsat(p, r.core) == "UNSAT"


def test_bad_input_is_refused():
    p = toy()
    with pytest.raises(ValueError):
        extract_core(p, [Constraint.root("a")])           # satisfiable
    with pytest.raises(ValueError):
        extract_core(p, [Constraint.root("zzz")])         # not in the problem
    with pytest.raises(ValueError):
        extract_core(p, [], check_start=False)
    with pytest.raises(ValueError):
        extract_core(p, method="magic")


def test_a_satisfiable_family_is_reported_as_such():
    assert explain(pinned_pair(20).problem).status == "SAT"


def test_default_start_is_learning_without_ac3():
    # docs/EXPLAIN.md: with AC-3 on, learning.py adds all of AC-3's used edges
    # to the conflict, which makes the C4 preset's conflict a poor start.
    assert DEFAULT_SOLVE.learning and not DEFAULT_SOLVE.ac3


def test_oracle_configuration_depends_on_the_method():
    # deletion and quickxplain only need SAT/UNSAT: AC-3 + MRV + learning
    # (AC-3 avoids learning's ~8 s per call on real boto3; learning avoids
    # AC-3 + MRV's exponential thrash_wide).  deletion+refine uses the
    # conflict: learning without AC-3 (AC-3 inflates it).
    from crux.explain.core import default_oracle
    for m in ("deletion", "quickxplain"):
        c = default_oracle(m)
        assert c.ac3 and c.mrv and c.learning and not c.lcv
    c = default_oracle("deletion+refine", timeout_s=3)
    assert c.learning and not c.ac3 and c.timeout_s == 3
