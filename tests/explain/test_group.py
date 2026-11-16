"""Grouping a core into statements (crux/explain/group.py).

What must hold:
  * one group per root dependency and one per (package, dependency) pair,
    so the count equals the benchmark's ``real_cores.groups``;
  * different specifiers in one (package, dependency) pair stay one group,
    summarised by a range (the boto3 shape);
  * "every allowed version" is claimed only when it is true, per group and
    per package;
  * the output does not depend on the order of the core.
"""

import json
import random
from pathlib import Path

import pytest

from crux.benchmark.real_cores import groups as benchmark_groups
from crux.data.graph import Graph
from crux.explain import explain
from crux.explain.group import (envelope, group_core, intersection,
                                rules_out_package, spec_bounds)
from crux.resolver import Constraint, Problem
from packaging.version import Version

ROOT = Path(__file__).parent.parent.parent
TOY = json.loads((ROOT / "tests" / "fixtures" / "toy_abc.json").read_text())


def core_of(graph: dict, *reqs):
    p = Problem.from_requirements(Graph.from_dict(graph), reqs)
    r = explain(p)
    assert r.status == "UNSAT" and r.minimal
    return p, r.core


def by_key(gs):
    return {(x.kind, x.pkg, x.dep): x for x in gs}


# ── the proposal's example ──────────────────────────────────────────────

def test_toy_eleven_constraints_become_four_statements():
    p, core = core_of(TOY, "a>=1.0", "b>=1.0")
    assert len(core) == 11
    gs = group_core(p, core)
    assert [(x.kind, x.pkg, x.dep) for x in gs] == [
        ("root", None, "a"), ("root", None, "b"),
        ("edge", "a", "c"), ("edge", "b", "c")]
    ac = by_key(gs)[("edge", "a", "c")]
    assert ac.versions == ("1.0", "1.1", "1.2", "1.3", "1.4")
    assert ac.common_spec == "<2.0" and ac.range == "<2.0"
    assert ac.covers_all and ac.n_allowed == 5 and ac.uncovered == ()
    assert (ac.n_matching, ac.lowest_match, ac.highest_match) == (3, "1.0", "1.2")
    bc = by_key(gs)[("edge", "b", "c")]
    assert (bc.first, bc.last, bc.n_matching) == ("1.0", "1.3", 2)
    root_a = by_key(gs)[("root", None, "a")]
    assert root_a.range == ">=1.0" and root_a.n_matching == 5 and root_a.depth == 0
    assert len(gs) == benchmark_groups(core)


def test_graph_or_problem_accepted():
    p, core = core_of(TOY, "a", "b")
    assert group_core(p, core) == group_core(p.graph, core)


# ── the boto3 shape: one group, many different specifiers ───────────────

def boto_like(n=50):
    dep = {f"1.{i}": [] for i in range(n)} | {f"0.{i}": [] for i in range(1, 6)}
    pkg = {f"2.{i}": [["d", f">=1.{i},<1.{i + 1}"]] for i in range(n)}
    return {"b": pkg, "d": dep}


def test_different_specifiers_stay_one_group_with_a_summary_range():
    p, core = core_of(boto_like(), "b", "d<=0.5")
    assert len(core) == 52
    gs = group_core(p, core)
    assert len(gs) == 3
    bd = by_key(gs)[("edge", "b", "d")]
    assert len(bd.members) == 50 and len(bd.specs) == 50
    assert bd.common_spec is None
    assert bd.range == "<1.50,>=1.0"
    assert (bd.first, bd.last) == ("2.0", "2.49") and bd.covers_all
    assert (bd.n_matching, bd.lowest_match, bd.highest_match) == (50, "1.0", "1.49")
    d = by_key(gs)[("root", None, "d")]
    assert (d.n_matching, d.highest_match) == (5, "0.5")


def test_versions_are_sorted_as_versions_not_text():
    p, core = core_of(boto_like(12), "b", "d<=0.5")
    bd = by_key(group_core(p, core))[("edge", "b", "d")]
    assert list(bd.versions) == sorted(bd.versions, key=Version)
    assert bd.last == "2.11"


# ── releases of one package failing for different reasons ───────────────

PARTIAL = {   # planted/partial/d1/none/s0
    "bramble": {"3.3.0": [], "3.3.1": [["ochre", "<1.2.1"]],
                "3.4.0": [["egret", ">=1.4.0"]]},
    "cinder": {"1.0.0": [["ochre", ">=1.2.3"]], "2.0.0": [["ochre", ">=1.2.3"]],
               "2.0.1": [["ochre", ">=1.2.3"]]},
    "ochre": {"1.2.0": [], "1.2.3": []},
    "egret": {"1.0.0": []},
}


def test_partial_cover_per_group_full_cover_per_package():
    p, core = core_of(PARTIAL, "bramble>=3.3.1", "cinder>=1.0.0")
    gs = group_core(p, core)
    be = by_key(gs)[("edge", "bramble", "egret")]
    bo = by_key(gs)[("edge", "bramble", "ochre")]
    assert not be.covers_all and be.uncovered == ("3.3.1",)
    assert not bo.covers_all and bo.uncovered == ("3.4.0",)
    assert be.unavailable and be.dep_known        # egret exists, not >=1.4.0
    assert rules_out_package(gs, "bramble")
    assert rules_out_package(gs, "cinder")
    assert not rules_out_package(gs, "ochre")      # no edge groups


# ── things the snapshot does not have ───────────────────────────────────

def test_request_for_a_missing_version():
    p, core = core_of({"x": {"1.0": []}}, "x==9")
    (g,) = group_core(p, core)
    assert g.kind == "root" and g.range == "==9"
    assert g.unavailable and g.dep_known
    assert (g.lowest_match, g.highest_match) == ("", "")
    assert g.missing == ()           # no drops known for a dict graph


def test_missing_version_carries_the_drop_reason():
    graph = Graph.from_dict(
        {"app": {"1.0": [["sqlalchemy", "==1.4.29"]]},
         "sqlalchemy": {"2.0.0": []}},
        drops={"sqlalchemy": [["1.4.29", "no_wheel"], ["1.4.28", "no_wheel"]]})
    p = Problem.from_requirements(graph, ["app"])
    gs = group_core(p, explain(p).core)
    edge = by_key(gs)[("edge", "app", "sqlalchemy")]
    assert edge.unavailable and edge.missing == (("1.4.29", "no_wheel"),)
    assert edge.missing_reasons == ("no_wheel",)
    assert by_key(gs)[("root", None, "app")].missing == ()   # app is available


def test_missing_reasons_union_over_an_edge_group():
    graph = Graph.from_dict(
        {"a": {"1.0": [["d", "==2.0"]], "1.1": [["d", ">=3"]]}, "d": {"1.0": []}},
        drops={"d": [["2.0", "yanked"], ["3.0", "no_wheel"], ["3.1", "no_wheel"]]})
    p = Problem.from_requirements(graph, ["a"])
    (edge,) = [x for x in group_core(p, explain(p).core) if x.kind == "edge"]
    assert edge.missing == (("3.1", "no_wheel"), ("3.0", "no_wheel"),
                            ("2.0", "yanked"))
    assert edge.missing_reasons == ("no_wheel", "yanked")


def test_dependency_with_no_releases_at_all():
    p, core = core_of({"a": {"1.0": [["ghost", ">=1"]]}}, "a")
    edge = by_key(group_core(p, core))[("edge", "a", "ghost")]
    assert edge.unavailable and not edge.dep_known


def test_contradicting_requests_are_one_group_with_no_range():
    graph = {"c": {"1.0": [], "3.0": []}}
    p, core = core_of(graph, "c<2", "c>=3")
    (g,) = group_core(p, core)
    assert len(g.members) == 2 and g.specs == ("<2", ">=3")
    assert g.range is None and g.unavailable
    assert g.missing == ()           # nothing to look up: the requests clash


# ── structure and order ─────────────────────────────────────────────────

CHAIN = {"a": {"1.0": [["b", ">=1"]]}, "b": {"1.0": [["c", ">=2"]]},
         "c": {"1.0": [], "2.0": []}}


def test_depth_follows_the_chain_from_the_request():
    p, core = core_of(CHAIN, "a", "c<2")
    gs = group_core(p, core)
    assert [(x.kind, x.pkg, x.dep, x.depth) for x in gs] == [
        ("root", None, "a", 0), ("root", None, "c", 0),
        ("edge", "a", "b", 1), ("edge", "b", "c", 2)]


def test_order_of_the_core_does_not_matter():
    p, core = core_of(PARTIAL, "bramble>=3.3.1", "cinder>=1.0.0")
    want = group_core(p, core)
    rng = random.Random(0)
    for _ in range(5):
        shuffled = list(core) * 2
        rng.shuffle(shuffled)
        assert group_core(p, shuffled) == want


# ── ranges ──────────────────────────────────────────────────────────────

V = Version


@pytest.mark.parametrize("spec, lo, hi", [
    ("", None, None),
    (">=1.2", (V("1.2"), True), None),
    (">1.2,<=2", (V("1.2"), False), (V("2"), True)),
    ("<2,>=1,>=1.5", (V("1.5"), True), (V("2"), False)),
    (">=1,>1", (V("1"), False), None),
    ("==1.4", (V("1.4"), True), (V("1.4"), True)),
    ("==1.2.*", (V("1.2"), True), (V("1.3"), False)),
    ("==1.*", (V("1"), True), (V("2"), False)),
    ("~=1.4.5", (V("1.4.5"), True), (V("1.5"), False)),
    ("~=2.2", (V("2.2"), True), (V("3"), False)),
    ("!=1.3,>=1", (V("1"), True), None),
    ("===1.0", (V("1.0"), True), (V("1.0"), True)),
])
def test_spec_bounds(spec, lo, hi):
    assert spec_bounds(spec) == (lo, hi)


@pytest.mark.parametrize("specs, want", [
    ([">=1.36,<1.37", ">=1.43,<1.44"], "<1.44,>=1.36"),
    (["<2.0"], "<2.0"),
    (["<2", ">=3"], ""),                 # unbounded on both sides
    ([">1", ">=1"], ">=1"),              # inclusive wins a tie in a union
    (["==1.0", "==2.0"], "<=2.0,>=1.0"),
    (["==3"], "==3"),
    ([">=2,<1"], None),                  # no member admits anything
    ([">=2,<1", "<5"], "<5"),
])
def test_envelope(specs, want):
    assert envelope(specs) == want


@pytest.mark.parametrize("specs, want", [
    ([">=1", "<2"], "<2,>=1"),
    (["<2", ">=3"], None),
    ([">=1", ">1"], ">1"),               # exclusive wins a tie in an intersection
    (["", ">=1"], ">=1"),
    ([""], ""),
    (["==1.0", "<=1.0"], "==1.0"),
])
def test_intersection(specs, want):
    assert intersection(specs) == want


# ── agreement with the committed benchmark ──────────────────────────────

REAL = ROOT / "benchmarks" / "results" / "cores" / "real_cores.jsonl"
REAL_INST = ROOT / "benchmarks" / "corpus" / "instances.jsonl.gz"
PLANTED = ROOT / "benchmarks" / "planted" / "instances.jsonl"


def benchmark_cases():
    from crux.benchmark.instance import constraint_from_json, load_jsonl
    insts = {i.id: i for i in load_jsonl(REAL_INST)}
    for line in REAL.read_text().splitlines():
        d = json.loads(line)
        yield (insts[d["instance"]], [constraint_from_json(c) for c in d["core"]],
               d["core_groups"])
    for i in load_jsonl(PLANTED):
        yield i, list(i.core), benchmark_groups(i.core)


@pytest.mark.skipif(not (REAL.exists() and REAL_INST.exists() and PLANTED.exists()),
                    reason="benchmark files not present")
def test_counts_and_coverage_match_the_benchmark():
    n = 0
    for inst, core, want in benchmark_cases():
        gs = group_core(inst.problem(), core)
        assert len(gs) == want, inst.id
        for pkg in {x.pkg for x in gs if x.kind == "edge"}:
            assert rules_out_package(gs, pkg), (inst.id, pkg)
        assert all(x.depth is not None for x in gs), inst.id
        n += 1
    assert n == 61 + 198
