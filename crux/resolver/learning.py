"""C4: conflict-directed backjumping with nogood recording.

Plain backtracking undoes the most recent decision when it hits a dead end,
even if that decision has nothing to do with the dead end.  Here every dead end
is traced back to the decisions that actually caused it and the search jumps
straight to the most recent of those, skipping the irrelevant ones between.

Vocabulary
----------
decision    a package assigned a version (one search frame each, with a level).
blame       when a version of q is ruled out because of a requirement that came
            from package p's chosen version, p is to blame.  Root requirements
            and AC-3 deletions are static facts: no decision is blamed.
conflict    a pair (D, K): D = the decisions that together cause the dead end,
            K = the constraints used to show it.  Claim: under K, assigning D
            as it is currently assigned has no extension to a solution.
nogood      a recorded conflict ({(pkg, version)...}, K).  While every member
            but one is assigned, the remaining version is ruled out, exactly as
            if a requirement had excluded it, with D = the other members.

When all values of a variable q are exhausted (or q has none) its conflict is
the union of the blame for every version it could have taken:
  * versions ruled out by requirements / nogoods: the blamed decisions;
  * versions tried and failed: the conflict that came back from below, minus q;
  * a version rejected at once by an assigned neighbour r: {r}.
If D is empty nothing can be revised and the problem is UNSAT.  Otherwise the
search jumps to the decision in D with the highest level, adding D minus that
decision to its own conflict, and carries on with its next value.

This is conflict-directed backjumping (Prosser 1993) plus nogood recording
(Bayardo & Schrag 1997, in the spirit of Marques-Silva & Sakallah 1999).  It
remains complete with dynamic variable ordering because jumping only discards
decisions that the conflict proves irrelevant; the oracle tests check it.

The UNSAT conflict is the constraint set K of the final refutation, plus the
chain of activating constraints of every package that took part (a package
that nothing enabled activates would never appear).  It excludes everything the
refutation did not use, so it does not grow with irrelevant packages.  When
AC-3 deleted a version that the refutation relied on, all of AC-3's used
constraints are added (coarse but sound).
"""

from __future__ import annotations

import time

from crux.resolver.backtrack import Search
from crux.resolver.problem import Result

MAX_NOGOODS = 20_000          # stop recording beyond this; search stays correct


class _Frame:
    __slots__ = ("var", "values", "mark", "dom0", "D", "K", "level")

    def __init__(self, var, values, dom0, level):
        self.var = var
        self.values = iter(values)
        self.mark = None            # trail position before the current choice
        self.dom0 = dom0            # the domain the frame started with
        self.D = set()              # decisions to blame, gathered as it fails
        self.K = set()              # constraints used so far
        self.level = level


class LearningSearch(Search):
    def __init__(self, problem, config):
        super().__init__(problem, config)
        self.level: dict[str, int] = {}
        self.nogoods: dict[tuple, list] = {}   # (pkg, ver) -> [(others, K)]
        self._known: set[tuple] = set()
        self.final = None                      # constraints of the refutation

    # ── nogoods ──────────────────────────────────────────────────────

    def _nogood_reason(self, pkg, v):
        """(others, K) of a nogood that currently rules out pkg=v, or None."""
        for others, k in self.nogoods.get((pkg, v), ()):
            if all(self.assign.get(p) == w for p, w in others):
                return others, k
        return None

    def domain(self, pkg):
        out = super().domain(pkg)
        if self.nogoods:
            out = tuple(v for v in out if self._nogood_reason(pkg, v) is None)
        return out

    def _record(self, D, K):
        if not self.config.nogoods or len(self._known) >= MAX_NOGOODS:
            return
        members = tuple(sorted((p, self.assign[p]) for p in D))
        if members in self._known:
            return
        self._known.add(members)
        k = frozenset(K)
        for i, m in enumerate(members):
            self.nogoods.setdefault(m, []).append(
                (members[:i] + members[i + 1:], k))
        self.stats.nogoods = len(self._known)

    # ── blame ────────────────────────────────────────────────────────

    def _chain(self, pkg):
        """Constraints that activated *pkg*, then its activator, up to a root."""
        out, seen = set(), set()
        while pkg is not None and pkg not in seen:
            seen.add(pkg)
            infos = self.req_info.get(pkg)
            if not infos:
                break
            con = infos[0]
            out.add(con)
            pkg = con.pkg if con.kind == "edge" else None
        return out

    def _blame(self, pkg, D, K):
        D.add(pkg)
        K |= self._chain(pkg)

    def _explain(self, var, dom0):
        """(D, K) accounting for every version of *var* outside *dom0*."""
        D, K = set(), set()
        g = self.graph
        reqs = self.req_info[var]
        ac_dom = self.ac.domains[var] if self.ac is not None else None
        for w in g.versions(var):
            if w in dom0:
                continue
            best = None                     # excluding requirement, lowest level
            for con in reqs:
                if w not in g.allowed(var, con.specifier):
                    lvl = -1 if con.kind == "root" else self.level[con.pkg]
                    if best is None or lvl < best[0]:
                        best = (lvl, con)
            if best is not None and (best[0] < 0 or ac_dom is None
                                     or w in ac_dom):
                con = best[1]
                K.add(con)
                if con.kind == "edge":
                    self._blame(con.pkg, D, K)
                continue
            if ac_dom is not None and w not in ac_dom:
                K |= self.ac.used           # static: AC-3 deleted it
                continue
            hit = self._nogood_reason(var, w)
            if hit is None:                 # cannot happen; fail loudly
                raise AssertionError(f"{var} {w}: removed for no known reason")
            others, k = hit
            K |= k
            for p, _ in others:
                self._blame(p, D, K)
        return D, K

    # ── the search ───────────────────────────────────────────────────

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
            dom = self.domain(var)
            frames.append(_Frame(var, self.order_values(var, dom),
                                 frozenset(dom), len(frames)))

            while True:
                fr = frames[-1]
                if fr.mark is not None:
                    self._undo(fr.mark)
                    self.stats.backtracks += 1
                    fr.mark = None
                v = next(fr.values, None)
                if v is None:
                    done = self._dead_end(frames)
                    if done is not None:
                        return done
                    continue
                hit = self._nogood_reason(fr.var, v) if self.nogoods else None
                if hit is not None:       # learned since this frame began
                    others, k = hit
                    fr.K |= k
                    for p, _ in others:
                        self._blame(p, fr.D, fr.K)
                    continue
                if time.perf_counter() >= deadline:
                    return Result("TIMEOUT", stats=self.stats)
                self.stats.nodes += 1
                mark = self._assign(fr.var, v)
                if mark is None:          # an assigned neighbour rejects it
                    self.stats.backtracks += 1
                    con = self.last_fail
                    fr.K.add(con)
                    self._blame(con.dep, fr.D, fr.K)
                    continue
                self.level[fr.var] = fr.level
                fr.mark = mark
                break

    def _dead_end(self, frames):
        """The top frame has no value left.  Returns a finished UNSAT Result,
        or None after rewinding to the decision to revise."""
        top = frames[-1]
        D0, K0 = self._explain(top.var, top.dom0)
        D = (top.D | D0) - {top.var}
        K = top.K | K0 | self._chain(top.var)
        # The dead end only exists because top.var is active, and that is a
        # consequence of the decision whose requirement activated it.  Change
        # that decision and the package may vanish.  (Its own activator is
        # added when its frame dead-ends in turn.)  Forgetting this made a
        # package with no versions at all look like an unconditional UNSAT.
        activator = self.req_info[top.var][0]
        if activator.kind == "edge":
            D.add(activator.pkg)
        if D:
            self._record(D, K)
        else:
            self.final = K
            return Result("UNSAT", conflict=self._conflict(), stats=self.stats)

        target = max(self.level[p] for p in D)
        if len(frames) - 2 - target > 0:
            self.stats.backjumps += 1
        while len(frames) - 1 > target:
            f = frames.pop()
            if f.mark is not None:
                self._undo(f.mark)
                self.stats.backtracks += 1
        t = frames[-1]
        assert t.level == target and t.var in D
        t.D |= D - {t.var}
        t.K |= K
        return None

    def _conflict(self):
        if self.final is None:
            return super()._conflict()          # AC-3 proved it before search
        return sorted(self.final, key=lambda c: (c.kind != "root", str(c)))
