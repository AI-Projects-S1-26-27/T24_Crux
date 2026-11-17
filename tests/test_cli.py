"""The command-line interface (crux/cli.py).

Every graph source (JSON file, SQLite snapshot, benchmark instance), every
exit code, and the rule that only the plan or the explanation goes to stdout.
All offline: fixture graphs, a temp-file database, committed instances.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from crux import cli
from crux.benchmark.instance import load_jsonl
from crux.data.graph import Graph
from crux.explain import message
from crux.resolver import Problem, Result

ROOT = Path(__file__).parent.parent
TOY = str(ROOT / "tests" / "fixtures" / "toy_abc.json")
PLANTED = str(ROOT / "benchmarks" / "planted" / "instances.jsonl")


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ── success: the plan ────────────────────────────────────────────────

def test_sat_prints_newest_plan_sorted(capsys):
    code, out, err = run(capsys, "resolve", "a", "--graph", TOY)
    assert code == cli.OK
    assert out == "a==1.4\nc==1.2\n"
    assert err == ""


def test_plan_lists_only_needed_packages(capsys):
    code, out, _ = run(capsys, "resolve", "c>=3", "--graph", TOY)
    assert code == cli.OK
    assert out == "c==3.2\n"


@pytest.mark.parametrize("name", list(cli.CONFIGS))
def test_every_config_gives_a_valid_answer(capsys, name):
    code, out, _ = run(capsys, "resolve", "a", "c<1.2", "--graph", TOY,
                       "--config", name)
    assert code == cli.OK
    lines = out.splitlines()
    assert {line.split("==")[0] for line in lines} == {"a", "c"}
    assert "c==1.2" not in lines


# ── failure: the explanation ─────────────────────────────────────────

def test_unsat_prints_the_benchmarked_message(capsys):
    code, out, err = run(capsys, "resolve", "a", "b", "--graph", TOY)
    assert code == cli.NO_SOLUTION
    g = Graph.from_dict(json.loads(Path(TOY).read_text()))
    expected = message(Problem.from_requirements(g, ["a", "b"]), width=80)
    assert out == expected + "\n"
    assert out.startswith("No solution.")
    assert err == ""


def test_width_zero_does_not_wrap(capsys):
    _, wrapped, _ = run(capsys, "resolve", "a", "b", "--graph", TOY)
    _, flat, _ = run(capsys, "resolve", "a", "b", "--graph", TOY,
                     "--width", "0")
    assert len(flat.splitlines()) < len(wrapped.splitlines())
    assert " ".join(flat.split()) == " ".join(wrapped.split())


def test_unknown_package_is_explained_not_crashed(capsys):
    code, out, _ = run(capsys, "resolve", "nosuchpkg", "--graph", TOY)
    assert code == cli.NO_SOLUTION
    assert "nosuchpkg" in out


def test_timeout_exit_code(capsys, monkeypatch):
    monkeypatch.setattr(cli, "solve",
                        lambda problem, config: Result("TIMEOUT"))
    code, out, err = run(capsys, "resolve", "a", "--graph", TOY,
                         "--timeout", "5")
    assert code == cli.TIMEOUT
    assert out == ""
    assert "5 s" in err


# ── stats go to stderr ───────────────────────────────────────────────

def test_stats_go_to_stderr_only(capsys):
    code, out, err = run(capsys, "resolve", "a", "--graph", TOY, "--stats")
    assert code == cli.OK
    assert out == "a==1.4\nc==1.2\n"
    assert err.startswith("[C4-noLCV] SAT: ")
    assert "nodes" in err


# ── bad input: one line on stderr, exit 2 ────────────────────────────

@pytest.mark.parametrize("req", ["a[extra]", "a; python_version<'3'",
                                 "a @ https://example.com/a.whl", "a>>1"])
def test_out_of_scope_or_malformed_requirement(capsys, req):
    code, out, err = run(capsys, "resolve", req, "--graph", TOY)
    assert code == cli.BAD_INPUT
    assert out == ""
    assert err.startswith("crux: error:")


def test_missing_database_has_a_hint(capsys, tmp_path):
    code, _, err = run(capsys, "resolve", "a",
                       "--db", str(tmp_path / "none.db"))
    assert code == cli.BAD_INPUT
    assert "run_ingest" in err


def test_uncleaned_database(capsys, tmp_path):
    db = tmp_path / "raw.db"
    sqlite3.connect(db).close()                  # exists, but no tables
    code, _, err = run(capsys, "resolve", "a", "--db", str(db))
    assert code == cli.BAD_INPUT
    assert "clean" in err


def test_missing_graph_file(capsys, tmp_path):
    code, _, err = run(capsys, "resolve", "a",
                       "--graph", str(tmp_path / "none.json"))
    assert code == cli.BAD_INPUT


def test_no_requirements(capsys):
    code, _, err = run(capsys, "resolve", "--graph", TOY)
    assert code == cli.BAD_INPUT
    assert "requirement" in err


def test_id_without_instance_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["resolve", "a", "--id", "x"])
    assert exc.value.code == 2


def test_two_sources_are_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["resolve", "a", "--graph", TOY, "--db", "x.db"])
    assert exc.value.code == 2


# ── the snapshot database ────────────────────────────────────────────

def test_database_source(capsys, tmp_path):
    db = tmp_path / "crux.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE releases (name, version, upload_time)")
    conn.execute("CREATE TABLE deps (name, version, dep_name, specifier)")
    conn.executemany("INSERT INTO releases VALUES (?, ?, NULL)",
                     [("app", "1.0"), ("lib", "1.0"), ("lib", "2.0")])
    conn.execute("INSERT INTO deps VALUES ('app', '1.0', 'lib', '<2')")
    conn.commit()
    conn.close()
    code, out, _ = run(capsys, "resolve", "app", "--db", str(db))
    assert code == cli.OK
    assert out == "app==1.0\nlib==1.0\n"


# ── benchmark instances ──────────────────────────────────────────────

def test_instance_without_id_lists_ids(capsys):
    code, out, _ = run(capsys, "resolve", "--instance", PLANTED)
    assert code == cli.OK
    lines = out.splitlines()
    assert len(lines) == len(load_jsonl(PLANTED))
    assert lines[0] == "planted/direct/d1/none/s0\tUNSAT"


def test_instance_uses_its_own_request(capsys):
    inst = load_jsonl(PLANTED)[0]
    code, out, _ = run(capsys, "resolve", "--instance", PLANTED,
                       "--id", inst.id)
    assert code == cli.NO_SOLUTION
    assert out == message(inst.problem(), width=80) + "\n"


def test_requirements_override_the_instance_request(capsys):
    inst = load_jsonl(PLANTED)[0]                 # glyph>=1.1.0 + tamarack
    code, out, _ = run(capsys, "resolve", "glyph", "--instance", PLANTED,
                       "--id", inst.id)
    assert code == cli.OK
    assert out.startswith("beacon==")


def test_unknown_instance_id(capsys):
    code, _, err = run(capsys, "resolve", "--instance", PLANTED,
                       "--id", "no/such/id")
    assert code == cli.BAD_INPUT
    assert "without --id" in err


# ── entry points ─────────────────────────────────────────────────────

def test_python_dash_m_crux():
    proc = subprocess.run(
        [sys.executable, "-m", "crux", "resolve", "a", "b", "--graph", TOY],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == cli.NO_SOLUTION
    assert proc.stdout.startswith("No solution.")


def test_arrow_survives_a_legacy_code_page():
    # Windows writes redirected output in cp1252, which has no "→"; deep
    # conflicts use it ("through a → b"). PYTHONIOENCODING forces the same
    # situation on any platform.
    deep = "planted/deep/d2/none/s1"
    proc = subprocess.run(
        [sys.executable, "-m", "crux", "resolve", "--instance", PLANTED,
         "--id", deep],
        capture_output=True, cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert proc.returncode == cli.NO_SOLUTION, proc.stderr.decode("utf-8")
    assert "→" in proc.stdout.decode("utf-8")


def test_help_lists_resolve(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "resolve" in capsys.readouterr().out
