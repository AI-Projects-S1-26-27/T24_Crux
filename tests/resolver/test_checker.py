"""Stage 1: the independent plan checker (offline, no solver)."""

import itertools
import json
import random
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import Constraint, Problem
from crux.resolver.checker import (
    Violation, admissible, check_plan, is_valid,
)

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


def V(**kw):
    return {k: Version(v) for k, v in kw.items()}


def kinds(problem, plan):
    return sorted(v.kind for v in check_plan(problem, plan))


def prob(d, *reqs, disabled=()):
    return Problem.from_requirements(Graph.from_dict(d), reqs,
                                     disabled=frozenset(disabled))


# ── valid plans ──────────────────────────────────────────────────────

def test_valid_single_root_plan():
    p = prob(TOY, "a")
    assert check_plan(p, V(a="1.4", c="1.2")) == []
    assert is_valid(p, V(a="1.0", c="1.0"))


def test_empty_problem_has_the_empty_plan():
    assert is_valid(prob(TOY), {})


def test_diamond_and_shared_dependency():
    d = {"app": {"1": [["l", ">=1"], ["r", ">=1"]]},
         "l": {"1": [["base", "<2"]]}, "r": {"1": [["base", ">=1"]]},
         "base": {"1": [], "2": []}}
    p = prob(d, "app")
    assert is_valid(p, V(app="1", l="1", r="1", base="1"))
    assert kinds(p, V(app="1", l="1", r="1", base="2")) == ["unsatisfied"]


def test_cycle_terminates_and_is_valid():
    d = {"a": {"1": [["b", ""]]}, "b": {"1": [["a", ""]]}}
    assert is_valid(prob(d, "a"), V(a="1", b="1"))


# ── the toy conflict has no valid plan at all ────────────────────────

def test_toy_abc_has_no_valid_plan_exhaustively():
    p = prob(TOY, "a", "b")
    g = p.graph
    for a, b, c in itertools.product(g.versions("a"), g.versions("b"),
                                     g.versions("c")):
        assert not is_valid(p, {"a": a, "b": b, "c": c}), (a, b, c)


# ── each violation kind ──────────────────────────────────────────────

def test_missing_dependency():
    v = check_plan(prob(TOY, "a"), V(a="1.4"))
    assert [x.kind for x in v] == ["missing_dependency"]
    assert v[0].pkg == "c"
    assert v[0].constraint == Constraint.edge("a", "1.4", "c", "<2.0")


def test_missing_root_package():
    assert kinds(prob(TOY, "a"), {}) == ["missing_dependency"]


def test_unsatisfied_names_the_constraint():
    v = check_plan(prob(TOY, "a"), V(a="1.4", c="3.2"))
    assert [x.kind for x in v] == ["unsatisfied"]
    assert v[0].constraint == Constraint.edge("a", "1.4", "c", "<2.0")


def test_root_unsatisfied():
    assert kinds(prob(TOY, "a>=1.3"), V(a="1.0", c="1.0")) == ["unsatisfied"]


def test_unknown_version_and_package():
    assert kinds(prob(TOY, "a"), V(a="9.9", c="1.0")) == ["unknown_version"]
    d = {"a": {"1": [["ghost", ""]]}}
    got = kinds(prob(d, "a"), V(a="1", ghost="1"))
    assert got == ["unknown_package"]


def test_dangling_dependency_has_no_valid_plan():
    d = {"a": {"1": [["ghost", ""]]}}
    p = prob(d, "a")
    assert not is_valid(p, V(a="1"))
    assert not is_valid(p, V(a="1", ghost="1"))


def test_unneeded_is_not_reported_when_reachability_is_unknown():
    # a is missing, so we cannot know whether c is needed: no cascade noise
    assert kinds(prob(TOY, "a"), V(c="1.0")) == ["missing_dependency"]


def test_unneeded_package():
    v = check_plan(prob(TOY, "a"), V(a="1.4", c="1.2", b="1.0"))
    assert [(x.kind, x.pkg) for x in v] == [("unneeded_package", "b")]


def test_package_only_reachable_through_unchosen_release_is_unneeded():
    d = {"a": {"1": [], "2": [["x", ""]]}, "x": {"1": []}}
    assert kinds(prob(d, "a"), V(a="1", x="1")) == ["unneeded_package"]
    assert is_valid(prob(d, "a"), V(a="2", x="1"))


def test_non_canonical_name_and_bad_plan():
    assert "non_canonical_name" in kinds(prob(TOY, "a"),
                                         {"A": Version("1.4"),
                                          "c": Version("1.2")})
    assert kinds(prob(TOY, "a"), [("a", "1.4")]) == ["bad_plan"]
    assert kinds(prob(TOY, "a"), {"a": "1.4", "c": Version("1.2")}) == [
        "bad_plan", "missing_dependency"]


def test_all_violations_are_reported_not_just_the_first():
    got = kinds(prob(TOY, "a", "b"), V(a="1.4", b="1.3", c="2.0"))
    assert got == ["unsatisfied", "unsatisfied"]    # a needs c<2, b needs c>=3.1
    got = kinds(prob(TOY, "a", "b"), V(a="1.4", b="1.3", c="3.1"))
    assert got == ["unsatisfied"]               # only a's edge fails
    got = kinds(prob(TOY, "a", "b"), V(a="9", b="9"))
    assert got == ["unknown_version", "unknown_version"]


# ── disabled constraints ─────────────────────────────────────────────

def test_disabled_root_is_not_required_or_activating():
    p = prob(TOY, "a", "b", disabled={Constraint.root("b")})
    assert is_valid(p, V(a="1.4", c="1.2"))
    assert kinds(p, V(a="1.4", c="1.2", b="1.0")) == ["unneeded_package"]


def test_disabled_edge_is_ignored_and_does_not_activate():
    edge = Constraint.edge("a", "1.4", "c", "<2.0")
    p = prob(TOY, "a", disabled={edge})
    assert is_valid(p, V(a="1.4"))
    assert kinds(p, V(a="1.4", c="1.0")) == ["unneeded_package"]
    # other releases of a keep their edge
    assert kinds(p, V(a="1.3")) == ["missing_dependency"]


def test_disabling_makes_the_toy_conflict_satisfiable():
    for dropped in (Constraint.root("a"), Constraint.root("b")):
        p = prob(TOY, "a", "b", disabled={dropped})
        other = "b" if dropped.dep == "a" else "a"
        plan = V(**{other: "1.3" if other == "b" else "1.4"},
                 c="3.2" if other == "b" else "1.2")
        assert is_valid(p, plan), dropped


# ── pre-release rule ─────────────────────────────────────────────────

PRE = {"p": {"1.0": [], "2.0b1": []},       # final exists
       "q": {"1.0b1": [], "1.0b2": []},      # only pre-releases
       "r": {"1.0": [], "2.0b1": [], "2.0": []}}


def test_prerelease_excluded_when_a_final_matches():
    assert kinds(prob(PRE, "p"), V(p="2.0b1")) == ["unsatisfied"]
    assert is_valid(prob(PRE, "p"), V(p="1.0"))


def test_prerelease_allowed_when_spec_names_one():
    assert is_valid(prob(PRE, "p>=2.0b1"), V(p="2.0b1"))
    assert is_valid(prob(PRE, "r>=2.0b1"), V(r="2.0b1"))


def test_prerelease_allowed_when_no_final_matches():
    assert is_valid(prob(PRE, "q"), V(q="1.0b2"))
    assert is_valid(prob(PRE, "p>1.0"), V(p="2.0b1"))   # only 2.0b1 > 1.0


def test_excluding_a_prerelease_does_not_invite_prereleases():
    """Regression (found by an independent review).  `a!=1.0b1` names a
    pre-release only to exclude it; packaging, the Graph and every solver
    treat it as not naming one.  The checker used to disagree."""
    from packaging.specifiers import SpecifierSet
    rel = [Version("1.5b1"), Version("1.0")]
    assert not admissible(SpecifierSet("!=1.0b1"), Version("1.5b1"), rel)
    assert admissible(SpecifierSet("!=1.0"), Version("1.5b1"), rel)  # no final left
    assert admissible(SpecifierSet(">=1.0b1"), Version("1.5b1"), rel)
    # the reviewer's instance: the solvers say UNSAT, so must the checker
    d = {"a": {"1.0": [], "1.5b1": []}}
    p = prob(d, "a!=1.0b1", "a!=1.0")
    assert kinds(p, V(a="1.5b1")) == ["unsatisfied"]
    assert kinds(p, V(a="1.0")) == ["unsatisfied"]


def test_dev_release_counts_as_prerelease():
    d = {"p": {"1.0": [], "2.0.dev1": []}}
    assert kinds(prob(d, "p"), V(p="2.0.dev1")) == ["unsatisfied"]


# ── differential test: checker rule == Graph.candidates ─────────────
# The checker re-derives the pre-release rule on purpose.  If this ever
# fails, either packaging changed or one side is wrong.

_VERSIONS = ["0.9", "1.0", "1.1", "1.5", "2.0", "3.0",
             "1.0b1", "1.1rc1", "2.0a1", "2.0.dev1", "2.1b2", "3.0rc1"]
_SPECS = ["", ">=1.0", "<2", ">=2.0b1", "==1.0", ">1.0,<3", "~=1.0",
          "!=1.1", "<3.0a1", ">=2.0.dev1", ">3", "<0.5", "==1.*", ">=1.1rc1",
          # `!=` against a pre-release does NOT name a pre-release; the first
          # checker got this wrong and this pool had no such spec to catch it
          "!=1.0b1", "!=2.0a1", "!=2.1b2", "!=1.1rc1,>=1.0", "!=3.0rc1,<3",
          "~=1.0b1", ">1.0b1", "<=1.1rc1", "==2.*", "!=1.*"]


def test_admissible_matches_graph_candidates():
    rng = random.Random(0)
    for _ in range(400):
        rel = rng.sample(_VERSIONS, rng.randint(1, len(_VERSIONS)))
        spec = rng.choice(_SPECS)
        g = Graph.from_dict({"p": {v: [] for v in rel}})
        versions = g.versions("p")
        mine = {v for v in versions if admissible(SpecifierSet(spec), v,
                                                  versions)}
        assert mine == set(g.candidates("p", spec)), (rel, spec)
