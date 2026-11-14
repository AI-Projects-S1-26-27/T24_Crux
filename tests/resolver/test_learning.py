"""C4: conflict-directed backjumping with nogoods (offline).

The oracle tests in test_backtrack.py prove C4 never changes an answer and
that its conflicts are sound.  These tests pin what C4 is FOR: the search
effort it saves, the tightness of its UNSAT conflict, and the one subtle bug
it had (see test_activating_decision_is_blamed).
"""

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Config, Constraint, Problem, solve
from crux.resolver import learning
from crux.resolver.checker import check_plan
from crux.resolver.families import chain, pinned_pair, random_csp, thrash_wide
from crux.resolver.gen import random_problem
from crux.resolver.learning import LearningSearch

C0, C3, C4 = (Config.named(n) for n in ("C0", "C3", "C4"))
LEARN = Config(learning=True)
BJ_ONLY = Config(learning=True, nogoods=False)


def prob(d, *reqs):
    return Problem.from_requirements(Graph.from_dict(d), reqs)


def between(n, k=3):
    """thrash_wide with the free packages BETWEEN the two culprits, so that a
    jump has frames to skip."""
    d = {f"f{i}": {str(v): [] for v in range(1, k + 1)} for i in range(n)}
    d["x"] = {str(v): [["y", "<2"]] for v in range(1, k + 1)}
    d["z"] = {str(v): [["y", ">=3"]] for v in range(1, k + 1)}
    d["y"] = {"1": [], "2": [], "3": []}
    return prob(d, "x", *[f"f{i}" for i in range(n)], "z")


# ── what it saves ────────────────────────────────────────────────────

def test_thrash_wide_is_linear_for_c4_and_exponential_for_the_rest():
    for n in range(3, 13):
        r = solve(thrash_wide(n).problem, C4)
        assert r.status == "UNSAT" and r.stats.nodes == n + 7, n
    p = thrash_wide(7).problem
    assert solve(p, C3).stats.nodes > 50 * solve(p, C4).stats.nodes


def test_a_real_backjump_skips_the_free_packages_between_the_culprits():
    for n in (3, 6, 9):
        r = solve(between(n), C4)
        assert r.status == "UNSAT"
        assert r.stats.backjumps == 1 and r.stats.nodes == n + 7, n
    assert solve(between(7), C0).stats.nodes > 20_000


def test_backjumps_counts_only_jumps_that_skip_a_decision():
    # In thrash_wide the culprits come last, so every jump lands on the
    # adjacent frame; the saving there comes from the conflict set becoming
    # empty (the proof never needed the free packages), not from skipping.
    assert solve(thrash_wide(8).problem, C4).stats.backjumps == 0
    assert solve(between(8), C4).stats.backjumps == 1


def test_c4_never_uses_more_nodes_than_c3_on_random_csp():
    # empirical, not a theorem (the variable order is dynamic): measured on
    # these seeds C4 is never worse and wins 21 of 24
    wins = 0
    for s in range(24):
        p = random_csp(14, s).problem
        a, b = solve(p, C3).stats.nodes, solve(p, C4).stats.nodes
        assert b <= a, s
        wins += b < a
    assert wins >= 15, wins


def test_backjumping_never_uses_more_nodes_than_plain_search_same_order():
    # learning-only keeps C0's variable and value order, so any difference is
    # the backjumping and the nogoods alone
    worse = better = 0
    for s in range(300):
        p = random_problem(s)
        a, b = solve(p, C0).stats.nodes, solve(p, LEARN).stats.nodes
        worse += b > a
        better += b < a
    assert worse == 0, worse
    assert better > 30, better


def test_nogoods_add_something_beyond_backjumping():
    better = worse = 0
    for s in range(30):
        p = random_csp(14, s).problem
        a, b = solve(p, BJ_ONLY), solve(p, LEARN)
        assert a.status == b.status
        better += b.stats.nodes < a.stats.nodes
        worse += b.stats.nodes > a.stats.nodes
    assert worse == 0 and better >= 15, (better, worse)


# ── the UNSAT conflict ───────────────────────────────────────────────

def test_conflict_is_the_two_roots_and_six_edges_whatever_the_size():
    expected = {Constraint.root("x"), Constraint.root("z")} \
        | {Constraint.edge("x", v, "y", "<2") for v in "123"} \
        | {Constraint.edge("z", v, "y", ">=3") for v in "123"}
    for n in (3, 10, 40):
        r = solve(thrash_wide(n).problem, C4)
        assert set(r.conflict) == expected, n
        assert not any(c.dep.startswith("f") for c in r.conflict)


def test_conflict_is_never_larger_than_plain_searchs_and_usually_smaller():
    smaller = larger = 0
    for s in range(400):
        p = random_problem(s)
        a = solve(p, C0)
        if a.status != "UNSAT":
            continue
        b = solve(p, LEARN)
        smaller += len(b.conflict) < len(a.conflict)
        larger += len(b.conflict) > len(a.conflict)
    assert larger == 0, larger
    assert smaller > 150, smaller           # measured 223 of 287


def test_activating_decision_is_blamed():
    """Regression.  A package with no versions is a dead end by itself, but it
    only exists because a decision activated it.  The first version of C4
    blamed nothing, concluded 'no decision to revise' and answered UNSAT where
    a solution exists (34 of 1,000 random instances)."""
    d = {"p1": {"2": [["ghost", ""]], "1": []}}
    for cfg in (LEARN, BJ_ONLY, C4):
        assert solve(prob(d, "p1"), cfg).plan == {"p1": Version("1")}

    # the smallest random instance that exposed it (seed 811), verbatim
    d = {"p0": {"2.0": [], "1.0": [["ghost", ""], ["p1", "<5.0,>=3.0"],
                                   ["p1", "<2.0"]]},
         "p1": {"4.0": [["ghost", ""], ["p0", "<3.0"], ["p0", ""]],
                "3.0": [["p0", ">=4.0"], ["p0", "<5.0,>=5.0"]],
                "2.0": [], "1.0": []}}
    p = prob(d, "p1", "p0<4.0a1")
    for cfg in (LEARN, BJ_ONLY, C4):
        r = solve(p, cfg)
        assert r.status == "SAT" and check_plan(p, r.plan) == []

    # and when there really is no way out, the conflict names the activation
    only = {"p1": {"1": [["ghost", ""]]}}
    r = solve(prob(only, "p1"), LEARN)
    assert r.status == "UNSAT"
    assert set(r.conflict) == {Constraint.root("p1"),
                               Constraint.edge("p1", "1", "ghost", "")}


def test_a_version_excluded_by_two_decisions_blames_the_earlier_one():
    """Both x and z rule out y's only version.  Blaming the earlier decision
    makes the jump go further, and that is the whole point.  Blaming the later
    one is also sound, so no oracle test notices; measured, it costs 23% more
    nodes at random_csp(16) and 62% more at (20), worse on 39 of 50 seeds."""
    d = {"x": {"1": [["y", ">=2"]]}, "z": {"1": [["y", ">=2"]]},
         "y": {"1": []}}
    s = _search(d, ["x", "z"])
    s._assign("x", Version("1"))
    s.level["x"] = 0
    s._assign("z", Version("1"))
    s.level["z"] = 1
    D, K = s._explain("y", frozenset())
    assert D == {"x"}
    assert Constraint.edge("x", "1", "y", ">=2") in K
    assert Constraint.edge("z", "1", "y", ">=2") not in K


def test_a_root_requirement_is_preferred_to_any_decision():
    # y>=2 comes from the root AND from x; the root is a static fact, so no
    # decision needs revising on its account
    d = {"x": {"1": [["y", ">=2"]]}, "y": {"1": []}}
    s = _search(d, ["x", "y>=2"])
    s._assign("x", Version("1"))
    s.level["x"] = 0
    D, K = s._explain("y", frozenset())
    assert D == set()
    assert Constraint.root("y", ">=2") in K


# ── nogood bookkeeping ───────────────────────────────────────────────

def _search(d, reqs, **flags):
    s = LearningSearch(prob(d, *reqs), Config(learning=True, **flags))
    assert s._setup(float("inf")) is None
    return s


AB = {"a": {"1": [], "2": []}, "b": {"1": [], "2": []}}


def test_a_recorded_nogood_removes_the_value_once_the_rest_is_assigned():
    s = _search(AB, ["a", "b"])
    ma = s._assign("a", Version("1"))
    s.level["a"] = 0
    mb = s._assign("b", Version("1"))
    s.level["b"] = 1
    s._record({"a", "b"}, set())                   # (a=1, b=1) is infeasible
    s._undo(mb)
    assert s.domain("b") == (Version("2"),)        # a=1 still assigned
    s._undo(ma)
    assert s.domain("b") == (Version("2"), Version("1"))   # no longer applies
    s._assign("a", Version("2"))
    assert s.domain("b") == (Version("2"), Version("1"))   # other value of a


def test_a_single_member_nogood_removes_the_value_outright():
    s = _search(AB, ["a", "b"])
    mark = s._assign("a", Version("1"))
    s.level["a"] = 0
    s._record({"a"}, set())
    s._undo(mark)                                  # a is unassigned again
    assert s.domain("a") == (Version("2"),)


def test_nogoods_are_deduplicated_and_can_be_switched_off():
    s = _search(AB, ["a", "b"])
    s._assign("a", Version("1"))
    s.level["a"] = 0
    s._record({"a"}, set())
    s._record({"a"}, set())
    assert s.stats.nogoods == 1
    r = solve(thrash_wide(5).problem, BJ_ONLY)
    assert r.stats.nogoods == 0 and r.status == "UNSAT"
    assert solve(thrash_wide(5).problem, LEARN).stats.nogoods > 0


def test_the_nogood_cap_stops_recording_but_not_the_search(monkeypatch):
    monkeypatch.setattr(learning, "MAX_NOGOODS", 0)
    for s in range(60):
        p = random_problem(s)
        r = solve(p, LEARN)
        assert r.stats.nogoods == 0
        assert r.status == solve(p, C0).status


# ── the rest of the contract ─────────────────────────────────────────

def test_timeout_is_a_result_not_an_exception():
    r = solve(thrash_wide(6).problem, Config(learning=True, timeout_s=0.0))
    assert r.status == "TIMEOUT" and r.plan is None and r.conflict is None


def test_works_with_ac3_and_on_deep_chains():
    r = solve(pinned_pair(100).problem, C4)
    assert r.status == "SAT" and r.stats.nodes == 2
    r = solve(chain(3000).problem, C4)
    assert r.status == "SAT" and len(r.plan) == 3000


def test_learning_is_deterministic():
    p = random_csp(14, 5).problem
    a, b = solve(p, C4), solve(p, C4)
    assert (a.status, a.plan, a.conflict) == (b.status, b.plan, b.conflict)
    assert (a.stats.nodes, a.stats.nogoods, a.stats.backjumps) == \
           (b.stats.nodes, b.stats.nogoods, b.stats.backjumps)


def test_every_c4_variant_agrees_on_random_csp():
    for s in range(30):
        p = random_csp(12, s).problem
        want = solve(p, C0).status
        for cfg in (LEARN, BJ_ONLY, C4, Config(learning=True, mrv=True)):
            assert solve(p, cfg).status == want, (s, cfg)
