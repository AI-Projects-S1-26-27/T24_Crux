"""User-study materials (crux/benchmark/study.py): the items follow the
selection rule, every answer key is consistent with the data, the Latin
square is balanced, and the form carries no answers."""

import json
from collections import Counter

import pytest

from crux.benchmark.study import (MIN_Q2, RESULTS, STYLES, assign, form_html,
                                  practice_item, select_items, targets)

pytestmark = pytest.mark.skipif(
    not (RESULTS / "real" / "crux_messages.jsonl").exists(),
    reason="run python -m crux.benchmark.crux_messages first")


@pytest.fixture(scope="module")
def items():
    return select_items()


def test_the_selection_rule_gives_these_eight(items):
    assert [(i["id"], i["instance"]) for i in items] == [
        ("S1", "real/Comfy-Org/ComfyUI#8069"),
        ("S2", "real/miaoshouai/ComfyUI-Miaoshouai-Tagger#52"),
        ("S3", "real/NVIDIA-NeMo/DataDesigner#1007"),
        ("S4", "real/Sport-Performance-Lab/mplbasketball#26"),
        ("S5", "planted/deep/d3/large/s3"),
        ("S6", "planted/deep/d4/small/s2"),
        ("S7", "planted/deep/d5/small/s1"),
        ("S8", "planted/deep/d6/small/s2")]


def test_answer_keys_are_consistent(items):
    for it in items:
        assert it["q1_correct"] and set(it["q1_correct"]) <= set(it["q1_options"])
        assert it["q1_options"] == it["requirements"]
        if it["q2_options"]:
            assert len(it["q2_options"]) >= MIN_Q2
            assert it["q2_correct"] and set(it["q2_correct"]) <= set(it["q2_options"])
        else:
            assert it["q2_correct"] == []


def test_planted_items_have_a_wrong_answer_to_pick(items):
    for it in items:
        if it["source"] == "planted":
            assert len(it["q1_correct"]) < len(it["q1_options"]), it["id"]
            assert it["q2_options"], it["id"]


def test_every_style_has_the_benchmarks_message(items):
    for it in items:
        assert set(it["messages"]) == set(STYLES)
        assert it["messages"]["pip"].startswith("ERROR:")
        assert "No solution found" in it["messages"]["uv"]
        assert it["messages"]["crux"].startswith("No solution.")
        for text in it["messages"].values():
            assert "/tmp" not in text and "wheels" not in text


def test_targets_on_the_toy():
    import json as j
    from pathlib import Path
    from crux.data.graph import Graph
    from crux.explain import explain
    from crux.resolver import Problem
    toy = j.loads((Path(__file__).parent.parent / "fixtures" / "toy_abc.json").read_text())
    p = Problem.from_requirements(Graph.from_dict(toy), ["a", "b"])
    assert targets(p, explain(p).core) == ["c"]


def test_latin_square_is_balanced(items):
    rows = assign(items, 18)
    assert len(rows) == 18 * 8
    cell = Counter((r["item"], r["style"]) for r in rows)
    assert set(cell.values()) == {6}                        # each item x style 6 times
    for p in range(1, 19):
        mine = [r for r in rows if r["participant"] == p]
        assert sorted(r["position"] for r in mine) == list(range(1, 9))
        assert {r["item"] for r in mine} == {i["id"] for i in items}
        assert sorted(Counter(r["style"] for r in mine).values()) == [2, 3, 3]
    assert assign(items, 18) == rows                        # deterministic
    orders = {tuple(r["item"] for r in rows if r["participant"] == p) for p in range(1, 19)}
    assert len(orders) > 1                                  # order varies


def test_form_has_no_answers(items):
    page = form_html(items, practice_item(), assign(items, 3))
    data = json.loads(page.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    for it in data["items"]:
        assert "q1_correct" not in it and "q2_correct" not in it
    assert data["practice"]["q1_correct"]                  # practice gives feedback
    assert "/*DATA*/" not in page
