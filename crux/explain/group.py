"""Grouping a minimal core into the statements a message is made of.

A core lists one constraint per release: the proposal's A/B/C conflict is
11 constraints (2 requests, 5 edges from ``a``, 4 from ``b``) and the real
boto3/botocore conflict is 432.  A reader needs one statement per kind of
requirement, not one per release, so this module groups the core:

* one ``Group`` per root dependency: every request the user made on one
  package, together;
* one ``Group`` per (package, dependency) pair of edges, whatever their
  specifiers.  On the boto3 conflict each of the 430 releases has a
  different botocore range, so grouping by identical specifier would
  compress nothing; the ranges are summarised instead (``envelope`` and
  the ``matching`` counts).

The number of groups is the benchmark's "distinct requirement statements"
(``crux.benchmark.real_cores.groups``).  This module decides nothing about
wording; turning groups into English is the renderer's job.

Every field is computed from the core and the graph alone: "every allowed
version of a" means every version the *core's own* constraints let ``a``
take, which is what minimality is proven against.

Context: docs/EXPLAIN.md sections 3 and 9.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import reduce
from typing import Iterable, Literal

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from crux.data.graph import Graph
from crux.resolver.problem import Constraint

Bound = tuple[Version, bool]          # (version, inclusive)


# ── version ranges ──────────────────────────────────────────────────────

def _next_prefix(release: tuple[int, ...]) -> Version:
    """1.4 -> 1.5, (2,) -> 3: the first version past a release prefix."""
    return Version(".".join(map(str, release[:-1] + (release[-1] + 1,))))


def _stricter_lower(a: Bound, b: Bound) -> Bound:
    if a[0] != b[0]:
        return max(a, b, key=lambda x: x[0])
    return (a[0], a[1] and b[1])


def _stricter_upper(a: Bound, b: Bound) -> Bound:
    if a[0] != b[0]:
        return min(a, b, key=lambda x: x[0])
    return (a[0], a[1] and b[1])


def _looser_lower(a: Bound, b: Bound) -> Bound:
    if a[0] != b[0]:
        return min(a, b, key=lambda x: x[0])
    return (a[0], a[1] or b[1])


def _looser_upper(a: Bound, b: Bound) -> Bound:
    if a[0] != b[0]:
        return max(a, b, key=lambda x: x[0])
    return (a[0], a[1] or b[1])


def spec_bounds(spec) -> tuple[Bound | None, Bound | None]:
    """Lowest and highest version *spec* admits, each as (version,
    inclusive); None means unbounded on that side.

    ``~=1.4.5`` is ``>=1.4.5,<1.5`` and ``==1.2.*`` is ``>=1.2,<1.3``.
    ``!=`` is ignored (it removes points, not a range), and so is a
    ``===`` whose operand is not a PEP 440 version.
    """
    lo = hi = None
    for s in SpecifierSet(str(spec)):
        op, v = s.operator, s.version
        if op == "==" and v.endswith(".*"):
            base = Version(v[:-2])
            low, high = (base, True), (_next_prefix(base.release), False)
        elif op == "~=":
            base = Version(v)
            low, high = (base, True), (_next_prefix(base.release[:-1]), False)
        elif op in ("==", "==="):
            try:
                x = Version(v)
            except InvalidVersion:
                continue
            low = high = (x, True)
        elif op == ">=":
            low, high = (Version(v), True), None
        elif op == ">":
            low, high = (Version(v), False), None
        elif op == "<=":
            low, high = None, (Version(v), True)
        elif op == "<":
            low, high = None, (Version(v), False)
        else:                                   # "!="
            continue
        if low is not None:
            lo = low if lo is None else _stricter_lower(lo, low)
        if high is not None:
            hi = high if hi is None else _stricter_upper(hi, high)
    return lo, hi


def _is_empty(lo: Bound | None, hi: Bound | None) -> bool:
    if lo is None or hi is None:
        return False
    if lo[0] != hi[0]:
        return lo[0] > hi[0]
    return not (lo[1] and hi[1])


def format_range(lo: Bound | None, hi: Bound | None) -> str:
    """Bounds back to a specifier string ("" for any version)."""
    if lo and hi and lo[0] == hi[0] and lo[1] and hi[1]:
        return f"=={lo[0]}"
    parts = []
    if lo:
        parts.append((">=" if lo[1] else ">") + str(lo[0]))
    if hi:
        parts.append(("<=" if hi[1] else "<") + str(hi[0]))
    return str(SpecifierSet(",".join(parts)))


def envelope(specs: Iterable[str]) -> str | None:
    """The smallest single range containing every range in *specs*: their
    union, with any gaps between them filled in.  ``>=1.36,<1.37`` and
    ``>=1.43,<1.44`` give ``>=1.36,<1.44``.  None if no spec admits any
    version (every range is empty)."""
    ranges = [spec_bounds(s) for s in specs]
    ranges = [r for r in ranges if not _is_empty(*r)]
    if not ranges:
        return None
    lows = [r[0] for r in ranges]
    highs = [r[1] for r in ranges]
    lo = None if any(x is None for x in lows) else reduce(_looser_lower, lows)
    hi = None if any(x is None for x in highs) else reduce(_looser_upper, highs)
    return format_range(lo, hi)


def intersection(specs: Iterable[str]) -> str | None:
    """The range every spec in *specs* admits at once, ignoring ``!=``.
    None if they cannot all hold (``<2`` and ``>=3``)."""
    lo, hi = spec_bounds(",".join(s for s in specs if s))
    return None if _is_empty(lo, hi) else format_range(lo, hi)


# ── groups ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Group:
    """One statement's worth of a core.

    kind, dep, pkg   ``("root", dep, None)``: the user's requests on *dep*;
                     ``("edge", dep, pkg)``: releases of *pkg* requiring *dep*.
    members          the core's constraints in this group, in core order.
    specs            distinct specifier strings, sorted.
    range            roots: the range all requests admit together (their
                     intersection; None if they contradict each other).
                     Edges: the envelope of the members' ranges (the
                     smallest range containing all of them).
    n_matching       releases of *dep* in the graph that satisfy the group:
                     every request (roots) or at least one member (edges).
                     0 means the group asks for something the snapshot does
                     not have.
    lowest_match, highest_match
                     the oldest and newest of those releases ("" if none).
    dep_known        the graph has at least one release of *dep*.
    missing          only when nothing matches (``unavailable``): releases
                     of *dep* that would match but that the snapshot
                     cannot use, as (version, reason) newest first, from
                     ``Graph.why_missing`` (``no_wheel``, ``yanked``,
                     ``after_as_of``, ``prerelease``, ...).  Empty if the
                     graph knows no such release, or if the user's
                     requests contradict each other (``range`` is None).
    versions         edges: the releases of *pkg* in the group, oldest first.
    n_allowed        edges: releases of *pkg* the core's own constraints let
                     it take (its requests, and at least one core edge into
                     it if there is any).
    covers_all       edges: ``versions`` includes every one of those, so the
                     message may say "every allowed version of pkg".
    uncovered        edges: the allowed releases of *pkg* this group does
                     not constrain, oldest first.  When different releases
                     of one package fail for different reasons (3.3.1
                     through one dependency, 3.4.0 through another) each
                     group leaves some uncovered; ``rules_out_package``
                     says whether the groups together cover them all.
    depth            0 for roots; for edges, how many requirements from the
                     user's request *pkg* is (1 = a package the user asked
                     for).  None if *pkg* is not reachable from a root
                     through the core.
    """

    kind: Literal["root", "edge"]
    dep: str
    pkg: str | None
    members: tuple[Constraint, ...]
    specs: tuple[str, ...]
    range: str | None
    n_matching: int
    lowest_match: str
    highest_match: str
    dep_known: bool
    missing: tuple[tuple[str, str], ...] = ()
    versions: tuple[str, ...] = ()
    n_allowed: int = 0
    covers_all: bool | None = None
    uncovered: tuple[str, ...] = ()
    depth: int | None = 0

    @property
    def common_spec(self) -> str | None:
        """The one specifier every member shares, if there is one."""
        return self.specs[0] if len(self.specs) == 1 else None

    @property
    def unavailable(self) -> bool:
        """No release of ``dep`` satisfies this group."""
        return self.n_matching == 0

    @property
    def missing_reasons(self) -> tuple[str, ...]:
        """Distinct reasons in ``missing``, most frequent first."""
        count: dict[str, int] = {}
        for _, r in self.missing:
            count[r] = count.get(r, 0) + 1
        return tuple(sorted(count, key=lambda r: (-count[r], r)))

    @property
    def first(self) -> str:
        return self.versions[0] if self.versions else ""

    @property
    def last(self) -> str:
        return self.versions[-1] if self.versions else ""


def _depths(core: list[Constraint]) -> dict[str, int]:
    """Distance of each package from the user's requests, through core edges."""
    adj: dict[str, set[str]] = {}
    for c in core:
        if c.kind == "edge":
            adj.setdefault(c.pkg, set()).add(c.dep)
    dist = {c.dep: 1 for c in core if c.kind == "root"}
    todo = deque(sorted(dist))
    while todo:
        p = todo.popleft()
        for q in sorted(adj.get(p, ())):
            if q not in dist:
                dist[q] = dist[p] + 1
                todo.append(q)
    return dist


def _matches(g: Graph, dep: str, specs, every: bool) -> list[Version]:
    sets = [g.allowed(dep, s) for s in specs]
    hit = frozenset.intersection(*sets) if every else frozenset().union(*sets)
    return sorted(hit)


def _missing(g: Graph, dep: str, specs, rng, kind) -> tuple:
    if kind == "root":
        return () if rng is None else g.why_missing(dep, ",".join(specs))
    rows = {row for s in specs for row in g.why_missing(dep, s)}
    return tuple(sorted(rows, key=lambda x: (Version(x[0]), x[1]), reverse=True))


def group_core(graph, core: Iterable[Constraint]) -> tuple[Group, ...]:
    """Group *core* (any order, e.g. ``explain(problem).core``) into
    statements.  *graph* is a ``Graph`` or a ``Problem``.

    Groups are ordered roots first, then edges by depth, then by name, so
    the same core always gives the same groups in the same order.
    """
    g: Graph = getattr(graph, "graph", graph)
    core = sorted(set(core), key=lambda c: (c.kind != "root", str(c)))
    buckets: dict[tuple, list[Constraint]] = {}
    for c in core:
        key = ("root", None, c.dep) if c.kind == "root" else ("edge", c.pkg, c.dep)
        buckets.setdefault(key, []).append(c)

    roots_on: dict[str, list[str]] = {}
    edges_into: dict[str, list[str]] = {}
    for c in core:
        (roots_on if c.kind == "root" else edges_into).setdefault(
            c.dep, []).append(c.spec)
    depth = _depths(core)

    out = []
    for (kind, pkg, dep), members in buckets.items():
        specs = tuple(sorted({c.spec for c in members}))
        hits = _matches(g, dep, specs, every=(kind == "root"))
        common = dict(
            kind=kind, dep=dep, pkg=pkg, members=tuple(members), specs=specs,
            n_matching=len(hits),
            lowest_match=str(hits[0]) if hits else "",
            highest_match=str(hits[-1]) if hits else "",
            dep_known=bool(g.versions(dep)))
        rng = intersection(specs) if kind == "root" else envelope(specs)
        if not hits:
            common["missing"] = _missing(g, dep, specs, rng, kind)
        if kind == "root":
            out.append(Group(range=rng, depth=0, **common))
            continue
        versions = sorted({Version(c.version) for c in members})
        allowed = set(g.allowed(pkg, ",".join(roots_on.get(pkg, [])) or ""))
        if pkg in edges_into:
            allowed &= set(_matches(g, pkg, edges_into[pkg], every=False))
        out.append(Group(
            range=rng,
            versions=tuple(str(v) for v in versions),
            n_allowed=len(allowed),
            covers_all=bool(allowed) and allowed <= set(versions),
            uncovered=tuple(str(v) for v in sorted(allowed - set(versions))),
            depth=depth.get(pkg),
            **common))

    inf = float("inf")
    out.sort(key=lambda x: (x.kind != "root",
                            inf if x.depth is None else x.depth,
                            x.pkg or "", x.dep))
    return tuple(out)


def rules_out_package(groups: Iterable[Group], pkg: str) -> bool:
    """True if the edge groups of *pkg* together constrain every release
    the core lets it take, so the message may say "every allowed version
    of pkg" even when no single group does.  False if *pkg* has no edge
    groups."""
    mine = [x for x in groups if x.kind == "edge" and x.pkg == pkg]
    if not mine or not any(x.n_allowed for x in mine):
        return False
    return not set.intersection(*(set(x.uncovered) for x in mine))
