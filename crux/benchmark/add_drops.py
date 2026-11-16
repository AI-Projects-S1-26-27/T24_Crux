"""Copy the data layer's drop reasons into the real-failure instances.

A real instance (``benchmarks/corpus/instances.jsonl.gz``) is a small graph
cut out of the corpus database as of the issue's report date, so it keeps
only usable releases.  When a requirement in it has no matching release,
the reason (no wheel for the target, yanked, uploaded after the report, ...)
stayed behind in the database.  This step puts it back, as
``meta["drops"] = {package: [[version, reason], ...]}``, which
``Instance.problem`` hands to ``Graph.from_dict`` and so to
``Graph.why_missing`` and the renderer.

Only what a message can need is copied: for every requirement in the
instance (a request or a dependency edge) that no release in the instance
satisfies, the database's versions of that package that match it, each with
its ``drops`` reason (stages ``ingest`` and ``clean``) or ``after_as_of`` if
the release exists but was uploaded after the report.  Everything else in the
instance is left byte-for-byte as it was.

    python -m crux.benchmark.add_drops --db data/corpus.db \\
        --instances benchmarks/corpus/instances.jsonl.gz

The corpus database is not committed (1.3 GB); it is the one
``corpus_ingest`` builds.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from crux.benchmark.instance import Instance, dump_jsonl, load_jsonl
from crux.data.graph import AFTER_AS_OF, Graph

RELEASE_STAGES = ("ingest", "clean")


def unmet(inst: Instance) -> set[tuple[str, str]]:
    """(package, specifier) of every requirement no release in the instance
    meets: the requests and the dependency edges."""
    g = Graph.from_dict(inst.graph)
    reqs = {(canonicalize_name(d), str(SpecifierSet(s))) for d, s in inst.roots}
    for vs in inst.graph.values():
        for deps in vs.values():
            reqs.update((canonicalize_name(d), str(SpecifierSet(s))) for d, s in deps)
    return {(d, s) for d, s in reqs if not g.allowed(d, s)}


def _matches(spec: SpecifierSet, version: str) -> bool:
    try:
        return spec.contains(Version(version), prereleases=True)
    except InvalidVersion:
        return False


def drops_for(inst: Instance, con: sqlite3.Connection) -> dict:
    """The ``meta["drops"]`` entry for one instance ({} if nothing is unmet)."""
    cutoff = inst.meta.get("created_at")
    cut = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) if cutoff else None
    out: dict[str, set] = {}
    marks = ",".join("?" * len(RELEASE_STAGES))
    for dep, spec_text in sorted(unmet(inst)):
        spec = SpecifierSet(spec_text)
        for version, reason in con.execute(
                f"SELECT version, reason FROM drops WHERE stage IN ({marks})"
                " AND name = ?", (*RELEASE_STAGES, dep)):
            if version is not None and _matches(spec, version):
                out.setdefault(dep, set()).add((version, reason))
        if cut is None:
            continue
        for version, uploaded in con.execute(
                "SELECT version, upload_time FROM releases WHERE name = ?", (dep,)):
            if (uploaded and _matches(spec, version)
                    and datetime.fromisoformat(uploaded.replace("Z", "+00:00")) > cut):
                out.setdefault(dep, set()).add((version, AFTER_AS_OF))
    return {p: [list(r) for r in sorted(rows, key=lambda x: (Version(x[0]), x[1]))]
            for p, rows in sorted(out.items())}


def add_drops(instances: list[Instance], db: str) -> tuple[list[Instance], int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out, n = [], 0
        for inst in instances:
            d = drops_for(inst, con)
            meta = {k: v for k, v in inst.meta.items() if k != "drops"}
            if d:
                meta["drops"] = d
                n += 1
            out.append(replace(inst, meta=meta))
        return out, n
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--instances", required=True)
    ap.add_argument("--out", help="default: rewrite --instances in place")
    a = ap.parse_args(argv)
    if not Path(a.db).exists():
        print(f"no database at {a.db}", file=sys.stderr)
        return 1
    insts, n = add_drops(load_jsonl(a.instances), a.db)
    dump_jsonl(insts, a.out or a.instances)
    print(f"{n} of {len(insts)} instances got drop reasons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
