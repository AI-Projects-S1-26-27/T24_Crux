"""Copying drop reasons into real instances (crux/benchmark/add_drops.py)."""

from crux.benchmark.add_drops import add_drops, drops_for, unmet
from crux.benchmark.instance import Instance
from crux.data import store
from crux.data.clean import _ensure_tables
from crux.explain import explain
from crux.explain.render import render_core

INST = Instance(
    id="real/t#1", expected="UNSAT", roots=(("app", ""),),
    graph={"app": {"1.0": [["sqlalchemy", "==1.4.29"]]},
           "sqlalchemy": {"2.0.0": []}, "ok": {"1.0": []}},
    meta={"created_at": "2024-01-01T00:00:00Z", "url": "u"})


def _db(tmp_path):
    db = tmp_path / "corpus.db"
    conn = store.open_db(str(db))
    _ensure_tables(conn)
    rel = "INSERT INTO releases VALUES (?, ?, ?, 0, NULL, 'f', 'u', 's')"
    conn.execute(rel, ("sqlalchemy", "2.0.0", "2023-01-01T00:00:00Z"))
    conn.execute(rel, ("sqlalchemy", "1.4.29", "2025-01-01T00:00:00Z"))   # after report
    drop = "INSERT INTO drops VALUES (?, ?, ?, ?, ?)"
    conn.execute(drop, ("ingest", "no_wheel", "sqlalchemy", "1.4.29", ""))
    conn.execute(drop, ("ingest", "yanked", "sqlalchemy", "1.3.0", ""))     # does not match
    conn.execute(drop, ("edge", "extra_dep", "sqlalchemy", "1.4.29", "x"))  # not a release
    conn.execute(drop, ("ingest", "no_wheel", "ok", "0.9", ""))             # not unmet
    conn.commit()
    conn.close()
    return db


def test_only_unmet_requirements_are_looked_up():
    assert unmet(INST) == {("sqlalchemy", "==1.4.29")}


def test_drops_for_matching_versions_only(tmp_path):
    import sqlite3
    con = sqlite3.connect(str(_db(tmp_path)))
    assert drops_for(INST, con) == {
        "sqlalchemy": [["1.4.29", "after_as_of"], ["1.4.29", "no_wheel"]]}


def test_add_drops_keeps_everything_else(tmp_path):
    (out,), n = add_drops([INST], str(_db(tmp_path)))
    assert n == 1
    assert out.graph == INST.graph and out.roots == INST.roots and out.id == INST.id
    assert {k: v for k, v in out.meta.items() if k != "drops"} == INST.meta
    assert add_drops([out], str(_db(tmp_path)))[0][0] == out      # idempotent


def test_the_message_now_says_why(tmp_path):
    (out,), _ = add_drops([INST], str(_db(tmp_path)))
    p = out.problem()
    text = render_core(p, explain(p).core).text
    # one version with two reasons is one release, given its drop reason
    assert text.endswith("but sqlalchemy 1.4.29 has no wheel for CPython 3.12 "
                         "on Linux x86-64.")
    before = INST.problem()
    assert "no release of sqlalchemy matches" in render_core(before, explain(before).core).text


def test_satisfiable_instance_gets_nothing(tmp_path):
    ok = Instance(id="real/t#2", expected=None, roots=(("ok", ""),),
                  graph={"ok": {"1.0": []}}, meta={"created_at": "2024-01-01T00:00:00Z"})
    (out,), n = add_drops([ok], str(_db(tmp_path)))
    assert n == 0 and "drops" not in out.meta
