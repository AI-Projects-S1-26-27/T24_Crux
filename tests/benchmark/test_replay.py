"""Replay outcomes on a tiny hand-built corpus database (offline)."""

import sqlite3

import pytest

from crux.benchmark.replay import Snapshot, replay_case

RELEASES = [  # name, version, upload_time
    ("a", "1.0", "2024-01-01T00:00:00Z"), ("a", "1.1", "2024-02-01T00:00:00Z"),
    ("b", "1.0", "2024-01-01T00:00:00Z"),
    ("c", "1.0", "2024-01-01T00:00:00Z"), ("c", "3.2", "2024-01-01T00:00:00Z"),
    ("new", "1.0", "2025-06-01T00:00:00Z"),
    ("needs-ghost", "1.0", "2024-01-01T00:00:00Z"),
    ("free", "1.0", "2024-01-01T00:00:00Z"),
]
DEPS = [("a", "1.0", "c", "<2.0"), ("a", "1.1", "c", "<2.0"),
        ("b", "1.0", "c", ">=3.1"), ("needs-ghost", "1.0", "ghost", ">=1")]
PROJECTS = [("a", "done"), ("b", "done"), ("c", "done"), ("new", "done"),
            ("needs-ghost", "done"), ("ghost", "done"), ("free", "done"),
            ("nope", "not_found")]


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    db = tmp_path_factory.mktemp("c") / "corpus.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE projects (name TEXT, status TEXT);
        CREATE TABLE releases (name TEXT, version TEXT, upload_time TEXT);
        CREATE TABLE deps (name TEXT, version TEXT, dep_name TEXT, specifier TEXT);""")
    conn.executemany("INSERT INTO projects VALUES (?, ?)", PROJECTS)
    conn.executemany("INSERT INTO releases VALUES (?, ?, ?)", RELEASES)
    conn.executemany("INSERT INTO deps VALUES (?, ?, ?, ?)", DEPS)
    conn.commit()
    conn.close()
    return Snapshot(db)


def case(reqs, created="2024-06-01T00:00:00Z", conflict=("a", "c")):
    return {"url": "https://github.com/o/r/issues/7", "repo": "o/r",
            "created_at": created, "requirements": reqs, "method": "A",
            "snippet": "s", "original_conflict": list(conflict),
            "reported_python": []}


def test_reproduced_conflict(snap):
    row, inst = replay_case(snap, case(["a", "b"]))
    assert row["outcome"] == "reproduced" and row["same_packages"] is True
    assert inst.expected == "UNSAT" and set(inst.graph) == {"a", "b", "c"}


def test_not_reproduced(snap):
    row, inst = replay_case(snap, case(["a"]))
    assert row["outcome"] == "not_reproduced" and inst is None


def test_snapshot_is_cut_at_the_report_date(snap):
    # a 1.1 was uploaded 2024-02-01: a report from January cannot see it
    row, _ = replay_case(snap, case(["a==1.1"], created="2024-01-15T00:00:00Z"))
    assert row["outcome"] == "root_unavailable"
    row, _ = replay_case(snap, case(["new"], created="2024-06-01T00:00:00Z"))
    assert row["outcome"] == "root_unavailable"


def test_package_not_on_pypi(snap):
    assert replay_case(snap, case(["nope"]))[0]["outcome"] == "not_on_pypi"


def test_failure_only_from_a_release_less_dependency_is_an_artefact(snap):
    row, inst = replay_case(snap, case(["needs-ghost"]))
    assert row["outcome"] == "missing_dependency" and inst is None
