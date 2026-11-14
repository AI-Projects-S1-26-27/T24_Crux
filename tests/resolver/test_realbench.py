"""realbench on a tiny database with the real (cleaned) schema (offline)."""

import re
import sqlite3

import pytest

from crux.data.graph import Graph
from crux.resolver import realbench
from crux.resolver.realbench import (
    CONFIGS, build_report, graph_facts, load_graph, main, pick_cases,
    problems, run_case,
)


def make_db(path, packages):
    """packages = {name: {version: [(dep, spec), ...]}} -> cleaned-schema db."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE releases (name TEXT, version TEXT, upload_time TEXT,
            is_prerelease INTEGER, requires_python TEXT, filename TEXT,
            url TEXT, sha256 TEXT);
        CREATE TABLE deps (name TEXT, version TEXT, dep_name TEXT,
            specifier TEXT);
    """)
    for name, vs in packages.items():
        for v, edges in vs.items():
            conn.execute("INSERT INTO releases VALUES (?,?,?,?,?,?,?,?)",
                         (name, v, None, 0, None, "f", "u", "s"))
            for dep, spec in edges:
                conn.execute("INSERT INTO deps VALUES (?,?,?,?)",
                             (name, v, dep, spec))
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    m = 12
    pkgs = {
        "boto3": {str(v): [("botocore", f"=={v}"), ("six", ">=1")]
                  for v in range(1, m + 1)},
        "botocore": {str(v): [("six", ">=1")] for v in range(1, m + 1)},
        "six": {"1": [], "2": []},
        "requests": {str(v): [("urllib3", "<3"), ("six", "")]
                     for v in range(1, 6)},
        "urllib3": {str(v): [] for v in range(1, 5)},
        "flask": {"2": [("requests", ">=3")], "1": [("requests", "<9")]},
    }
    path = tmp_path / "crux.db"
    make_db(path, pkgs)
    return path


def test_graph_loads_with_time_and_memory_and_facts(db):
    g, seconds, peak = load_graph(db)
    assert seconds > 0 and peak > 0
    facts = graph_facts(g)
    assert facts["packages"] == 6
    assert facts["releases"] == 12 + 12 + 2 + 5 + 4 + 2
    assert facts["dangling"] == 0 and facts["collisions"] == 0
    assert facts["largest"][0][0] in ("boto3", "botocore")


def test_cases_come_from_what_the_graph_contains(db):
    g, _, _ = load_graph(db)
    cases = pick_cases(g)
    names = [n for n, _ in cases]
    assert "pair:boto3+botocore" in names
    assert any(n.startswith("pin:boto3+botocore<=") for n in names)
    assert any(n.startswith("pin:boto3+botocore==") for n in names)
    assert "single:requests" in names and "single:flask" in names
    keys = [tuple(sorted(r)) for _, r in cases]
    assert len(keys) == len(set(keys))                # no duplicates, in any
    assert "pair:botocore+boto3" not in names         # order (was counted twice)
    packages = set(g._versions)
    for _, reqs in cases:
        for r in reqs:
            assert re.split(r"[<>=!~]", r)[0] in packages, r
    assert any(n.startswith("backtrack:requests+urllib3<=") for n in names)
    assert any(n.startswith("conflict:boto3>=") for n in names)


def test_pick_cases_is_deterministic_and_respects_the_limit(db):
    g, _, _ = load_graph(db)
    assert pick_cases(g) == pick_cases(g)
    assert len(pick_cases(g, limit=3)) == 3


def test_run_case_runs_every_config_and_they_agree(db):
    g, _, _ = load_graph(db)
    rows = run_case(g, "pair", ["boto3", "botocore"], timeout_s=10)
    assert [r.config for r in rows] == list(CONFIGS)
    assert all(r.status == "SAT" and r.valid_plan and r.consistent
               for r in rows)
    assert all(r.plan_size == 3 for r in rows)        # boto3, botocore, six
    assert problems(rows) == []


def test_an_unsatisfiable_case_is_reported_unsat_by_all(db):
    g, _, _ = load_graph(db)
    # boto3 needs botocore==v, the root wants botocore>=99: nothing fits
    rows = run_case(g, "impossible", ["boto3", "botocore>=99"], timeout_s=10)
    assert {r.status for r in rows} == {"UNSAT"}
    assert all(r.valid_plan is None for r in rows)
    assert problems(rows) == []


def test_plans_are_recorded_per_config_and_lcv_can_pick_older_ones(tmp_path):
    # the newest release rules out one of lib's versions (LCV cost 1), the
    # oldest has no dependencies (cost 0): newest-first takes app 2, LCV app 1.
    # This is exactly what happens on real data (requests -> 2.15.1).
    path = tmp_path / "lcv.db"
    make_db(path, {"app": {"2": [("lib", ">=2")], "1": []},
                   "lib": {"1": [], "2": []}})
    g, _, _ = load_graph(path)
    rows = {r.config: r for r in run_case(g, "app", ["app"], timeout_s=10)}
    assert "C4-noLCV" in rows
    for name in ("C0", "C1", "C2", "C4-noLCV"):
        assert (rows[name].plan_size, rows[name].non_latest) == (2, 0), name
    for name in ("C3", "C4"):                         # LCV
        assert (rows[name].plan_size, rows[name].non_latest) == (1, 1), name
    assert problems(list(rows.values())) == []        # all valid, all agree
    text = build_report(list(rows.values()), facts=graph_facts(g),
                        load_s=0.1, peak_bytes=1, timeout_s=10, db="x")
    assert "Plan chosen" in text and "2 / 0" in text and "1 / 1" in text


def test_the_real_style_unsat_and_backtracking_cases_behave(db):
    g, _, _ = load_graph(db)
    cases = dict(pick_cases(g))
    conflict = next(v for k, v in cases.items() if k.startswith("conflict:"))
    rows = run_case(g, "conflict", conflict, timeout_s=10)
    assert {r.status for r in rows} == {"UNSAT"} and problems(rows) == []
    back = next(v for k, v in cases.items() if k.startswith("backtrack:"))
    rows = run_case(g, "backtrack", back, timeout_s=10)
    assert {r.status for r in rows} == {"SAT"} and problems(rows) == []


def test_every_run_starts_cold_and_reports_a_warm_time_too(db):
    g, _, _ = load_graph(db)
    rows = run_case(g, "pair", ["boto3", "botocore"], timeout_s=10)
    assert all(r.warm_s >= 0 and r.wall_s >= 0 for r in rows)
    # the caches really are emptied between runs
    solve_once = realbench.solve
    realbench.reset_graph_caches(g)
    assert not g._allowed and not g._candidates and g._reverse is None
    from crux.resolver.problem import Config, Problem
    solve_once(Problem.from_requirements(g, ["boto3"]), Config.named("C1"))
    assert g._allowed                          # AC-3 filled them
    realbench.reset_graph_caches(g)
    assert not g._allowed and g.allowed_misses == 0


def test_a_timed_out_run_is_not_repeated_for_the_warm_time(db, monkeypatch):
    g, _, _ = load_graph(db)
    from crux.resolver.problem import Result, Stats
    calls = []

    def fake(problem, config):
        calls.append(1)
        return Result("TIMEOUT", stats=Stats(wall_s=3.0))

    monkeypatch.setattr(realbench, "solve", fake)
    rows = run_case(g, "pair", ["boto3", "botocore"], configs=("C0",),
                    timeout_s=3)
    assert len(calls) == 1 and rows[0].warm_s == 3.0


def test_disagreement_between_configs_is_flagged(db, monkeypatch):
    g, _, _ = load_graph(db)
    real = realbench.solve
    from crux.resolver.problem import Result

    def fake(problem, config):
        if config.learning:
            return Result("UNSAT", conflict=[])
        return real(problem, config)

    monkeypatch.setattr(realbench, "solve", fake)
    rows = run_case(g, "pair", ["boto3", "botocore"], timeout_s=10)
    assert all(not r.consistent for r in rows)
    assert len(problems(rows)) == len(CONFIGS)


def test_an_invalid_plan_is_flagged(db, monkeypatch):
    g, _, _ = load_graph(db)
    from crux.resolver.problem import Result
    from packaging.version import Version

    monkeypatch.setattr(realbench, "solve",
                        lambda p, c: Result("SAT", plan={
                            "boto3": Version("1")}))      # misses botocore
    rows = run_case(g, "pair", ["boto3", "botocore"], configs=("C0",),
                    timeout_s=10)
    assert rows[0].valid_plan is False and len(problems(rows)) == 1


def test_main_writes_the_report_and_csv(db, tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["--db", str(db), "--timeout", "10", "--out", str(out),
                 "--limit", "6"])
    assert code == 0
    text = (out / "real_report.md").read_text(encoding="utf-8")
    for needle in ("# Resolver on real PyPI data", "## The graph",
                   "## Nodes explored", "## Time, cold (seconds)",
                   "## Time, warm (seconds)", "## Summary per configuration",
                   "## Caveats", "every SAT plan passed the independent "
                   "checker", "30-seed pilot closure", "median cold s",
                   "median warm s"):
        assert needle in text, needle
    assert (out / "real_rows.csv").is_file()
    assert "peak" in capsys.readouterr().out


def test_report_refuses_to_look_clean_when_something_is_wrong(db):
    g, s, p = load_graph(db)
    rows = run_case(g, "pair", ["boto3", "botocore"], timeout_s=10)
    rows[0].valid_plan = False
    text = build_report(rows, facts=graph_facts(g), load_s=s, peak_bytes=p,
                        timeout_s=10, db="x.db")
    assert "PROBLEM ROWS" in text and "every SAT plan passed" not in text


def test_a_missing_database_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "nope.db")])
