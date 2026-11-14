"""The ablation module: statistics, report, files (offline, tiny sizes)."""

import pytest

from crux.resolver import ablation
from crux.resolver.ablation import (
    build_report, main, reduction_stats, reduction_table, run_ablation,
)
from crux.resolver.bench import Row, read_csv


def row(cfg, nodes, status="SAT", seed=0, size=5, family="f"):
    return Row(family, size, seed, cfg, status, nodes, nodes, 0, 0, 0.5, "",
               None, True, None, None)


# ── reduction_stats: how timeouts and ties are treated ───────────────

def test_ratio_is_base_over_other_and_median_is_over_finished_cases():
    rows = []
    for seed, (b, o) in enumerate([(100, 10), (90, 30), (50, 50), (10, 40)]):
        rows += [row("C0", b, seed=seed), row("C4", o, seed=seed)]
    s = reduction_stats(rows)
    assert (s["cases"], s["both_finished"]) == (4, 4)
    assert (s["wins"], s["ties"], s["losses"]) == (2, 1, 1)
    assert s["median"] == pytest.approx(2.0)      # ratios 10, 3, 1, 0.25
    assert s["worst"] == pytest.approx(0.25) and s["best"] == 10


def test_a_base_timeout_is_a_lower_bound_and_kept_out_of_the_plain_median():
    rows = [row("C0", 100, seed=0), row("C4", 10, seed=0),         # ratio 10
            row("C0", 5000, "TIMEOUT", seed=1), row("C4", 5, seed=1)]
    s = reduction_stats(rows)
    assert s["both_finished"] == 1 and s["base_timed_out"] == 1
    assert s["median"] == 10                          # only the finished case
    assert s["lower_bound_median"] == pytest.approx((10 + 1000) / 2)
    assert s["lower_bound_median"] >= s["median"]


def test_a_loss_by_timeout_is_counted_and_not_hidden():
    rows = [row("C0", 100, seed=0), row("C4", 10, seed=0),
            row("C0", 7, seed=1), row("C4", 9000, "TIMEOUT", seed=1)]
    s = reduction_stats(rows)
    assert s["other_timed_out"] == 1
    assert s["both_finished"] == 1                    # the lost case is not
    assert s["median"] == 10                          # averaged away
    # and the lower bound must account for it: the lost case's true ratio is
    # at most 7/9000, so it enters at 0 and pulls the bound down.  The first
    # version dropped it and reported 10, which is not a lower bound.
    assert s["lower_bound_median"] == pytest.approx(5.0)


def test_the_lower_bound_median_is_never_above_the_true_median():
    # brute check of the definition: assign every timed-out run an arbitrary
    # true node count >= what it reached, recompute the true median ratio,
    # and confirm the reported bound never exceeds it
    import random
    rng = random.Random(0)
    for trial in range(300):
        rows, true_pairs = [], []
        for seed in range(rng.randint(1, 9)):
            b, o = rng.randint(1, 500), rng.randint(1, 500)
            bs = "TIMEOUT" if rng.random() < 0.3 else "SAT"
            os_ = "TIMEOUT" if rng.random() < 0.3 else "SAT"
            rows += [row("C0", b, bs, seed=seed), row("C4", o, os_, seed=seed)]
            true_b = b * (rng.uniform(1, 50) if bs == "TIMEOUT" else 1)
            true_o = o * (rng.uniform(1, 50) if os_ == "TIMEOUT" else 1)
            true_pairs.append(true_b / true_o)
        bound = reduction_stats(rows)["lower_bound_median"]
        import statistics as st
        assert bound <= st.median(true_pairs) + 1e-9, (trial, bound)


def test_both_timing_out_is_its_own_count():
    rows = [row("C0", 9000, "TIMEOUT"), row("C4", 8000, "TIMEOUT")]
    s = reduction_stats(rows)
    assert s["both_timed_out"] == 1 and s["median"] is None
    assert s["lower_bound_median"] == 0.0             # nothing can be claimed


def test_cases_missing_a_configuration_are_ignored():
    rows = [row("C0", 100, seed=0), row("C4", 10, seed=0),
            row("C0", 70, seed=1)]
    assert reduction_stats(rows)["cases"] == 1


def test_reduction_table_has_one_line_per_family_and_size():
    rows = []
    for fam, size in [("chain", 5), ("chain", 8), ("thrash", 3)]:
        rows += [row("C0", 10, size=size, family=fam),
                 row("C4", 5, size=size, family=fam)]
    t = reduction_table(rows)
    assert [(r["family"], r["size"]) for r in t] == [
        ("thrash", 3), ("chain", 5), ("chain", 8)]    # registry order
    assert all(r["median"] == 2 for r in t)


# ── a real, tiny ablation ────────────────────────────────────────────

@pytest.fixture(scope="module")
def tiny():
    rows, skipped = run_ablation(["thrash_wide", "pinned_pair", "chain"],
                                 [4, 6], range(1), timeout_s=10, repeats=1)
    return rows, skipped


def test_all_five_configs_run_and_agree(tiny):
    rows, skipped = tiny
    assert skipped == []
    assert {r.config for r in rows} == {"C0", "C1", "C2", "C3", "C4"}
    assert len(rows) == 3 * 2 * 5
    assert all(r.correct is not False and r.consistent for r in rows)


def test_the_headline_numbers_are_the_known_ones(tiny):
    rows, _ = tiny
    by = {(r.family, r.size, r.config): r for r in rows}
    assert by["thrash_wide", 6, "C4"].nodes == 13            # n + 7
    assert by["thrash_wide", 6, "C0"].nodes > 5_000          # ~3**n blow-up
    assert by["pinned_pair", 6, "C1"].nodes == 2
    t = {(r["family"], r["size"]): r for r in reduction_table(rows)}
    assert t["thrash_wide", 6]["median"] > 100               # C0 -> C4
    assert t["chain", 6]["median"] == 1.0                    # nothing to gain


def test_report_contains_the_tables_and_the_caveats(tiny):
    rows, skipped = tiny
    text = build_report(rows, settings={"seeds": 1}, skipped=skipped)
    for needle in ("# Ablation: C0 to C4", "## Headline: node reduction",
                   "## What each technique adds", "## Per-family detail",
                   "### thrash_wide", "Synthetic and easier than real",
                   "Medians, not sums", "Timeouts are censored",
                   "no wrong answers"):
        assert needle in text, needle
    assert "median >=" in text and "C4 t/o" in text


def test_report_refuses_to_look_clean_when_something_is_wrong(tiny):
    rows, skipped = tiny
    bad = list(rows)
    bad[0] = Row(**{**bad[0].__dict__, "correct": False})
    text = build_report(bad, settings={}, skipped=skipped)
    assert "PROBLEM ROWS" in text and "no wrong answers" not in text


def test_a_later_config_that_times_out_is_named_not_left_blank():
    rows = []
    for cfg, status in (("C0", "SAT"), ("C1", "SAT"), ("C2", "SAT"),
                        ("C3", "TIMEOUT"), ("C4", "SAT")):
        rows.append(row(cfg, 50, status, family="chain", size=7))
    text = build_report(rows, settings={})
    line = next(l for l in text.splitlines() if l.startswith("| chain | 7 |")
                and "t/o on" in l)
    assert "(C3 t/o on 1)" in line
    assert "0.00 (C3 t/o on 1)" in line      # the loss pulls the bound to 0


def test_both_timing_out_is_named_too():
    rows = [row(c, 50, "TIMEOUT", family="chain", size=7)
            for c in ("C0", "C1", "C2", "C3", "C4")]
    text = build_report(rows, settings={})
    assert "(C1 t/o on 1)" in text and "(C4 t/o on 1)" in text


# ── CLI and files ────────────────────────────────────────────────────

def test_from_rows_rebuilds_everything_without_running_anything(tmp_path,
                                                                monkeypatch):
    first = tmp_path / "first"
    assert main(["--out", str(first), "--families", "thrash_wide,chain",
                 "--sizes", "4,5", "--seeds", "1", "--repeats", "1",
                 "--no-plots"]) == 0

    def boom(*a, **k):                       # re-running anything is a bug
        raise AssertionError("must not run the solver")

    monkeypatch.setattr(ablation, "run_ablation", boom)
    second = tmp_path / "second"
    assert main(["--from-rows", str(first / "rows.csv"), "--out",
                 str(second), "--no-plots"]) == 0
    assert read_csv(second / "rows.csv") == read_csv(first / "rows.csv")
    headline = lambda p: (p / "report.md").read_text(encoding="utf-8").split(
        "## What each technique adds")[0].split("## Headline")[1]
    assert headline(second) == headline(first)

def test_cli_writes_every_file(tmp_path, capsys):
    out = tmp_path / "ab"
    code = main(["--out", str(out), "--families", "thrash_wide,chain",
                 "--sizes", "4,5", "--seeds", "1", "--repeats", "1",
                 "--timeout", "10", "--no-plots"])
    assert code == 0
    for name in ("rows.csv", "summary.csv", "reduction.csv", "report.md"):
        assert (out / name).is_file(), name
    assert len(read_csv(out / "rows.csv")) == 2 * 2 * 5
    assert not (out / "nodes_by_size.png").exists()
    assert "wrote rows.csv" in capsys.readouterr().out


def test_cli_rejects_an_unknown_family():
    with pytest.raises(SystemExit):
        main(["--families", "nope"])


def test_plots_are_written_when_matplotlib_is_available(tmp_path, tiny):
    pytest.importorskip("matplotlib")
    paths = ablation.make_plots(tiny[0], tmp_path)
    assert [p.name for p in paths] == ["nodes_by_size.png", "reduction.png"]
    assert all(p.stat().st_size > 5_000 for p in paths)
