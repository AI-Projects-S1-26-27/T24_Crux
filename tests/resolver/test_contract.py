"""Stage 0: the resolver contract (offline, no solver needed)."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from packaging.version import Version

from crux.data.graph import Graph
from crux.resolver import (
    Config, Constraint, Problem, Result, Stats, solve,
)

TOY = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


def test_constraint_spec_order_does_not_matter():
    a = Constraint.edge("A", "1.0", "C", ">=1.0,<2.0")
    b = Constraint.edge("a", "1.0", "c", "<2.0,>=1.0")
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1


def test_constraint_canonicalizes_and_prints():
    c = Constraint.edge("Foo_Bar", "1.0", "Baz.Qux", "<2")
    assert (c.pkg, c.dep, c.version) == ("foo-bar", "baz-qux", "1.0")
    assert str(c) == "foo-bar 1.0 requires baz-qux<2"
    assert str(Constraint.root("A")) == "root requires a"


def test_constraint_kind_rules():
    with pytest.raises(ValueError):
        Constraint("edge", "c", "", pkg=None, version=None)
    with pytest.raises(ValueError):
        Constraint("root", "c", "", pkg="a", version="1.0")


def test_problem_from_requirements_canonicalizes():
    g = Graph.from_dict(TOY)
    p = Problem.from_requirements(g, ["A>=1.0", "B"])
    assert p.roots == (Constraint.root("a", ">=1.0"), Constraint.root("b"))


@pytest.mark.parametrize("bad", ['a; python_version<"3"', "a[x]>=1",
                                 "a @ https://example.com/a.whl"])
def test_problem_rejects_out_of_scope_requirements(bad):
    with pytest.raises(ValueError):
        Problem.from_requirements(Graph.from_dict(TOY), [bad])


def test_disabled_constraints_drop_out_of_active_roots():
    g = Graph.from_dict(TOY)
    p = Problem.from_requirements(g, ["a", "b"])
    q = p.with_disabled({Constraint.root("b")})
    assert q.active_roots() == (Constraint.root("a"),)
    assert p.active_roots() == p.roots          # original untouched


def test_types_are_frozen():
    cfg = Config()
    with pytest.raises(FrozenInstanceError):
        cfg.ac3 = True
    p = Problem.from_requirements(Graph.from_dict(TOY), ["a"])
    with pytest.raises(FrozenInstanceError):
        p.roots = ()


def test_config_presets_are_cumulative():
    flags = lambda c: (c.ac3, c.mrv, c.lcv, c.learning)
    assert flags(Config.named("C0")) == (False, False, False, False)
    assert flags(Config.named("C1")) == (True, False, False, False)
    assert flags(Config.named("C2")) == (True, True, False, False)
    assert flags(Config.named("C3")) == (True, True, True, False)
    assert flags(Config.named("C4")) == (True, True, True, True)
    assert Config.named("C4", timeout_s=5).timeout_s == 5
    with pytest.raises(ValueError):
        Config.named("C5")


def test_result_invariants():
    plan = {"a": Version("1.0")}
    assert Result("SAT", plan=plan).status == "SAT"
    assert Result("UNSAT", conflict=[Constraint.root("a")]).plan is None
    assert Result("TIMEOUT").conflict is None
    for bad in (dict(status="SAT"),
                dict(status="UNSAT"),
                dict(status="SAT", plan=plan, conflict=[]),
                dict(status="UNSAT", plan=plan, conflict=[]),
                dict(status="TIMEOUT", plan=plan),
                dict(status="MAYBE")):
        with pytest.raises(ValueError):
            Result(**bad)
    assert Stats().nodes == 0


def test_toy_abc_is_unsat():
    """a needs c<2.0, b needs c>=3.1: together they cannot hold."""
    p = Problem.from_requirements(Graph.from_dict(TOY), ["a", "b"])
    r = solve(p)
    assert r.status == "UNSAT"
    assert {Constraint.root("a"), Constraint.root("b")} <= set(r.conflict)
