"""Scale families and the benchmark harness (offline)."""

import pytest

from crux.resolver import Config, Problem, solve
from crux.resolver import bench
from crux.resolver.bench import (
    Row, build_cases, format_table, implemented, main, node_reduction,
    problems, read_csv, run_case, run_suite, summarize, write_csv,
)
from crux.resolver.families import FAMILIES, Case, chain, pinned_pair, \
    random_csp, thrash
from crux.resolver.oracle import brute_force
from crux.resolver.problem import Result

C0 = Config.named("C0")


def nodes(case):
    r = solve(case.problem, C0)
    return r.stats.nodes, r.stats.backtracks, r.status


# ── families: known answers ──────────────────────────────────────────

@pytest.mark.parametrize("case", [thrash(2), thrash(3), pinned_pair(3),
                                  pinned_pair(5), chain(4)],
                         ids=lambda c: c.name)
def test_expected_status_matches_the_oracle(case):
    assert brute_force(case.problem).status == case.expected


@pytest.mark.parametrize("name", list(FAMILIES))
def test_expected_status_matches_c0_at_default_small_sizes(name):
    fam = FAMILIES[name]
    case = fam.build(fam.sizes[0], 0)
    r = solve(case.problem, C0)
    if case.expected:
        assert r.status == case.expected


def test_random_csp_has_unknown_answer_and_oracle_agrees_with_c0():
    for seed in range(6):
        c = random_csp(5, seed)
        assert c.expected is None
        assert solve(c.problem, C0).status == brute_force(c.problem).status


# ── families: they scale the way they claim ──────────────────────────

def test_thrash_is_exponential_for_plain_backtracking():
    counts = [nodes(thrash(n))[0] for n in range(3, 8)]
    for a, b in zip(counts, counts[1:]):
        assert 2.5 < b / a < 3.5, counts            # k = 3 per extra package


def test_pinned_pair_scans_every_release():
    for m in (5, 20, 100):
        n, back, status = nodes(pinned_pair(m))
        assert (n, back, status) == (m + 1, m - 1, "SAT")


def test_chain_is_linear_with_no_backtracking():
    for n in (5, 50, 500):
        assert nodes(chain(n)) == (n, 0, "SAT")


def test_random_csp_is_not_trivially_easy_near_the_default_density():
    # guards against the family drifting back into the trivial regime
    counts = sorted(nodes(random_csp(16, s))[0] for s in range(5))
    assert counts[2] > 200, counts
    statuses = {nodes(random_csp(16, s))[2] for s in range(12)}
    assert statuses == {"SAT", "UNSAT"}


def test_builders_are_deterministic_and_seed_matters_only_when_seeded():
    a, b = random_csp(10, 3), random_csp(10, 3)
    assert a.problem.roots == b.problem.roots
    assert a.problem.constraints() == b.problem.constraints()
    assert a.problem.constraints() != random_csp(10, 4).problem.constraints()
    assert thrash(4, 0).problem.constraints() == \
        thrash(4, 9).problem.constraints()


def test_case_names_and_registry():
    assert thrash(4, 2).name == "thrash/4/s2"
    assert set(FAMILIES) == {"thrash", "thrash_wide", "pinned_pair", "chain",
                             "random_csp"}
    assert all(f.sizes for f in FAMILIES.values())


def test_unseeded_families_are_built_once_per_size():
    cases = build_cases(["chain", "random_csp"], sizes=[6, 8],
                        seeds=range(3))
    names = [c.name for c in cases]
    assert names == ["chain/6/s0", "chain/8/s0"] + \
        [f"random_csp/{s}/s{k}" for s in (6, 8) for k in range(3)]


# ── harness: rows ────────────────────────────────────────────────────

def test_run_case_unsat_row():
    r = run_case(thrash(3), "C0")
    assert (r.status, r.nodes, r.backtracks) == ("UNSAT", 93, 93)
    assert r.correct is True and r.consistent is True
    assert r.valid_plan is None and r.expected == "UNSAT"
    assert r.wall_s > 0


def test_run_case_sat_row_checks_the_plan():
    r = run_case(chain(20), "C0")
    assert r.status == "SAT" and r.correct is True and r.valid_plan is True


def test_run_case_timeout_row_is_neither_correct_nor_incorrect():
    r = run_case(thrash(9), "C0", timeout_s=0.0)
    assert r.status == "TIMEOUT" and r.correct is None


def test_run_case_unknown_answer_has_no_correctness_verdict():
    r = run_case(random_csp(6, 0), "C0")
    assert r.expected == "" and r.correct is None


def test_repeats_take_the_median_wall_time_but_counts_once():
    r = run_case(chain(50), "C0", repeats=5)
    assert r.nodes == 50


# ── harness: configurations ──────────────────────────────────────────

def test_every_preset_is_implemented():
    assert all(implemented(Config.named(f"C{i}")) for i in range(5))


def _pretend_not_implemented(monkeypatch, *flags):
    """Make solve() refuse any config that turns one of *flags* on."""
    real = bench.solve

    def fake(problem, config):
        if any(getattr(config, f) for f in flags):
            raise NotImplementedError(flags)
        return real(problem, config)

    monkeypatch.setattr(bench, "solve", fake)


def test_unimplemented_configs_are_skipped_not_faked(monkeypatch):
    _pretend_not_implemented(monkeypatch, "mrv")          # hides C2, C3, C4
    assert not implemented(Config.named("C2"))
    rows, skipped = run_suite([chain(5)], ["C0", "C1", "C2", "C3", "C4"])
    assert skipped == ["C2", "C3", "C4"]
    assert {r.config for r in rows} == {"C0", "C1"}


def test_ac3_share_pruned_is_reported():
    rows, _ = run_suite([pinned_pair(50), thrash(3)], ["C0", "C1"])
    by = {(r.family, r.config): r for r in rows}
    assert by["pinned_pair", "C1"].ac3_pruned == 49
    assert by["pinned_pair", "C1"].ac3_versions == 51
    assert by["pinned_pair", "C0"].ac3_versions == 0
    shares = {(s["family"], s["config"]): s["ac3_share_pruned"]
              for s in summarize(rows)}
    assert shares["pinned_pair", "C1"] == pytest.approx(49 / 51)
    assert shares["pinned_pair", "C0"] is None
    assert shares["thrash", "C1"] == 0
    text = format_table(rows)
    assert "ac3 pruned" in text and "96.1%" in text


def test_c1_cuts_nodes_on_pinned_pair_and_the_reduction_is_measurable():
    rows, _ = run_suite([pinned_pair(100)], ["C0", "C1"])
    out = node_reduction(rows, "C0", "C1")
    assert out["cases"] == 1 and out["median"] == 101 / 2


def test_wrong_answer_is_flagged():
    unsat = thrash(2).problem
    fake = Case("fake", 1, 0, unsat, "SAT")            # claims SAT, is UNSAT
    rows, _ = run_suite([fake], ["C0"])
    assert rows[0].correct is False
    assert problems(rows) == rows


def test_disagreeing_configs_are_flagged(monkeypatch):
    real = bench.solve

    def fake_solve(problem, config):
        if config.ac3:                                   # pretend C1 exists
            return Result("SAT", plan={})
        return real(problem, config)

    monkeypatch.setattr(bench, "solve", fake_solve)
    rows, skipped = run_suite([thrash(2)], ["C0", "C1"])
    assert skipped == [] and len(rows) == 2
    assert all(not r.consistent for r in rows)
    assert len(problems(rows)) == 2


def test_timeouts_do_not_count_as_disagreement(monkeypatch):
    real = bench.solve

    def fake_solve(problem, config):
        if config.ac3:
            return Result("TIMEOUT")
        return real(problem, config)

    monkeypatch.setattr(bench, "solve", fake_solve)
    rows, _ = run_suite([thrash(2)], ["C0", "C1"])
    assert all(r.consistent for r in rows) and problems(rows) == []


# ── harness: summaries ───────────────────────────────────────────────

def _row(cfg, nodes_, status="SAT", seed=0, size=5):
    return Row("f", size, seed, cfg, status, nodes_, nodes_, 0, 0, 0.5, "",
               None, True, None, None)


def test_summarize_takes_medians_over_seeds():
    rows = [_row("C0", n, seed=i) for i, n in enumerate((10, 30, 20))]
    rows.append(_row("C0", 99, "TIMEOUT", seed=3))
    (s,) = summarize(rows)
    assert (s["runs"], s["solved"], s["timeouts"]) == (4, 3, 1)
    assert s["median_nodes"] == 25


def test_node_reduction():
    rows = [_row("C0", 100, seed=0), _row("C4", 10, seed=0),
            _row("C0", 90, seed=1), _row("C4", 30, seed=1),
            _row("C0", 500, "TIMEOUT", seed=2), _row("C4", 5, seed=2),
            _row("C0", 7, seed=3)]                       # no C4: ignored
    out = node_reduction(rows)
    assert out == {"cases": 2, "median": 6.5,
                   "base_timed_out_other_solved": 1,
                   "other_timed_out_base_solved": 0}
    lost = rows + [_row("C0", 40, seed=4), _row("C4", 900, "TIMEOUT", seed=4)]
    assert node_reduction(lost)["other_timed_out_base_solved"] == 1
    assert node_reduction(rows, "C0", "C9")["median"] is None


def test_format_table_has_a_header_and_one_line_per_group():
    rows, _ = run_suite([chain(5), chain(8)], ["C0"])
    lines = format_table(rows).splitlines()
    assert lines[0].split()[:3] == ["family", "size", "config"]
    assert len(lines) == 3


def test_csv_round_trip(tmp_path):
    rows, _ = run_suite([thrash(2), chain(6), random_csp(6, 0)], ["C0"])
    path = tmp_path / "deep" / "out.csv"
    write_csv(rows, path)
    assert read_csv(path) == rows


# ── harness: CLI ─────────────────────────────────────────────────────

def test_cli_runs_and_writes_csv(tmp_path, capsys, monkeypatch):
    out = tmp_path / "r.csv"
    code = main(["--families", "chain,pinned_pair", "--sizes", "5,10",
                 "--configs", "C0,C1", "--timeout", "5", "--out", str(out)])
    text = capsys.readouterr().out
    assert code == 0
    assert "not implemented yet" not in text      # C0 and C1 both run now
    assert len(read_csv(out)) == 2 * 2 * 2        # families x sizes x configs
    _pretend_not_implemented(monkeypatch, "learning")
    assert main(["--families", "chain", "--sizes", "5", "--configs",
                 "C0,C4"]) == 0
    assert "not implemented yet, skipped: C4" in capsys.readouterr().out


def test_cli_rejects_unknown_family():
    with pytest.raises(SystemExit):
        main(["--families", "nope"])


def test_cli_exit_code_is_nonzero_on_a_wrong_answer(monkeypatch, capsys):
    bad = Case("thrash", 2, 0, thrash(2).problem, "SAT")
    monkeypatch.setattr(bench, "build_cases", lambda *a, **k: [bad])
    assert main(["--configs", "C0", "--timeout", "5"]) == 1
    assert "PROBLEM" in capsys.readouterr().err
