"""Brute-force oracle: the ground truth for tiny problems.

Exhaustive generate-and-test.  It shares no code with any solver and never
prunes, so it is slow on purpose and obviously correct.  Use it only on small
inputs; it refuses (``OracleLimitExceeded``) when the search space is too big.

How it works
------------
Take the *static closure*: every package reachable from the active roots
through ANY version's non-disabled dependencies.  Give every closure package a
version (a package with no releases gets ``None``, meaning "unusable").  For
each such full assignment, walk from the active roots through the chosen
releases; the packages reached are the active ones.  The assignment is a
solution when every walked constraint holds, and its plan is the assignment
restricted to the active packages.  Different full assignments can give the
same plan (inactive packages are free), so plans are de-duplicated.

Why that equals the checker's definition (checker.py): a valid plan fixes
versions only for active packages, and which packages are active depends only
on those versions, so every valid plan extends to a full assignment and every
solution restricts to a valid plan.  ``tests/resolver/test_oracle.py``
cross-checks the two on random instances with a second, checker-based
enumeration.

The only piece taken from the checker is ``admissible`` (the per-package
pre-release rule), which is itself tested against ``Graph.candidates``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterator, Literal

from packaging.version import Version

from crux.resolver.checker import admissible
from crux.resolver.problem import Constraint, Problem

DEFAULT_MAX_STATES = 2_000_000


class OracleLimitExceeded(RuntimeError):
    """The search space is larger than ``max_states``."""


@dataclass(frozen=True)
class OracleResult:
    status: Literal["SAT", "UNSAT"]
    plan: dict[str, Version] | None     # first plan found, newest-first order
    states: int                         # full assignments examined


class _Model:
    """Everything precomputed once per problem."""

    def __init__(self, problem: Problem):
        g = problem.graph
        self.disabled = problem.disabled
        self.roots = [(c.dep, c.spec) for c in problem.active_roots()]

        # static closure over every version's non-disabled edges
        self.edges: dict[tuple[str, Version], list[tuple[str, str]]] = {}
        order: list[str] = []
        seen: set[str] = set()
        todo = [dep for dep, _ in self.roots]
        while todo:
            pkg = todo.pop()
            if pkg in seen:
                continue
            seen.add(pkg)
            order.append(pkg)
            for v in g.versions(pkg):
                es = []
                for dep, spec in g.deps(pkg, v):
                    con = Constraint.edge(pkg, v, dep, spec)
                    if con in self.disabled:
                        continue
                    es.append((con.dep, con.spec))
                    todo.append(con.dep)
                self.edges[(pkg, v)] = es
        self.packages = sorted(order)
        self.domains = [list(g.versions(p)) or [None] for p in self.packages]

        # admissible versions per distinct (dep, spec) requirement
        self.ok: dict[tuple[str, str], frozenset] = {}
        reqs = set(self.roots)
        for es in self.edges.values():
            reqs.update(es)
        for dep, spec in reqs:
            rel = g.versions(dep)
            s = Constraint.root(dep, spec).specifier
            self.ok[(dep, spec)] = frozenset(
                v for v in rel if admissible(s, v, rel))

    def size(self) -> int:
        n = 1
        for d in self.domains:
            n *= len(d)
        return n

    def walk(self, assign: dict[str, Version | None]):
        """Return the active package set if every constraint holds, else None."""
        active: set[str] = set()
        stack = list(self.roots)
        while stack:
            dep, spec = stack.pop()
            v = assign[dep]
            if v is None or v not in self.ok[(dep, spec)]:
                return None
            if dep in active:
                continue
            active.add(dep)
            stack.extend(self.edges[(dep, v)])
        return active


def search_space(problem: Problem) -> int:
    """Number of full assignments the oracle would examine."""
    return _Model(problem).size()


def all_plans(problem: Problem, *,
              max_states: int = DEFAULT_MAX_STATES) -> Iterator[dict]:
    """Yield every distinct valid plan (each a ``{pkg: Version}`` dict)."""
    m = _Model(problem)
    if m.size() > max_states:
        raise OracleLimitExceeded(
            f"search space {m.size()} exceeds max_states={max_states}")
    seen: set[tuple] = set()
    for combo in itertools.product(*m.domains):
        assign = dict(zip(m.packages, combo))
        active = m.walk(assign)
        if active is None:
            continue
        plan = {p: assign[p] for p in sorted(active)}
        key = tuple((p, str(v)) for p, v in plan.items())
        if key not in seen:
            seen.add(key)
            yield plan


def brute_force(problem: Problem, *,
                max_states: int = DEFAULT_MAX_STATES) -> OracleResult:
    """SAT with the first plan found, or UNSAT.  Examines states until a
    solution is found, or all of them for UNSAT."""
    m = _Model(problem)
    if m.size() > max_states:
        raise OracleLimitExceeded(
            f"search space {m.size()} exceeds max_states={max_states}")
    states = 0
    for combo in itertools.product(*m.domains):
        states += 1
        assign = dict(zip(m.packages, combo))
        active = m.walk(assign)
        if active is not None:
            plan = {p: assign[p] for p in sorted(active)}
            return OracleResult("SAT", plan, states)
    return OracleResult("UNSAT", None, states)


def count_plans(problem: Problem, **kw) -> int:
    return sum(1 for _ in all_plans(problem, **kw))
