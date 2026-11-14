"""C1: arc consistency (AC-3) as a preprocessing step.

Constraint: "release p=v requires q in S".  Version v of p has *support* when
q still has at least one version in S.  A version with no support can never
be part of a plan, so it is deleted; the deletion may remove the support of
other versions, which are rechecked.  Repeat to a fixed point.  No solution is
ever lost.

Scope: the static closure, i.e. every package reachable from the active roots
through ANY release's enabled edges.  Root requirements act first as unary
constraints (a package a root asks for is limited to the versions the root
allows).  Those unary removals are not counted as AC-3 work: plain search
already honours them, so counting them would flatter the technique.

A package that is not active in some plan can still have its versions pruned:
"v cannot be chosen" is a fact about v, whether or not p is ever needed.

Soundness bookkeeping: every deletion records the one constraint (edge) that
justified it, in ``used``.  The UNSAT conflict reported by the solver must
include them, because a version excluded here is never tried by the search and
the conflict has to explain why.  Replaying the derivation with only the roots
and ``used`` enabled deletes the same versions.

This is arc consistency on the *packages*, applied once before search.  It is
not maintained during search (MAC), which would be an extension.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from packaging.version import Version

from crux.resolver.problem import Constraint, Problem


class ACTimeout(Exception):
    """The deadline passed during propagation."""


@dataclass
class ACResult:
    domains: dict[str, set[Version]]       # surviving versions per package
    pruned: int                            # removed by arc propagation
    versions: int                          # domain total before propagation
    wiped: bool                            # a root package has no version left
    used: set[Constraint] = field(default_factory=set)


def ac3(problem: Problem, deadline: float | None = None) -> ACResult:
    g = problem.graph
    disabled = problem.disabled
    roots = problem.active_roots()

    # ── static closure with, per release, its enabled edges ──────────
    edges: dict[tuple[str, Version], list[tuple[Constraint, frozenset]]] = {}
    rev: dict[str, list[tuple[str, Version]]] = {}
    closure: list[str] = []
    seen: set[str] = set()
    todo = [r.dep for r in roots]
    while todo:
        pkg = todo.pop()
        if pkg in seen:
            continue
        seen.add(pkg)
        closure.append(pkg)
        for v in g.versions(pkg):
            # Check the deadline per release while building the closure: this
            # loop is where the cold Graph.allowed() calls happen.  Checking
            # only in the propagation loop below let a cold boto3 run overrun
            # a 10 s timeout to 18.5 s on the real pilot graph.
            if deadline is not None and time.perf_counter() >= deadline:
                raise ACTimeout
            es = []
            for dep, spec in g.deps(pkg, v):
                con = Constraint.edge(pkg, v, dep, spec)
                if con in disabled:
                    continue
                es.append((con, g.allowed(dep, spec)))
                rev.setdefault(con.dep, []).append((pkg, v))
                todo.append(con.dep)
            edges[(pkg, v)] = es

    # ── unary: root requirements limit their package ─────────────────
    domains = {p: set(g.versions(p)) for p in closure}
    for r in roots:
        domains[r.dep] &= g.allowed(r.dep, r.specifier)
    total = sum(len(d) for d in domains.values())

    # ── propagate to a fixed point ───────────────────────────────────
    pruned = 0
    used: set[Constraint] = set()
    queue = deque(sorted(closure))
    queued = set(queue)
    while queue:
        if deadline is not None and time.perf_counter() >= deadline:
            raise ACTimeout
        q = queue.popleft()
        queued.discard(q)
        dq = domains[q]
        for p, v in rev.get(q, ()):
            if deadline is not None and time.perf_counter() >= deadline:
                raise ACTimeout
            dp = domains[p]
            if v not in dp:
                continue
            for con, ok in edges[(p, v)]:
                if con.dep == q and dq.isdisjoint(ok):
                    dp.discard(v)
                    pruned += 1
                    used.add(con)
                    if p not in queued:
                        queue.append(p)
                        queued.add(p)
                    break

    wiped = any(not domains[r.dep] for r in roots)
    return ACResult(domains, pruned, total, wiped, used)
