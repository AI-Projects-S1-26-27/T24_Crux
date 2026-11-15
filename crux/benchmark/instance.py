"""Benchmark instances: one resolution problem plus what is known about it.

The planted generator, the real-failure corpus and the pip / uv / Crux harness
all exchange instances as plain JSON, one object per line (JSONL):

    {
      "id":       "planted/deep/d3/small/s0",
      "graph":    {pkg: {version: [[dep, spec], ...]}},   # Graph.from_dict format
      "roots":    [[dep, spec], ...],                     # the user's request
      "expected": "UNSAT" | "SAT" | null,                 # null when unknown
      "core":     [constraint, ...] | null,               # planted ground truth
      "meta":     {...}                                   # free-form provenance
    }

A constraint is the field set of ``crux.resolver.problem.Constraint``:

    {"kind": "root", "dep": "a", "spec": ">=1.0"}
    {"kind": "edge", "pkg": "a", "version": "1.0", "dep": "c", "spec": "<2.0"}

``graph`` is exactly what ``Graph.from_dict`` reads, so no new loader exists,
and a core is a set of ``Constraint`` values, so comparing a computed core with
the ground truth is plain set arithmetic.
"""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from crux.data.graph import Graph
from crux.resolver.problem import Constraint, Problem


def constraint_to_json(c: Constraint) -> dict:
    if c.kind == "root":
        return {"kind": "root", "dep": c.dep, "spec": c.spec}
    return {"kind": "edge", "pkg": c.pkg, "version": c.version,
            "dep": c.dep, "spec": c.spec}


def constraint_from_json(d: dict) -> Constraint:
    if d["kind"] == "root":
        return Constraint.root(d["dep"], d["spec"])
    if d["kind"] == "edge":
        return Constraint.edge(d["pkg"], d["version"], d["dep"], d["spec"])
    raise ValueError(f"unknown constraint kind {d['kind']!r}")


def _sort_key(c: Constraint):
    return (c.kind != "root", c.pkg or "", c.version or "", c.dep, c.spec)


@dataclass(frozen=True)
class Instance:
    id: str
    graph: dict                      # {pkg: {version: [[dep, spec], ...]}}
    roots: tuple                     # ((dep, spec), ...)
    expected: str | None = None      # "SAT", "UNSAT" or None
    core: frozenset | None = None    # frozenset[Constraint] or None
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.expected not in (None, "SAT", "UNSAT"):
            raise ValueError(f"bad expected {self.expected!r}")
        if self.core is not None and self.expected != "UNSAT":
            raise ValueError("a core only makes sense for an UNSAT instance")

    # ── views ────────────────────────────────────────────────────────
    def root_constraints(self) -> tuple[Constraint, ...]:
        return tuple(Constraint.root(d, s) for d, s in self.roots)

    def problem(self, graph: Graph | None = None) -> Problem:
        """The resolver's view.  Pass a prebuilt ``graph`` to reuse caches."""
        g = graph if graph is not None else Graph.from_dict(self.graph)
        return Problem(g, self.root_constraints())

    def requirements(self) -> list[str]:
        """Root requirements as PEP 508 strings, for pip and uv."""
        return [f"{c.dep}{c.spec}" for c in self.root_constraints()]

    # ── JSON ─────────────────────────────────────────────────────────
    def to_json(self) -> dict:
        return {
            "id": self.id,
            "graph": self.graph,
            "roots": [list(r) for r in self.roots],
            "expected": self.expected,
            "core": (None if self.core is None else
                     [constraint_to_json(c)
                      for c in sorted(self.core, key=_sort_key)]),
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Instance":
        core = d.get("core")
        return cls(
            id=d["id"],
            graph=d["graph"],
            roots=tuple(tuple(r) for r in d["roots"]),
            expected=d.get("expected"),
            core=(None if core is None else
                  frozenset(constraint_from_json(c) for c in core)),
            meta=d.get("meta", {}),
        )


class _Closing:
    """Text wrapper that also closes the underlying raw file."""

    def __init__(self, text, raw):
        self.text, self.raw = text, raw

    def __enter__(self):
        return self.text

    def __exit__(self, *exc):
        self.text.close()          # flushes and closes the GzipFile
        self.raw.close()


def _open(path: Path, mode: str):
    """Plain or gzip-compressed (``.gz``) text file; gzip output is
    byte-for-byte reproducible (no timestamp or file name in the header)."""
    if path.suffix == ".gz":
        if "w" in mode:
            gz = gzip.GzipFile(filename="", fileobj=open(path, "wb"), mode="wb",
                               mtime=0)
            return _Closing(io.TextIOWrapper(gz, encoding="utf-8"), gz.fileobj)
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def dump_jsonl(instances: Iterable[Instance], path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with _open(path, "w") as f:
        for inst in instances:
            f.write(json.dumps(inst.to_json(), sort_keys=True) + "\n")
            n += 1
    return n


def load_jsonl(path) -> list[Instance]:
    with _open(Path(path), "r") as f:
        return [Instance.from_json(json.loads(line)) for line in f if line.strip()]
