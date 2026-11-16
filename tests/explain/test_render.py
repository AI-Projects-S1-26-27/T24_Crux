"""The English renderer (crux/explain/render.py).

Golden texts pin the wording for the shapes the corpus contains: a direct
clash, a request against a dependency, a deep chain, releases of one
package failing for different reasons, three requirements that clash only
together, contradicting requests and versions the snapshot lacks.  Then
properties over the committed benchmark: every message is complete, every
clash is confirmed, and every package of the core is named.
"""

import json
import random
import re
from pathlib import Path

import pytest

from crux.data.graph import Graph
from crux.explain import explain
from crux.explain.core import CoreResult
from crux.explain.render import (TARGET, message, pretty_spec, render,
                                 render_core, span)
from crux.resolver import Problem

ROOT = Path(__file__).parent.parent.parent
TOY = json.loads((ROOT / "tests" / "fixtures" / "toy_abc.json").read_text())


def problem(graph, *reqs, drops=None):
    g = graph if isinstance(graph, Graph) else Graph.from_dict(graph, drops=drops)
    return Problem.from_requirements(g, reqs)


def text(p):
    r = explain(p)
    assert r.status == "UNSAT" and r.minimal
    m = render_core(p, r.core)
    assert m.complete and m.checked
    return m.text


# ── golden messages ─────────────────────────────────────────────────────

def test_the_proposals_example():
    assert text(problem(TOY, "a>=1.0", "b>=1.0")) == (
        "No solution. Your requirements a>=1.0 and b>=1.0 cannot both hold:\n"
        "  every allowed version of a (1.0 to 1.4, 5 releases) needs c<2.0, "
        "but every allowed version of b (1.0 to 1.3, 4 releases) needs c>=3.1.")


def test_message_is_the_one_call():
    p = problem(TOY, "a>=1.0", "b>=1.0")
    assert message(p) == text(p)


def test_dependency_against_a_request():
    graph = {"grpcio-status": {"1.71.0": [["protobuf", "<6.0dev,>=5.26.1"]]},
             "protobuf": {"5.29.0": [], "6.30.2": []}}
    assert text(problem(graph, "grpcio-status==1.71.0", "protobuf==6.30.2")) == (
        "No solution. Your requirements grpcio-status==1.71.0 and "
        "protobuf==6.30.2 cannot both hold:\n"
        "  the only allowed version of grpcio-status (1.71.0) needs "
        "protobuf>=5.26.1,<6.0dev, but you asked for protobuf==6.30.2.")


def test_different_ranges_per_release_are_summarised():
    dep = {f"1.{i}": [] for i in range(30)} | {"0.5": []}
    pkg = {f"2.{i}": [["d", f">=1.{i},<1.{i + 1}"]] for i in range(30)}
    assert text(problem({"b": pkg, "d": dep}, "b", "d<=0.5")) == (
        "No solution. Your requirements b and d<=0.5 cannot both hold:\n"
        "  every allowed version of b (2.0 to 2.29, 30 releases) needs "
        "d>=1.0,<1.30 (the exact range differs by release), "
        "but you asked for d<=0.5.")


CHAIN = {"a": {"1.0": [["b", ">=1"]]}, "b": {"1.0": [["c", ">=2"]]},
         "c": {"1.0": [], "2.0": []}}


def test_deep_requirement_is_attributed_to_the_request():
    assert text(problem(CHAIN, "a", "c<2")) == (
        "No solution. Your requirements a and c<2 cannot both hold:\n"
        "  a needs c>=2 (through b), but you asked for c<2.")


def test_two_chains_meeting_far_from_the_requests():
    graph = {"x": {"1.0": [["p", ""]]}, "p": {"1.0": [["q", ""]]},
             "q": {"1.0": [["t", "<2"]]},
             "y": {"1.0": [["r", ""]]}, "r": {"1.0": [["t", ">=2"]]},
             "t": {"1.0": [], "2.0": []}}
    assert text(problem(graph, "x", "y")) == (
        "No solution. Your requirements x and y cannot both hold:\n"
        "  x needs t<2 (through p → q), but y needs t>=2 (through r).")


PARTIAL = {   # planted/partial/d1/none/s0
    "bramble": {"3.3.0": [], "3.3.1": [["ochre", "<1.2.1"]],
                "3.4.0": [["egret", ">=1.4.0"]]},
    "cinder": {"1.0.0": [["ochre", ">=1.2.3"]], "2.0.0": [["ochre", ">=1.2.3"]],
               "2.0.1": [["ochre", ">=1.2.3"]]},
    "ochre": {"1.2.0": [], "1.2.3": []},
    "egret": {"1.3.0": []},
}


def test_releases_of_one_package_failing_for_different_reasons():
    assert text(problem(PARTIAL, "bramble>=3.3.1", "cinder>=1.0.0")) == (
        "No solution. Your requirements bramble>=3.3.1 and cinder>=1.0.0 "
        "cannot both hold:\n"
        "  bramble 3.4.0 needs egret>=1.4.0, but no release of egret matches "
        "(the newest in the snapshot is 1.3.0).\n"
        "  bramble 3.3.1 needs ochre<1.2.1, but every allowed version of "
        "cinder (1.0.0, 2.0.0 and 2.0.1) needs ochre>=1.2.3.")


def test_several_releases_take_a_plural_verb():
    graph = {"m": {"1.0": [["x", "==9"]], "1.1": [["x", "==9"]],
                   "2.0": [["y", "==9"]]},
             "x": {"1.0": []}, "y": {"1.0": []}}
    t = text(problem(graph, "m"))
    assert "m 1.0 and 1.1 need x==9, but" in t
    assert "m 2.0 needs y==9, but" in t


def test_three_requirements_that_clash_only_together():
    graph = {"a": {"1": [["c", "<3"]]}, "b": {"1": [["c", ">=2"]]},
             "d": {"1": [["c", "!=2"]]}, "c": {"1": [], "2": [], "3": []}}
    assert text(problem(graph, "a", "b", "d")) == (
        "No solution. Your requirements a, b and d cannot all hold:\n"
        "  the only allowed version of a (1) needs c<3, the only allowed "
        "version of b (1) needs c>=2 and the only allowed version of d (1) "
        "needs c!=2; no version of c satisfies all of them.")


def test_contradicting_requests():
    assert text(problem({"six": {"1.15.0": [], "1.16.0": []}},
                        "six==1.16.0", "six~=1.15.0")) == (
        "No solution. Your requirements six==1.16.0 and six~=1.15.0 "
        "cannot both hold:\n"
        "  no version of six satisfies both ==1.16.0 and ~=1.15.0.")


# ── versions the snapshot lacks ─────────────────────────────────────────

def test_request_for_a_package_with_no_releases():
    assert text(problem({"other": {"1.0": []}}, "meadow>=2.2")) == (
        "No solution. Your requirement meadow>=2.2 cannot be met:\n"
        "  meadow has no releases in the snapshot.")


def test_request_for_a_version_that_does_not_exist():
    assert text(problem({"x": {"1.0": [], "1.1": []}}, "x>=2")) == (
        "No solution. Your requirement x>=2 cannot be met:\n"
        "  no release of x matches (the newest in the snapshot is 1.1).")


def test_missing_version_with_its_drop_reason():
    graph = {"app": {"1.0": [["sqlalchemy", "==1.4.29"]]},
             "sqlalchemy": {"2.0.0": []}}
    t = text(problem(graph, "app", drops={"sqlalchemy": [["1.4.29", "no_wheel"]]}))
    assert t == (
        "No solution. Your requirement app cannot be met:\n"
        "  the only allowed version of app (1.0) needs sqlalchemy==1.4.29, "
        f"but sqlalchemy 1.4.29 has no wheel for {TARGET}.")
    assert TARGET == "CPython 3.12 on Linux x86-64"


def test_several_missing_versions_with_mixed_reasons():
    graph = {"a": {"1.0": [["d", ">=3"]]}, "d": {"1.0": []}}
    drops = {"d": [["3.0", "no_wheel"], ["3.1", "no_wheel"], ["3.2", "yanked"]]}
    assert text(problem(graph, "a", drops=drops)).endswith(
        "but none of the 3 matching releases of d is usable: "
        f"2 have no wheel for {TARGET} and 1 was yanked.")


def test_several_missing_versions_with_one_reason():
    graph = {"a": {"1.0": [["d", ">=3"]]}, "d": {"1.0": []}}
    drops = {"d": [["3.0", "py_version"], ["3.1", "py_version"]]}
    assert text(problem(graph, "a", drops=drops)).endswith(
        "but the 2 matching releases of d all require a Python other "
        "than 3.12.")


# ── status and caveats ──────────────────────────────────────────────────

def test_unproven_core_says_so():
    p = problem(TOY, "a", "b")
    r = explain(p)
    m = render_core(p, r.core, minimal=False)
    assert m.lines[-1] == ("  (Crux could not prove that this is the smallest "
                           "set of conflicting requirements.)")


def test_status_other_than_unsat():
    p = problem(TOY, "a")
    assert render(p, CoreResult(status="SAT", method="deletion")) == \
        "Crux found versions that satisfy every requirement."
    assert "time limit" in render(p, CoreResult(status="TIMEOUT", method="deletion"))
    assert "could not find which" in render(p, CoreResult(status="UNSAT",
                                                          method="deletion"))
    assert message(p) == "Crux found versions that satisfy every requirement."


def test_order_of_the_core_does_not_matter():
    p = problem(PARTIAL, "bramble>=3.3.1", "cinder>=1.0.0")
    core = list(explain(p).core)
    want = render_core(p, core).text
    rng = random.Random(1)
    for _ in range(5):
        rng.shuffle(core)
        assert render_core(p, core).text == want


def test_wrapping_keeps_lines_unwrapped_for_counting():
    p = problem(TOY, "a>=1.0", "b>=1.0")
    m = render_core(p, explain(p).core)
    out = m.wrapped(60)
    assert all(len(line) <= 60 for line in out.splitlines())
    assert len(out.splitlines()) > len(m.lines) == 2
    assert " ".join(out.split()) == " ".join(m.text.split())
    assert message(p, width=60) == out


# ── helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec, want", [
    ("<2.0,>=1.0", ">=1.0,<2.0"),
    ("", ""),
    ("!=1.3,<2,>=1", ">=1,<2,!=1.3"),
    ("==1.4", "==1.4"),
])
def test_pretty_spec(spec, want):
    assert pretty_spec(spec) == want


@pytest.mark.parametrize("vs, want", [
    (["1.0"], "1.0"), (["1.0", "1.1"], "1.0 and 1.1"),
    (["1", "2", "3"], "1, 2 and 3"), (["1", "2", "3", "4"], "1 to 4 (4 releases)"),
])
def test_span(vs, want):
    assert span(vs) == want


# ── the committed benchmark ─────────────────────────────────────────────

REAL = ROOT / "benchmarks" / "results" / "cores" / "real_cores.jsonl"
REAL_INST = ROOT / "benchmarks" / "corpus" / "instances.jsonl.gz"
PLANTED = ROOT / "benchmarks" / "planted" / "instances.jsonl"


@pytest.mark.skipif(not (REAL.exists() and REAL_INST.exists() and PLANTED.exists()),
                    reason="benchmark files not present")
def test_every_benchmark_message_is_complete_and_names_the_core():
    from crux.benchmark.instance import constraint_from_json, load_jsonl
    insts = {i.id: i for i in load_jsonl(REAL_INST)}
    cases = [(insts[d["instance"]], [constraint_from_json(c) for c in d["core"]])
             for d in map(json.loads, REAL.read_text().splitlines())]
    cases += [(i, list(i.core)) for i in load_jsonl(PLANTED)]
    assert len(cases) == 61 + 198
    for inst, core in cases:
        m = render_core(inst.problem(), core)
        assert m.complete and m.checked, inst.id
        words = set(re.findall(r"[a-z0-9][a-z0-9._-]*", m.text))
        for c in core:
            assert c.dep in words and (c.pkg is None or c.pkg in words), inst.id
            if c.kind == "root":
                assert c.dep in m.lines[0], inst.id
        assert len(m.lines) <= 5, inst.id


# ── safeguards: what the message does when the input is not a clean core ─

def test_clauses_follow_the_headline_order():
    graph = {"alpha": {"1.0": [["t", "<2"]]},
             "zeta": {"1.0": [["t", ">=2"]], "1.1": [["t", ">=2"]]},
             "t": {"1.0": [], "2.0": []}}
    assert text(problem(graph, "alpha", "zeta")).splitlines()[1] == (
        "  the only allowed version of alpha (1.0) needs t<2, but every "
        "allowed version of zeta (1.0 and 1.1) needs t>=2.")


def test_chain_from_some_releases_of_the_request():
    graph = {"a": {"1.0": [["b", ""]], "2.0": [["c", ""]]},
             "b": {"1.0": [["t", ">=5"]]}, "c": {"1.0": [["u", "==9"]]},
             "t": {"1.0": []}, "u": {"1.0": []}}
    assert text(problem(graph, "a")).splitlines()[1:] == [
        "  a 1.0 needs t>=5 (through b), but no release of t matches "
        "(the newest in the snapshot is 1.0).",
        "  a 2.0 needs u==9 (through c), but no release of u matches "
        "(the newest in the snapshot is 1.0)."]


def test_a_clash_that_is_not_one_is_flagged():
    from crux.resolver.problem import Constraint
    p = problem(TOY, "a", "b")
    fake = [Constraint.root("a"), Constraint.root("b"),
            Constraint.edge("a", "1.0", "c", "<2.0"),
            Constraint.edge("b", "1.0", "c", "<3.0")]      # both allow c 1.x
    assert not render_core(p, fake).checked


def test_a_group_off_every_path_is_still_shown():
    from crux.resolver.problem import Constraint
    graph = {"a": {"1.0": [["p", ""]]}, "b": {"1.0": [["p", ""]]},
             "p": {"1.0": [["t", "<2"]]}, "t": {"1.0": [], "2.0": []}}
    p = problem(graph, "a", "b", "t>=2")
    core = [Constraint.root("a"), Constraint.root("b"), Constraint.root("t", ">=2"),
            Constraint.edge("a", "1.0", "p", ""), Constraint.edge("b", "1.0", "p", ""),
            Constraint.edge("p", "1.0", "t", "<2")]          # b -> p is redundant
    m = render_core(p, core)
    assert not m.complete
    assert m.lines[-1] == ("  Also involved: the only allowed version of b (1.0) "
                           "needs p.")
