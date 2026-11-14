"""The planted generator: deterministic, and every recorded core is proven to
be the unique minimal core.  The mutation tests show the proof rejects a wrong
answer key in both directions (a constraint too many, a constraint too few)."""

import dataclasses
import json
from pathlib import Path

import pytest

from crux.benchmark.instance import Instance, load_jsonl
from crux.benchmark.planted import (KINDS, NOISE, Params, PlantedProofError,
                                    generate, prove, suite)
from crux.resolver.problem import Config, Constraint
from crux.resolver.solver import solve

ROOT = Path(__file__).parent.parent.parent
COMMITTED = ROOT / "benchmarks" / "planted" / "instances.jsonl"
TOY = json.loads((ROOT / "tests" / "fixtures" / "toy_abc.json").read_text())

SAMPLE = [Params(k, {"deep": 3, "impossible": 2}.get(k, 1), n, s)
          for k in KINDS for n in NOISE for s in (0, 1)]


def test_suite_shape():
    ps = suite()
    assert len(ps) == 198
    assert len({p.id for p in ps}) == 198
    assert {p.kind for p in ps} == set(KINDS)
    assert {p.noise for p in ps} == set(NOISE)


@pytest.mark.parametrize("p", SAMPLE, ids=lambda p: p.id)
def test_generated_instance_is_proven(p):
    inst = generate(p)
    assert inst.expected == "UNSAT"
    assert inst.core and inst.core <= set(inst.problem().constraints())
    assert inst.meta["proof"]["core_alone"] == "UNSAT"
    assert inst.meta["proof"]["witnesses_checked"] == len(inst.core)


@pytest.mark.parametrize("p", SAMPLE[:6], ids=lambda p: p.id)
def test_generation_is_deterministic(p):
    assert generate(p).to_json() == generate(p).to_json()


def test_different_seeds_differ():
    a, b = generate(Params("deep", 3, "small", 0)), generate(Params("deep", 3, "small", 1))
    assert a.graph != b.graph


def _with_core(inst, core):
    return dataclasses.replace(inst, core=frozenset(core))


@pytest.mark.parametrize("p", [Params("deep", 3, "small", 0),
                               Params("partial", 1, "large", 1),
                               Params("threeway", 1, "small", 2)], ids=lambda p: p.id)
def test_proof_rejects_a_core_with_one_constraint_too_many(p):
    inst = generate(p)
    extra = next(c for c in inst.problem().constraints() if c not in inst.core)
    with pytest.raises(PlantedProofError):
        prove(_with_core(inst, inst.core | {extra}))


@pytest.mark.parametrize("p", [Params("deep", 3, "small", 0),
                               Params("partial", 1, "large", 1),
                               Params("threeway", 1, "small", 2)], ids=lambda p: p.id)
def test_proof_rejects_a_core_with_one_constraint_missing(p):
    inst = generate(p)
    for drop in sorted(inst.core, key=str):
        with pytest.raises(PlantedProofError):
            prove(_with_core(inst, inst.core - {drop}))


def test_proof_rejects_constraints_not_in_the_instance():
    inst = generate(Params("direct", 1, "none", 0))
    ghost = Constraint.edge("nowhere", "1.0", "nothing", ">=1")
    with pytest.raises(PlantedProofError):
        prove(_with_core(inst, inst.core | {ghost}))


def test_toy_abc_core_is_the_unique_minimal_core():
    """The proposal's own example: 2 roots + 5 a-edges + 4 b-edges = 11."""
    core = frozenset(
        [Constraint.root("a", ">=1.0"), Constraint.root("b", ">=1.0")]
        + [Constraint.edge("a", v, "c", "<2.0") for v in TOY["a"]]
        + [Constraint.edge("b", v, "c", ">=3.1") for v in TOY["b"]])
    inst = Instance("toy", TOY, (("a", ">=1.0"), ("b", ">=1.0")), "UNSAT", core)
    assert len(core) == 11
    assert prove(inst)["witnesses_checked"] == 11


CONFIGS = {**{f"C{i}": Config.named(f"C{i}", timeout_s=5) for i in range(5)},
           "C4-noLCV": Config(ac3=True, mrv=True, learning=True, timeout_s=5)}


@pytest.mark.parametrize("p", [q for q in SAMPLE if q.seed == 0], ids=lambda p: p.id)
def test_no_solver_is_wrong_and_every_conflict_contains_the_core(p):
    """Every unsatisfiable subset contains the unique minimal core, so a sound
    conflict must contain the planted core.  A SAT answer, or a conflict
    missing a core constraint, is a solver bug.  A TIMEOUT is not wrong: plain
    backtracking (C0, C1) can thrash for millions of nodes on a conflict
    buried in large noise, which is a finding the harness measures, so it is
    allowed here.  C4 must finish."""
    inst = generate(p)
    prob = inst.problem()
    for name, cfg in CONFIGS.items():
        res = solve(prob, cfg)
        assert res.status in ("UNSAT", "TIMEOUT"), (name, res.status)
        if res.status == "UNSAT":
            assert inst.core <= set(res.conflict), name
        if name.startswith("C4"):
            assert res.status == "UNSAT", name


def test_committed_instances_match_the_generator():
    """benchmarks/planted/instances.jsonl is exactly what the code produces."""
    committed = load_jsonl(COMMITTED)
    assert [i.id for i in committed] == [p.id for p in suite()]
    for inst in committed[::17]:      # every 17th: a spread across kinds
        p = Params(inst.meta["kind"], inst.meta["depth"],
                   inst.meta["noise"], inst.meta["seed"])
        assert generate(p).to_json() == inst.to_json()
