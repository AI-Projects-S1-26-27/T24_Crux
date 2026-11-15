"""Tool runners on the proposal's toy instance.  pip runs from the current
interpreter; uv tests are skipped when uv is not installed.  Both run with
--no-index on a local folder, so these tests stay offline."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from crux.benchmark.harness import (crux_rows, message_row, run_crux_e2e,
                                    run_pip, run_uv)
from crux.benchmark.instance import Instance
from crux.benchmark.wheels import write_index
from crux.resolver.problem import Constraint

TOY = json.loads((Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())
CORE = frozenset([Constraint.root("a", ">=1.0"), Constraint.root("b", ">=1.0")]
                 + [Constraint.edge("a", v, "c", "<2.0") for v in TOY["a"]]
                 + [Constraint.edge("b", v, "c", ">=3.1") for v in TOY["b"]])
UNSAT = Instance("toy", TOY, (("a", ">=1.0"), ("b", ">=1.0")), "UNSAT", CORE,
                 {"core_packages": ["a", "b", "c"], "core_size": 11})
SAT = Instance("toy-sat", TOY, (("a", ">=1.0"),), "SAT")


@pytest.fixture
def wheels(tmp_path):
    write_index(TOY, tmp_path / "w")
    return tmp_path / "w"


def test_pip_unsat_and_sat(wheels, tmp_path):
    r = run_pip("pip", sys.executable, wheels, UNSAT.requirements(), 60, tmp_path)
    assert r.status == "UNSAT" and r.exit_code != 0
    row = message_row(UNSAT, r)
    assert row["core_recall"] == 1.0
    r = run_pip("pip", sys.executable, wheels, SAT.requirements(), 60, tmp_path)
    assert r.status == "SAT" and r.plan == {"a": "1.4", "c": "1.2"}
    assert message_row(SAT, r)["plan_valid"] is True


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_uv_unsat_and_sat(wheels, tmp_path):
    uv = shutil.which("uv")
    r = run_uv("uv", uv, wheels, UNSAT.requirements(), 60, tmp_path)
    assert r.status == "UNSAT"
    r = run_uv("uv", uv, wheels, SAT.requirements(), 60, tmp_path)
    assert r.status == "SAT" and r.plan == {"a": "1.4", "c": "1.2"}


def test_crux_end_to_end(tmp_path):
    f = tmp_path / "toy.json"
    f.write_text(json.dumps(UNSAT.to_json()))
    r = run_crux_e2e("crux-e2e", f, "C4-noLCV", 60, tmp_path)
    assert r.status == "UNSAT" and r.extra["conflict_size"] >= 11


def test_crux_configurations_contain_the_core():
    rows = crux_rows(UNSAT, 10)
    assert {r["config"] for r in rows} == {"C0", "C1", "C2", "C3", "C4", "C4-noLCV"}
    for r in rows:
        assert r["status"] == "UNSAT" and r["contains_core"] is True, r["config"]
