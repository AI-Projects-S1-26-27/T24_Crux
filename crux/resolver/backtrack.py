"""C0: plain depth-first backtracking over a dynamic CSP.

State: a partial assignment of versions to *active* packages.  A package
becomes active when a root requirement or an assigned version requires it.
Search repeatedly picks an unassigned active package, tries each version that
satisfies every requirement currently placed on it, and recurses; a package
whose domain is empty is a dead end and the last choice is undone.

This is deliberately the baseline.  It has no arc consistency, no ordering
heuristic and no learning.  The four hooks below are where C1 to C4 plug in:

    select_variable()   C2 (MRV) replaces first-activated order
    order_values()      C3 (LCV) replaces newest-first order
    domain()            C1 (AC-3) prunes domains once, before search
    (conflict handling) C4 (learning) replaces the trivial UNSAT conflict

Iterative on purpose: real dependency chains can be deeper than Python's
recursion limit.

``Stats.nodes`` counts assignments tried; ``Stats.backtracks`` counts
assignments undone.

UNSAT conflict (no learning, so deliberately coarse but sound): the active
roots plus every dependency edge of every version that was tried.  Replaying
this search with every other constraint disabled fails identically, because a
version that was never tried was excluded by constraints that are in the set.
"""

from __future__ import annotations

import time

from packaging.version import Version

from crux.resolver.ac3 import ACTimeout, ac3
from crux.resolver.ordering import lcv_order, mrv_select
from crux.resolver.problem import Config, Constraint, Problem, Result, Stats


class _Frame:
    __slots__ = ("var", "values", "mark")

    def __init__(self, var, values):
        self.var = var
        self.values = iter(values)
        self.mark = None            # trail position before the current choice


class Search:
    def __init__(self, problem: Problem, config: Config):
        self.problem = problem
        self.config = config
        self.graph = problem.graph
        self.stats = Stats()
        self.assign: dict[str, Version] = {}
        self.reqs: dict[str, list[str]] = {}    # pkg -> spec strings on it
        self.req_info: dict[str, list] = {}     # pkg -> the Constraint behind
                                                # each of those specs, in step
        self.last_fail = None                   # constraint that made the last
                                                # immediate assignment fail
        self.order: list[str] = []              # active packages, by activation
        self.active: set[str] = set()
        self.trail: list[tuple] = []
        self.touched: set[Constraint] = set()   # edges of every tried version
        self._edges: dict[tuple[str, Version], list[Constraint]] = {}
        self.ac = None                          # ACResult when config.ac3

    # ── hooks (the places later configurations replace) ──────────────

    def select_variable(self):
        """C0: first active package, in activation order, with no version yet.
        C2 (mrv): the one with the fewest admissible versions."""
        if self.config.mrv:
            return mrv_select(self)
        for pkg in self.order:
            if pkg not in self.assign:
                return pkg
        return None

    def order_values(self, pkg, versions):
        """C0: newest first (the order Graph.candidates already uses).
        C3 (lcv): fewest options ruled out for the dependencies first."""
        if self.config.lcv:
            return lcv_order(self, pkg, versions)
        return versions

    def domain(self, pkg):
        """Versions of *pkg* admissible under every requirement on it."""
        specs = self.reqs[pkg]
        first, rest = specs[0], specs[1:]
        g = self.graph
        out = g.candidates(pkg, first)
        for s in rest:
            ok = g.allowed(pkg, s)
            out = tuple(v for v in out if v in ok)
        if self.ac is not None:                       # C1: AC-3 survivors
            alive = self.ac.domains[pkg]
            out = tuple(v for v in out if v in alive)
        return out

    # ── assignment with an undo trail ────────────────────────────────

    def _edges_of(self, pkg, version):
        key = (pkg, version)
        es = self._edges.get(key)
        if es is None:
            es = []
            for dep, spec in self.graph.deps(pkg, version):
                con = Constraint.edge(pkg, version, dep, spec)
                if con not in self.problem.disabled:
                    es.append(con)
            self._edges[key] = es
        return es

    def _activate(self, dep, spec, con=None):
        """Place requirement *spec* (from constraint *con*) on package *dep*."""
        self.reqs.setdefault(dep, []).append(spec)
        self.req_info.setdefault(dep, []).append(con)
        self.trail.append(("req", dep))
        if dep not in self.active:
            self.active.add(dep)
            self.order.append(dep)
            self.trail.append(("active", dep))

    def _assign(self, pkg, version):
        """Try pkg = version.  Returns the undo mark, or None on conflict
        (in which case everything is already undone)."""
        mark = len(self.trail)
        edges = self._edges_of(pkg, version)
        self.touched.update(edges)
        self.assign[pkg] = version
        self.trail.append(("assign", pkg))
        for con in edges:
            dep = con.dep
            chosen = self.assign.get(dep)
            if chosen is not None and \
                    chosen not in self.graph.allowed(dep, con.spec):
                self.last_fail = con
                self._undo(mark)
                return None
            self._activate(dep, con.spec, con)
        return mark

    def _undo(self, mark):
        while len(self.trail) > mark:
            kind, name = self.trail.pop()
            if kind == "assign":
                del self.assign[name]
            elif kind == "req":
                self.reqs[name].pop()
                self.req_info[name].pop()
            else:
                self.active.remove(name)
                self.order.pop()

    # ── the search ───────────────────────────────────────────────────

    def _setup(self, deadline):
        """AC-3 (if on) and root activation, shared by every search loop.
        Returns a finished Result (TIMEOUT, or UNSAT proved by AC-3), or None
        when the search should go ahead."""
        if self.config.ac3:
            try:
                self.ac = ac3(self.problem, deadline)
            except ACTimeout:
                return Result("TIMEOUT", stats=self.stats)
            self.stats.ac3_pruned = self.ac.pruned
            self.stats.ac3_versions = self.ac.versions
            if self.ac.wiped:                 # proved UNSAT before any search
                return Result("UNSAT", conflict=self._conflict(),
                              stats=self.stats)
        for root in self.problem.active_roots():
            self._activate(root.dep, root.spec, root)
        return None

    def run(self, t0: float) -> Result:
        deadline = t0 + self.config.timeout_s
        early = self._setup(deadline)
        if early is not None:
            return early

        frames: list[_Frame] = []
        while True:
            var = self.select_variable()
            if var is None:
                plan = {p: self.assign[p] for p in self.order}
                return Result("SAT", plan=plan, stats=self.stats)
            values = self.order_values(var, self.domain(var))
            frames.append(_Frame(var, values))

            while frames:
                fr = frames[-1]
                if fr.mark is not None:
                    self._undo(fr.mark)
                    self.stats.backtracks += 1
                    fr.mark = None
                v = next(fr.values, None)
                if v is None:
                    frames.pop()
                    continue
                if time.perf_counter() >= deadline:
                    return Result("TIMEOUT", stats=self.stats)
                self.stats.nodes += 1
                mark = self._assign(fr.var, v)
                if mark is None:
                    self.stats.backtracks += 1
                    continue
                fr.mark = mark
                break
            else:
                return Result("UNSAT", conflict=self._conflict(),
                              stats=self.stats)

    def _conflict(self):
        cons = set(self.problem.active_roots()) | self.touched
        if self.ac is not None:
            cons |= self.ac.used          # why AC-3 excluded untried versions
        return sorted(cons, key=lambda c: (c.kind != "root", str(c)))
