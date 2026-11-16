"""Crux's messages on the benchmark (crux/benchmark/crux_messages.py).

Uses planted/direct/d1/none/s0, whose real pip 26.2.1 and uv 0.11.32 output
is in tests/fixtures/benchmark, so all three tools are measured on one
instance by the same code."""

import json
from pathlib import Path

import pytest

from crux.benchmark.crux_messages import (WIDTH, crux_row, summary, tool_rows,
                                          truth)
from crux.benchmark.instance import Instance, constraint_to_json
from crux.benchmark.messages import measure, message

FIX = Path(__file__).parent.parent / "fixtures" / "benchmark"

DIRECT = Instance(   # planted/direct/d1/none/s0, gadget only
    id="planted/direct/d1/none/s0", expected="UNSAT",
    roots=(("glyph", ">=1.1.0"), ("tamarack", "")),
    graph={"glyph": {"1.0.0": [["beacon", "<1.7.0"]], "1.1.0": [["beacon", "<1.7.1"]]},
           "tamarack": {"1.0.0": [["beacon", ">=1.7.1"]], "2.0.0": [["beacon", ">=1.7.1"]]},
           "beacon": {"1.6.1": [], "1.7.0": [], "1.7.1": [], "1.7.2": []}},
    meta={"kind": "direct", "depth": 1})


def planted_core(inst):
    from crux.explain import explain
    return frozenset(explain(inst.problem()).core)


def test_crux_message_is_measured_like_pip_and_uv():
    row = crux_row(DIRECT, planted_core(DIRECT))
    assert row["tool"] == "crux" and row["status"] == "UNSAT"
    assert row["complete"] and row["checked"] and row["minimal"]
    text = row["stdout"]
    assert text.startswith("No solution. Your requirements glyph>=1.1.0 and tamarack")
    assert all(len(line) <= WIDTH for line in text.splitlines())
    assert row["msg_lines"] == len([x for x in text.splitlines() if x.strip()])
    assert row["words"] == len(text.split())
    assert row["named_list"] == "beacon glyph tamarack"
    assert row["names_change"] and row["core_recall"] == 1.0
    assert row["core_precision"] == 1.0
    assert row["statements"] == 4               # 2 requests + 2 edge groups
    assert row["logical_lines"] == 2


def test_crux_is_shorter_than_pip_and_uv_on_the_same_instance():
    core = planted_core(DIRECT)
    crux = crux_row(DIRECT, core)
    kw = dict(packages={"glyph", "tamarack", "beacon"}, roots=["glyph", "tamarack"])
    pip = measure("pip", (FIX / "pip26_direct.txt").read_text(), "", **kw)
    uv = measure("uv", "", (FIX / "uv_direct.txt").read_text(), **kw)
    assert crux["words"] < uv["words"] < pip["words"]
    assert crux["msg_lines"] < uv["msg_lines"] < pip["msg_lines"]


def test_message_rule_for_crux_is_the_whole_text():
    assert message("crux", "a\nb") == "a\nb"
    m = measure("crux", "No solution.\n  x needs y.", "", packages={"x", "y"},
                roots=["x"])
    assert m["msg_lines"] == 2 and m["words"] == 5 and m["named"] == 2


def test_truth_planted_unique_real_and_ambiguous_real():
    from crux.resolver.problem import Constraint
    c = Constraint.root("x", "")
    planted = Instance(id="p", expected="UNSAT", roots=(("x", ""),),
                       graph={}, core=frozenset({c}))
    assert truth(planted, {}) == frozenset({c})
    real = Instance(id="r", expected="UNSAT", roots=(("x", ""),), graph={})
    cores = {"r": {"unique": "unique", "core": [constraint_to_json(c)]}}
    assert truth(real, cores) == frozenset({c})
    cores["r"]["unique"] = "not_unique"
    assert truth(real, cores) is None
    assert truth(real, {}) is None


def test_saved_tool_output_is_remeasured(tmp_path):
    part = tmp_path / "planted"
    part.mkdir()
    rows = [
        {"instance": DIRECT.id, "tool": "pip", "status": "UNSAT",
         "stdout": (FIX / "pip26_direct.txt").read_text(), "stderr": ""},
        {"instance": DIRECT.id, "tool": "uv", "status": "UNSAT", "stdout": "",
         "stderr": (FIX / "uv_direct.txt").read_text()},
        {"instance": DIRECT.id, "tool": "pip-baseline", "status": "TIMEOUT",
         "stdout": "", "stderr": ""},          # no message: skipped
        {"instance": DIRECT.id, "tool": "crux-e2e", "status": "UNSAT",
         "stdout": "", "stderr": ""},          # solver only: skipped
    ]
    (part / "messages.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    core = planted_core(DIRECT)
    got = tool_rows(part, {DIRECT.id: DIRECT}, {DIRECT.id: core})
    assert [r["tool"] for r in got] == ["pip", "uv"]
    pip, uv = got
    assert pip["names_change"] and uv["names_change"]
    assert pip["msg_lines"] == 11 and uv["msg_lines"] == 6     # as in test_messages.py


def test_summary_states_the_target():
    def row(tool, lines, words):
        return {"instance": "i", "tool": tool, "status": "UNSAT",
                "msg_lines": lines, "words": words}
    rows = [row("pip", 10, 80), row("pip-baseline", 8, 60), row("uv", 6, 40),
            row("crux", 4, 24)]
    out = summary(rows, scored=False)
    assert "Lines: Crux 4 vs pip 10 (met); words: Crux 24 vs pip 80 (met)." in out
    rows[-1] = row("crux", 6, 50)
    assert "(not met)" in summary(rows, scored=False)


def test_pips_fixed_advice_can_be_left_out():
    from crux.benchmark.crux_messages import without_advice
    msg = message("pip", (FIX / "pip26_direct.txt").read_text())
    core = without_advice(msg)
    assert "To fix this" not in core and "loosen" not in core
    assert "ResolutionImpossible" not in core
    assert "The conflict is caused by:" in core and "beacon" in core
    assert sum(1 for x in core.splitlines() if x.strip()) == 7


def test_summary_reports_the_stricter_count():
    def row(tool, lines, words, **kw):
        return {"instance": "i", "tool": tool, "status": "UNSAT",
                "msg_lines": lines, "words": words, **kw}
    rows = [row("pip", 10, 80, lines_no_advice=6, words_no_advice=44),
            row("pip-baseline", 8, 60), row("uv", 6, 40), row("crux", 4, 24)]
    out = summary(rows, scored=False)
    assert "pip's median is 6 lines and 44 words" in out
    assert "67% of that in lines and 55% in words" in out and "not met" in out
