"""Analysis of the user study (crux/benchmark/study_analysis.py): the exact
Wilcoxon test, the Holm correction, scoring and exclusions, and the whole
pipeline on simulated participants, which must find a built-in effect and
must not find one that is not there."""

import itertools
import json

import pytest

from crux.benchmark.study_analysis import (STUDY, _ranks, analyse, holm, main,
                                           per_participant, power, report, score,
                                           simulate_sessions, wilcoxon)

pytestmark = pytest.mark.skipif(not (STUDY / "items.json").exists(),
                                reason="study materials not generated")


# ── statistics ──────────────────────────────────────────────────────────

def brute(d):
    d = [x for x in d if x != 0]
    ranks = _ranks([abs(x) for x in d])
    w = sum(r for r, x in zip(ranks, d) if x > 0)
    sums = [sum(r for r, s in zip(ranks, signs) if s)
            for signs in itertools.product([0, 1], repeat=len(d))]
    lo = sum(s <= w + 1e-9 for s in sums) / len(sums)
    hi = sum(s >= w - 1e-9 for s in sums) / len(sums)
    return min(1.0, 2 * min(lo, hi))


def test_wilcoxon_textbook_values():
    assert wilcoxon([1, 2, 3, 4, 5]).p == pytest.approx(2 / 32)
    assert wilcoxon([-1, -2, -3, -4, -5, -6]).p == pytest.approx(2 / 64)
    assert wilcoxon([1, -1]).p == 1.0
    assert wilcoxon([]).p == 1.0 and wilcoxon([0, 0]).n == 0
    assert wilcoxon([0, 3, -1, 0, 2]).n == 3          # zeros dropped


@pytest.mark.parametrize("d", [
    [1, 1, 2, -2, 3, 3, 3], [0.5, -0.5, 1, 1, 1, -2], [4, 4, 4, 4, -4],
    [1, 2, 2, 3, -3, 3, 5, -5, 6], [-1, -1, -1, 2, 2],
])
def test_wilcoxon_exact_with_ties_matches_enumeration(d):
    assert wilcoxon(d).p == pytest.approx(brute(d))


def test_ranks_average_ties():
    assert _ranks([3, 1, 3, 2]) == [3.5, 1, 3.5, 2]


def test_holm():
    assert holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5}) == pytest.approx(
        {"a": 0.04, "c": 0.09, "b": 0.09, "d": 0.5})
    assert holm({"a": 0.9, "b": 0.6}) == pytest.approx({"b": 1.0, "a": 1.0})


# ── scoring ─────────────────────────────────────────────────────────────

ITEMS = {
    "S1": {"source": "real", "q1_options": ["a", "b", "c"], "q1_correct": ["a"],
           "q2_options": ["x", "y", "z"], "q2_correct": ["x"]},
    "S2": {"source": "planted", "q1_options": ["a", "b"], "q1_correct": ["a", "b"],
           "q2_options": [], "q2_correct": []},
}
ASSIGN = {(1, "S1"): "crux", (1, "S2"): "pip", (2, "S1"): "pip", (2, "S2"): "crux"}


def resp(item, style, q1, ms, q2=None, q2_ms=None, pos=1):
    return {"item": item, "style": style, "q1": q1, "q1_ms": ms, "q2": q2,
            "q2_ms": q2_ms, "position": pos}


def test_scoring_and_exclusions():
    sessions = [
        {"_file": "p1.json", "participant": 1, "background": {"pip_use": "Monthly"},
         "responses": [resp("S1", "crux", "a", 9000, "y", 2000),
                       resp("S2", "pip", "I can't tell", 400_000)]},
        {"_file": "p2.json", "participant": 2,
         "responses": [resp("S1", "uv", "a", 5000)]},       # wrong style
        {"_file": "p1b.json", "participant": 1, "responses": []},   # duplicate
    ]
    rows, problems = score(sessions, ITEMS, ASSIGN)
    assert len(rows) == 2
    s1, s2 = rows
    assert s1["q1_correct"] and s1["q2_asked"] and s1["q2_correct"] is False
    assert s1["pip_use"] == "Monthly"
    assert not s2["q1_correct"] and s2["q1_ms"] is None and s2["q2_correct"] is None
    text = "\n".join(problems)
    assert "over 5 minutes" in text and "assignment says pip" in text
    assert "appears twice" in text and "1 of 2 items answered" in text


def test_time_counts_correct_answers_only():
    rows = [
        {"participant": 1, "style": "crux", "q1_correct": True, "q1_ms": 4000},
        {"participant": 1, "style": "crux", "q1_correct": False, "q1_ms": 100},
        {"participant": 1, "style": "pip", "q1_correct": False, "q1_ms": 9000},
    ]
    pp = per_participant(rows)
    assert pp[1]["crux"] == {"acc": 0.5, "time": 4000}
    assert pp[1]["pip"] == {"acc": 0.0, "time": None}   # no correct answer: missing
    assert pp[1]["uv"] == {"acc": None, "time": None}


# ── the whole pipeline on simulated participants ────────────────────────

def run(n=18, seed=3, **kw):
    from crux.benchmark.study import assign
    from crux.benchmark.study_analysis import load_items
    items = load_items()
    assignments = {(int(r["participant"]), r["item"]): r["style"]
                   for r in assign(list(items.values()), n)}
    sessions = [s | {"_file": "sim"} for s in simulate_sessions(n, seed=seed, **kw)]
    rows, problems = score(sessions, items, assignments)
    return rows, problems, analyse(rows)


def test_a_large_built_in_effect_is_found():
    rows, problems, res = run(n=24, time_factor={"pip": 1.0, "uv": 0.9, "crux": 0.5})
    assert not problems
    assert res["H1"]
    t = res["tests"][("pip", "time")]
    assert t["effect"] > 0.3 and t["p_holm"] < 0.05


def test_no_effect_is_not_found():
    same_time = {"pip": 1.0, "uv": 1.0, "crux": 1.0}
    same_acc = {(s, src): 0.8 for s in ("pip", "uv", "crux") for src in ("real", "planted")}
    _, _, res = run(time_factor=same_time, acc=same_acc)
    assert not res["H1"] and not res["H2"]


def test_false_positive_rate_without_an_effect_is_low():
    same_time = {"pip": 1.0, "uv": 1.0, "crux": 1.0}
    same_acc = {(s, src): 0.8 for s in ("pip", "uv", "crux") for src in ("real", "planted")}
    r = power(18, reps=40, time_factor=same_time, acc=same_acc)
    assert r["H1"] <= 0.1 and r["H2"] <= 0.1


def test_report_on_simulated_data_says_so():
    rows, problems, res = run()
    text = report(rows, problems, res, simulated=True, sessions=18)
    assert text.startswith("# SIMULATED DATA: NOT STUDY RESULTS")
    assert "## The four tests" in text and "### By item" in text
    real = report(rows, problems, res, simulated=False, sessions=18)
    assert real.startswith("# User study results") and "SIMULATED" not in real


def test_cli_refuses_to_mix_or_to_simulate_into_real_responses(tmp_path):
    assert main(["--simulate", "3", "--responses", str(STUDY / "responses")]) == 2
    folder = tmp_path / "mixed"
    assert main(["--simulate", "3", "--responses", str(folder),
                 "--out", str(tmp_path / "r.md")]) == 0
    assert (tmp_path / "r.md").read_text().startswith("# SIMULATED")
    real = json.loads((folder / "crux-study-p01.json").read_text())
    real.pop("simulated")
    (folder / "crux-study-p99.json").write_text(json.dumps(real | {"participant": 99}))
    assert main(["--responses", str(folder), "--out", str(tmp_path / "r2.md")]) == 2


def test_significant_but_below_target_is_not_supported():
    # reliably faster, but by about 20%: significant, short of the 30% target
    rows, _, res = run(n=36, seed=5, time_factor={"pip": 1.0, "uv": 1.0, "crux": 0.8})
    t = res["tests"][("pip", "time")]
    assert t["significant"] and 0.1 < t["effect"] < 0.3
    assert not res["H1"]
