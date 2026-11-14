"""C2 (MRV) and C3 (LCV): the heuristics themselves (offline).

The oracle tests in test_backtrack.py prove these never change an answer.
They cannot prove the heuristics do what they claim: a deliberately bad
ordering is still correct.  So these tests check the ordering directly.
"""

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Config, Problem, solve
from crux.resolver.backtrack import Search
from crux.resolver.bench import non_latest_pins, run_case
from crux.resolver.families import chain, pinned_pair, random_csp, thrash
from crux.resolver.ordering import lcv_cost, lcv_order, mrv_select

C0, C1, C2, C3 = (Config.named(n) for n in ("C0", "C1", "C2", "C3"))


def prob(d, *reqs):
    return Problem.from_requirements(Graph.from_dict(d), reqs)


def search(d, reqs, **flags):
    """A Search with the roots already activated, ready to be queried."""
    s = Search(prob(d, *reqs), Config(**flags))
    for r in s.problem.active_roots():
        s._activate(r.dep, r.spec)
    return s


def vs(*xs):
    return tuple(Version(x) for x in xs)


# ── MRV: variable selection ──────────────────────────────────────────

SIZES = {"a": {"1": [], "2": [], "3": []},
         "b": {"1": []},
         "c": {"1": [], "2": []}}


def test_mrv_picks_the_smallest_domain_first():
    s = search(SIZES, ["a", "b", "c"], mrv=True)
    assert mrv_select(s) == "b"
    s._assign("b", Version("1"))
    assert mrv_select(s) == "c"
    s._assign("c", Version("1"))
    assert mrv_select(s) == "a"
    s._assign("a", Version("1"))
    assert mrv_select(s) is None


def test_mrv_ties_go_to_the_package_activated_first():
    d = {"x": {"1": [], "2": []}, "y": {"1": [], "2": []}}
    assert mrv_select(search(d, ["x", "y"], mrv=True)) == "x"
    assert mrv_select(search(d, ["y", "x"], mrv=True)) == "y"


def test_mrv_counts_only_admissible_versions():
    # a has 3 versions in total but the root allows one; c has 2, so a is
    # more constrained even though it is listed after c
    s = search(SIZES, ["c", "a==2"], mrv=True)
    assert mrv_select(s) == "a"
    # b>=9 admits nothing: an empty domain beats everything
    s2 = search(SIZES, ["c", "a==2", "b>=9"], mrv=True)
    assert mrv_select(s2) == "b"


def test_mrv_picks_an_empty_domain_first_even_if_activated_last():
    s = search(SIZES, ["a", "c"], mrv=True)
    s._activate("b", "<1")
    s._activate("b", ">=1")                          # contradictory: 0 versions
    assert mrv_select(s) == "b"


def test_plain_search_keeps_activation_order():
    s = search(SIZES, ["a", "b", "c"])               # mrv off
    assert s.select_variable() == "a"
    s2 = search(SIZES, ["a", "b", "c"], mrv=True)
    assert s2.select_variable() == "b"


# ── MRV: what it buys ────────────────────────────────────────────────

def test_mrv_solves_thrash_in_constant_nodes():
    for n in range(3, 13):
        r = solve(thrash(n).problem, C2)
        assert r.status == "UNSAT" and r.stats.nodes == 2, n
    only = solve(thrash(9).problem, Config(mrv=True))
    assert only.stats.nodes == 2                     # without AC-3 too
    assert solve(thrash(9).problem, C1).stats.nodes == 68889   # control


def test_mrv_is_not_monotone_a_documented_limit():
    # A heuristic can lose on a given instance.  Pinned so nobody "fixes"
    # the comparison into an always-better claim.
    case = random_csp(12, 3)
    assert solve(case.problem, C2).stats.nodes > solve(case.problem,
                                                       C0).stats.nodes


def test_mrv_wins_most_instances_and_the_median_but_has_a_heavy_tail():
    # Measured on random_csp(12), seeds 0..39, vs C1 (same AC-3, only the
    # variable order differs): MRV uses fewer nodes on 32 of 39, more on 7;
    # median ratio 3.5x.  Do NOT assert on summed nodes: at size 16 a single
    # seed (2: 31 -> 87,553 nodes) outweighs everything else.  Medians and
    # win counts are the honest statistics.
    ratios, wins, losses = [], 0, 0
    for s in range(40):
        p = random_csp(12, s).problem
        a, b = solve(p, C1).stats.nodes, solve(p, C2).stats.nodes
        ratios.append(a / b)
        wins += b < a
        losses += b > a
    assert wins > 2 * losses, (wins, losses)
    assert sorted(ratios)[len(ratios) // 2] > 2.0, sorted(ratios)[20]
    assert min(ratios) < 0.5          # and the tail really exists


# ── LCV: value ordering ──────────────────────────────────────────────

LCV = {"p": {"3": [["q", "==1"]],            # rules out 4 of q's 5 versions
             "2": [["q", "<=4"]],            # rules out 1
             "1": []},                       # rules out none
       "q": {str(i): [] for i in range(1, 6)}}


def test_lcv_cost_counts_the_options_ruled_out():
    s = search(LCV, ["p"], lcv=True)
    assert [lcv_cost(s, "p", Version(v)) for v in "321"] == [4, 1, 0]


def test_lcv_orders_by_ascending_cost_and_plain_search_does_not():
    s = search(LCV, ["p"], lcv=True)
    assert s.domain("p") == vs("3", "2", "1")                 # newest first
    assert lcv_order(s, "p", s.domain("p")) == vs("1", "2", "3")
    assert s.order_values("p", s.domain("p")) == vs("1", "2", "3")
    plain = search(LCV, ["p"])
    assert plain.order_values("p", plain.domain("p")) == vs("3", "2", "1")


def test_lcv_ties_keep_newest_first():
    d = {"p": {"4": [["q", "==1"]], "3": [["q", "==1"]],
               "2": [["q", "==2"]], "1": []},
         "q": {str(i): [] for i in range(1, 4)}}
    s = search(d, ["p"], lcv=True)
    assert lcv_order(s, "p", s.domain("p")) == vs("1", "4", "3", "2")


def test_lcv_charges_a_version_that_contradicts_an_assigned_neighbour():
    s = search(LCV, ["p"], lcv=True)
    s.assign["q"] = Version("5")             # q already chosen
    # p=3 needs q==1 and p=2 needs q<=4: both contradict q=5 (cost 1); p=1 free
    assert [lcv_cost(s, "p", Version(v)) for v in "321"] == [1, 1, 0]
    assert lcv_order(s, "p", s.domain("p")) == vs("1", "3", "2")


def test_lcv_uses_the_live_domain_of_an_active_neighbour():
    s = search(LCV, ["p", "q<=3"], lcv=True)      # q already limited to 1..3
    # p=3 (q==1) rules out 2 of {1,2,3}; p=2 (q<=4) rules out none
    assert [lcv_cost(s, "p", Version(v)) for v in "321"] == [2, 0, 0]


def test_the_fast_lcv_cost_equals_the_reference_loop_everywhere():
    """lcv_order uses a cached cost; lcv_cost is the plain reference.  They
    must agree on every candidate, in every state the search reaches."""
    from crux.resolver.gen import random_problem
    from crux.resolver.ordering import _cost_cached
    checked = 0
    for seed in range(150):
        p = random_problem(seed)
        s = Search(p, Config(lcv=True, ac3=bool(seed % 2)))
        if s._setup(float("inf")) is not None:
            continue
        # walk a few steps of the search, comparing at every variable chosen
        for _ in range(6):
            var = s.select_variable()
            if var is None:
                break
            dom = s.domain(var)
            cache = {}
            for v in dom:
                assert _cost_cached(s, var, v, cache) == lcv_cost(s, var, v), \
                    (seed, var, v)
                checked += 1
            mark = None
            for v in s.order_values(var, dom):
                mark = s._assign(var, v)
                if mark is not None:
                    break
            if mark is None:
                break
    assert checked > 300, checked


def test_lcv_order_is_unchanged_by_the_cache():
    from crux.resolver.gen import random_problem
    for seed in range(120):
        s = Search(random_problem(seed), Config(lcv=True))
        if s._setup(float("inf")) is not None:
            continue
        var = s.select_variable()
        if var is None:
            continue
        dom = s.domain(var)
        naive = tuple(v for _, _, v in sorted(
            ((lcv_cost(s, var, v), i, v) for i, v in enumerate(dom)),
            key=lambda t: (t[0], t[1])))
        assert lcv_order(s, var, dom) == naive, seed


def test_lcv_avoids_a_backtrack_that_plain_search_makes():
    d = {"a": {"1": [["x", ""]], "2": [["x", ">=2"]]},
         "b": {"1": [["x", "<2"]]},
         "x": {"1": [], "2": []}}
    p = prob(d, "a", "b")
    plain, lcv = solve(p, C0), solve(p, Config(lcv=True))
    assert plain.stats.backtracks >= 1 and lcv.stats.backtracks == 0
    assert lcv.stats.nodes < plain.stats.nodes
    assert lcv.plan == plain.plan


def test_lcv_can_trade_the_newest_release_for_fewer_backtracks():
    d = {"a": {"1": [["x", ""]], "2": [["x", ">=2"]]},
         "b": {"1": [["x", "<2"]]},
         "x": {"1": [], "2": []}}
    p = prob(d, "a", "b")
    for cfg in (C0, C3):
        r = solve(p, cfg)
        assert r.plan["a"] == Version("1")        # both end on a=1 here
    # but with a free choice LCV may pick an older one for a cheaper subtree
    free = {"a": {"2": [["x", "==1"]], "1": []}, "x": {"1": [], "2": []}}
    assert solve(prob(free, "a"), C0).plan["a"] == Version("2")
    assert solve(prob(free, "a"), C3).plan["a"] == Version("1")


# ── the non-latest metric ────────────────────────────────────────────

def test_non_latest_pins_counts_packages_below_their_newest_release():
    p = prob({"a": {"2": [["c", "<2"]], "1": []},
              "c": {"3": [], "2": [], "1": []}}, "a")
    plan = solve(p, C0).plan
    assert plan == {"a": Version("2"), "c": Version("1")}
    assert non_latest_pins(p, plan) == 1                 # only c is below newest
    assert non_latest_pins(p, {"a": Version("2"), "c": Version("3")}) == 0


def test_non_latest_in_rows():
    assert run_case(chain(10), "C0").non_latest == 0
    assert run_case(pinned_pair(10), "C0").non_latest == 2   # both pinned to v1
    assert run_case(thrash(3), "C2").non_latest is None      # UNSAT


# ── presets ──────────────────────────────────────────────────────────

def test_presets_turn_on_the_expected_flags():
    assert (C2.ac3, C2.mrv, C2.lcv, C2.learning) == (True, True, False, False)
    assert (C3.ac3, C3.mrv, C3.lcv, C3.learning) == (True, True, True, False)


@pytest.mark.parametrize("flags", [dict(mrv=True), dict(lcv=True),
                                   dict(mrv=True, lcv=True)])
def test_heuristics_work_without_ac3(flags):
    r = solve(chain(40).problem, Config(**flags))
    assert r.status == "SAT" and len(r.plan) == 40
    assert r.stats.ac3_pruned == 0 and r.stats.ac3_versions == 0
