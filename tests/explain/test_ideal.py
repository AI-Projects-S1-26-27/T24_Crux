"""The hand-written ideal explanations (benchmarks/ideal/ideal.jsonl) are
checked against the data, so their facts cannot drift from the snapshot:
the requests, which of them are in the conflict, and that every proposed
fix really works.  Their wording is left to review."""

from collections import Counter

import pytest

from crux.explain import explain
from crux.explain.ideal import (IDEAL, PLANTED, REAL, Row, apply_fix, compare,
                                load, packages_named, problem_for, _instances)
from crux.resolver import Config
from crux.resolver.checker import check_plan
from crux.resolver.solver import solve

pytestmark = pytest.mark.skipif(
    not (IDEAL.exists() and REAL.exists() and PLANTED.exists()),
    reason="benchmark files not present")

RECORDS = load() if IDEAL.exists() else []
SOLVE = Config(ac3=True, mrv=True, learning=True, timeout_s=30)


def req_text(c):
    from crux.explain.render import req
    return req(c.dep, c.spec)


def test_thirty_cases_of_the_planned_mix():
    assert len(RECORDS) == 30
    assert len({r["id"] for r in RECORDS}) == 30
    assert len({r["instance"] for r in RECORDS}) == 30
    assert Counter(r["source"] for r in RECORDS) == {"toy": 1, "real": 20,
                                                    "planted": 9}
    assert all(r["provenance"] == "hand-written" for r in RECORDS)
    assert all(isinstance(r["reviewed"], bool) for r in RECORDS)


@pytest.mark.parametrize("rec", RECORDS, ids=lambda r: r["id"])
def test_case_matches_the_data(rec):
    if rec["instance"] != "toy_abc":
        assert sorted(rec["requirements"]) == sorted(
            _instances()[rec["instance"]].requirements())
    p = problem_for(rec)
    r = explain(p)
    assert r.status == "UNSAT" and r.minimal
    # "change" is exactly the set of requests in the minimal core
    from packaging.requirements import Requirement
    from crux.explain.render import req
    assert sorted(req(Requirement(x).name, Requirement(x).specifier)
                  for x in rec["change"]) == sorted(
        req_text(c) for c in r.core if c.kind == "root")


@pytest.mark.parametrize("rec", RECORDS, ids=lambda r: r["id"])
def test_every_fix_works(rec):
    for fix in rec["fixes"]:
        reqs = apply_fix(rec["requirements"], fix)
        p = problem_for(rec, reqs)
        res = solve(p, SOLVE)
        assert res.status == "SAT", (rec["id"], fix)
        assert check_plan(p, res.plan) == [], (rec["id"], fix)   # no violations


@pytest.mark.parametrize("rec", RECORDS, ids=lambda r: r["id"])
def test_cases_without_a_fix_really_have_none(rec):
    """No fix is claimed only when no single request can be changed: with
    any one request in the conflict loosened to any version, still UNSAT."""
    if rec["fixes"]:
        return
    from packaging.requirements import Requirement
    for i, x in enumerate(rec["requirements"]):
        loose = list(rec["requirements"])
        loose[i] = Requirement(x).name
        assert solve(problem_for(rec, loose), SOLVE).status == "UNSAT", rec["id"]


@pytest.mark.parametrize("rec", RECORDS, ids=lambda r: r["id"])
def test_ideal_shape(rec):
    lines = rec["ideal"].splitlines()
    assert 1 <= len(lines) <= 3 and all(line.strip() for line in lines)
    from packaging.requirements import Requirement
    names = {Requirement(x).name for x in rec["change"]}
    assert packages_named(rec["ideal"], names) == names, rec["id"]


def test_apply_fix():
    reqs = ["a==1", "b>=2", "c"]
    assert apply_fix(reqs, {"replace": {"a==1": "a<1"}}) == ["a<1", "b>=2", "c"]
    assert apply_fix(reqs, {"remove": ["c"]}) == ["a==1", "b>=2"]
    with pytest.raises(ValueError):
        apply_fix(reqs, {"replace": {"zz": "zz<1"}})


def test_compare_runs_on_the_toy():
    row = compare(RECORDS[0])
    assert isinstance(row, Row) and row.instance == "toy_abc"
    assert row.crux_names_change and row.crux_lines == 2
