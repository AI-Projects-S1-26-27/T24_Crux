"""The benchmark instance format round-trips and speaks the resolver contract."""

import json
from pathlib import Path

import pytest

from crux.benchmark.instance import (Instance, constraint_from_json,
                                     constraint_to_json, dump_jsonl, load_jsonl)
from crux.resolver.problem import Constraint, Problem

TOY = json.loads((Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())


@pytest.mark.parametrize("c", [
    Constraint.root("A", ">=1.0"),
    Constraint.root("b", ""),
    Constraint.edge("A", "1.0", "C", "<2.0,>=1"),
    Constraint.edge("a", "1.0.0", "c", "!=1.5"),
])
def test_constraint_round_trip(c):
    assert constraint_from_json(json.loads(json.dumps(constraint_to_json(c)))) == c


def test_constraint_normalisation_matches_the_contract():
    # spellings that the contract treats as the same constraint stay equal
    a = constraint_from_json({"kind": "edge", "pkg": "Foo_Bar", "version": "1.0",
                              "dep": "C", "spec": ">=1,<2"})
    assert a == Constraint.edge("foo-bar", "1.0", "c", "<2,>=1")


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        constraint_from_json({"kind": "bogus", "dep": "a", "spec": ""})


def _toy():
    core = frozenset(
        [Constraint.root("a", ">=1.0"), Constraint.root("b", ">=1.0")]
        + [Constraint.edge("a", v, "c", "<2.0") for v in TOY["a"]]
        + [Constraint.edge("b", v, "c", ">=3.1") for v in TOY["b"]])
    return Instance("toy", TOY, (("a", ">=1.0"), ("b", ">=1.0")), "UNSAT", core,
                    {"source": "fixture"})


def test_instance_round_trip(tmp_path):
    inst = _toy()
    path = tmp_path / "x.jsonl"
    assert dump_jsonl([inst, inst], path) == 2
    back = load_jsonl(path)
    assert back == [inst, inst]
    assert back[0].to_json() == inst.to_json()


def test_instance_builds_the_resolver_problem():
    inst = _toy()
    p = inst.problem()
    assert isinstance(p, Problem)
    assert p.roots == (Constraint.root("a", ">=1.0"), Constraint.root("b", ">=1.0"))
    assert inst.core <= set(p.constraints())
    assert inst.requirements() == ["a>=1.0", "b>=1.0"]


def test_core_requires_unsat():
    with pytest.raises(ValueError):
        Instance("x", {}, (), "SAT", frozenset())


def test_gzip_round_trip_is_reproducible(tmp_path):
    inst = _toy()
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    dump_jsonl([inst], a)
    dump_jsonl([inst], b)
    assert load_jsonl(a) == [inst]
    assert a.read_bytes() == b.read_bytes()
