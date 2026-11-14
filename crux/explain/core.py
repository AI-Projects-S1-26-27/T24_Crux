"""Minimal unsatisfiable core extraction.

Given an UNSAT ``Problem``, find a set of its constraints (root requirements
and dependency edges) that

  * is unsatisfiable on its own: with every other constraint disabled, the
    problem is still UNSAT; and
  * is *minimal*: removing any single member makes it satisfiable.

That set is what the English renderer turns into "you cannot have A and B
because ...": every member is needed for the conflict, nothing else is.

The solver is the oracle.  "Subset S is UNSAT" is decided by
``solve(problem.with_disabled(U - S))`` where U is the problem's universe,
``Problem.constraints()``, computed ONCE.  (Recomputing it per call would be
wrong: disabling edges shrinks the static closure, so the universe of a
restricted problem is smaller than the original's.)

Why the algorithms are correct: unsatisfiability is *monotone*.  If S is a
subset of S' and S' has a plan P, then P cut down to the packages reachable
from S's roots through S's edges is a plan for S, because every S-constraint
it walks is an S'-constraint that P satisfies.  So adding constraints never
turns UNSAT into SAT, and removing them never turns SAT into UNSAT.
``tests/explain/test_core.py`` checks this property against the oracle.

Methods (``METHODS``):

  deletion          one constraint at a time: drop c; if the rest is still
                    UNSAT, c is unnecessary, otherwise keep it.  One solver
                    call per constraint of the starting set.  Minimal by
                    construction: c was kept because core_then - {c} is SAT,
                    the final core is a subset of core_then, so final - {c}
                    is SAT too (monotonicity).
  deletion+refine   deletion, but when a trial is UNSAT the solver's own
                    conflict for that trial (sound, usually smaller) replaces
                    the working set, so several constraints can go per call
                    ("clause-set refinement", Liffiton & Sakallah 2008).
                    Constraints already kept always survive a refinement (a
                    smaller UNSAT set without them would contradict the SAT
                    test that kept them).  A refinement trusts the resolver's
                    claim that its conflict is UNSAT on its own, so when any
                    refinement shrank the set, the final core is checked once
                    more; if it is not shown UNSAT, plain deletion is rerun
                    from the start.  With that check the proof above holds
                    whatever the resolver returns.
  quickxplain       Junker's divide and conquer (AAAI 2004).  Splits the set
                    in halves and recurses, so a core of k out of n costs
                    about 2k*log2(n/k) + 2k calls instead of n.  It is worse
                    than deletion when almost everything is needed (k close
                    to n).

Starting set.  By default the conflict of conflict-directed learning (C4
without AC-3 or LCV, see DEFAULT_SOLVE), which is sound and nearly tight,
not the whole universe: on real data the universe has thousands of edges
and that conflict a handful.  Any UNSAT starting set
works; minimality is relative to the problem, not to the start (a core is
minimal if no member can be dropped, whatever set it was cut from).

Timeouts.  A solver call that times out is "unknown".  The constraint under
test is then kept (keeping is always sound for unsatisfiability), and the
result says ``minimal=False``: not proven.  ``budget_s`` limits the whole
extraction; when it runs out the method stops and returns what it has (for
deletion the current working set, which is still UNSAT; for QuickXplain the
starting set), again with ``minimal=False``.  An unproven core is never
reported as minimal.

Different methods (and different starting orders) can return different
minimal cores when a problem has several.  Each one is minimal; none is
"the" core.  Which one comes back is deterministic for a given input.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Literal

from crux.resolver.problem import Config, Constraint, Problem
from crux.resolver.solver import solve

METHODS = ("deletion", "deletion+refine", "quickxplain")

# Every configuration gives the same SAT/UNSAT, so the choice is about speed
# and, where the conflict is used, about how tight the conflict is.
# (docs/EXPLAIN.md, sections "Which conflict to start from" and "Which
# configuration answers the oracle calls".)
#
# DEFAULT_ORACLE (deletion, quickxplain): only the status is used, so the most
# robust fast configuration: AC-3 + MRV + learning, no LCV.  Each part covers a
# case where the others are slow (measured, s per call):
#                                    AC-3+MRV  AC-3+MRV+learn  MRV+learn
#   real boto3/botocore conflict       0.098       0.101         8.3
#   thrash_wide(9), universe start     0.63        0.0007          -
# and on random_csp(16, 9) from the universe, QuickXplain with AC-3+MRV timed
# out (120 s budget) where AC-3+MRV+learning finished in 1.6 s.
DEFAULT_ORACLE = Config(ac3=True, mrv=True, lcv=False, learning=True,
                        timeout_s=10.0)

# REFINE_ORACLE (deletion+refine): the conflict IS used, so learning, and
# AC-3 deliberately OFF.  When AC-3 deleted a version the refutation relied
# on, learning.py adds ALL of AC-3's justifying edges to the conflict (coarse
# but sound).  Measured on the UNSAT instances among random_problem seeds
# 0..299 (215 of them): mean conflict size C0 5.6, C4 preset (with AC-3) 10.3,
# learning + MRV without AC-3 1.86, true minimal core 1.79.
REFINE_ORACLE = Config(ac3=False, mrv=True, lcv=False, learning=True,
                       timeout_s=10.0)

# The configuration explain() uses for the first solve: the tight conflict of
# learning without AC-3 is the starting set (same measurement as above).
DEFAULT_SOLVE = Config(ac3=False, mrv=True, lcv=False, learning=True,
                       timeout_s=60.0)


def default_oracle(method: str, timeout_s: float | None = None) -> Config:
    """The oracle configuration *method* uses unless told otherwise."""
    cfg = REFINE_ORACLE if method == "deletion+refine" else DEFAULT_ORACLE
    if timeout_s is not None:
        cfg = Config(cfg.ac3, cfg.mrv, cfg.lcv, cfg.learning, timeout_s,
                     cfg.nogoods)
    return cfg


@dataclass
class CoreResult:
    """Outcome of core extraction.

    status   the problem's own status: core extraction only happens on UNSAT;
             for SAT or TIMEOUT ``core`` is empty.
    core     the core, roots first then by text (deterministic).
    minimal  True only when every member was shown necessary by a solver call
             that finished (and the budget did not run out).
    calls    solver calls made (cache hits not counted); sat_calls +
             unsat_calls + timeouts == calls.
    """

    status: Literal["SAT", "UNSAT", "TIMEOUT"]
    method: str
    core: tuple[Constraint, ...] = ()
    minimal: bool = False
    start_size: int = 0
    calls: int = 0
    sat_calls: int = 0
    unsat_calls: int = 0
    timeouts: int = 0
    budget_exhausted: bool = False
    wall_s: float = 0.0          # whole extraction, including bookkeeping
    solver_s: float = 0.0        # time inside solve()

    @property
    def size(self) -> int:
        return len(self.core)


def _key(c: Constraint):
    return (c.kind != "root", str(c))


def _sorted(cons: Iterable[Constraint]) -> list[Constraint]:
    return sorted(set(cons), key=_key)


class _BudgetExhausted(Exception):
    pass


class _Oracle:
    """Decides SAT/UNSAT of a subset of the universe, with counters."""

    def __init__(self, problem: Problem, universe, config: Config,
                 deadline: float | None):
        self.problem = problem
        self.base = problem.disabled
        self.universe = frozenset(universe)
        self.config = config
        self.deadline = deadline
        self.cache: dict[frozenset, tuple[str, list | None]] = {}
        self.calls = self.sat = self.unsat = self.timeouts = 0
        self.solver_s = 0.0

    def check(self, subset) -> tuple[str, list | None]:
        """("SAT" | "UNSAT" | "TIMEOUT", conflict-or-None) for *subset*."""
        key = frozenset(subset)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        cfg = self.config
        if self.deadline is not None:
            left = self.deadline - time.perf_counter()
            if left <= 0:
                raise _BudgetExhausted
            if left < cfg.timeout_s:
                cfg = Config(cfg.ac3, cfg.mrv, cfg.lcv, cfg.learning,
                             left, cfg.nogoods)
        disabled = self.base | (self.universe - key)
        t = time.perf_counter()
        r = solve(self.problem.with_disabled(disabled), cfg)
        self.solver_s += time.perf_counter() - t
        self.calls += 1
        if r.status == "SAT":
            self.sat += 1
        elif r.status == "UNSAT":
            self.unsat += 1
        else:
            self.timeouts += 1
            if self.deadline is not None and \
                    time.perf_counter() >= self.deadline:
                raise _BudgetExhausted
        out = (r.status, r.conflict)
        self.cache[key] = out
        return out


# ── the methods ──────────────────────────────────────────────────────

def _deletion(oracle: _Oracle, start: list, refine: bool, state: dict):
    # Invariant: core[:i] are the constraints kept so far (each shown
    # necessary, or undecided after a timeout); core[i:] are untested.
    core = list(start)
    i = 0
    while i < len(core):
        state["core"] = core                # for the budget fallback
        trial = core[:i] + core[i + 1:]
        status, conflict = oracle.check(trial)
        if status == "UNSAT":
            if refine and conflict is not None:
                keep = set(conflict)
                # Accept only a conflict that is a strict subset of the trial
                # AND keeps every constraint kept so far.  A sound conflict
                # always does (a smaller UNSAT set without a kept c would
                # contradict the SAT call that kept c); refusing anything else
                # keeps the invariant, so no constraint is left untested.
                if keep <= set(trial) and len(keep) < len(trial) and \
                        all(c in keep for c in core[:i]):
                    trial = [c for c in trial if c in keep]
                    state["refined"] = True
            core = trial                    # index i now holds the next one
        else:                               # SAT: needed; TIMEOUT: unknown
            if status == "TIMEOUT":
                state["unproven"] = True
            i += 1
    state["core"] = core
    if state.get("refined"):
        # A refinement takes the resolver's word that its conflict is UNSAT
        # on its own.  Every kept constraint was shown necessary against a
        # superset of the final core (so it is necessary for the final core
        # too, by monotonicity); what is left to confirm is that the final
        # core is UNSAT.  One extra call, made only when refinement has
        # already saved at least one, so refine never costs more than plain
        # deletion.
        status, _ = oracle.check(core)
        if status == "SAT":                 # an unsound resolver conflict
            state["refined"] = False
            state["refine_failed"] = True
            return _deletion(oracle, list(start), False, state)
        if status == "TIMEOUT":             # not shown UNSAT: fall back to the
            state["unproven"] = True        # start, which is
            core = list(start)
            state["core"] = core
    return core


def _quickxplain(oracle: _Oracle, start: list, state: dict):
    def unsat(cons):
        status, _ = oracle.check(cons)
        if status == "TIMEOUT":
            state["unproven"] = True
        return status == "UNSAT"

    def qx(background, delta, cons):
        # Invariant: background + cons is UNSAT.  Returns a minimal subset
        # of cons that is UNSAT together with background.
        if delta and unsat(background):
            return []
        if len(cons) == 1:
            return list(cons)
        k = len(cons) // 2
        c1, c2 = cons[:k], cons[k:]
        d2 = qx(background + c1, c1, c2)
        d1 = qx(background + d2, d2, c1)
        return d1 + d2

    core = qx([], [], list(start))
    if state.get("unproven"):
        # Defence in depth.  With "unknown" treated as "not UNSAT" (above),
        # QuickXplain stays sound under timeouts: it only drops constraints
        # after a PROVEN UNSAT, so by induction every call returns a set that
        # is UNSAT with its background.  This re-check guards that reasoning
        # against a later edit: only return the result if it is still shown
        # UNSAT, else fall back to the (UNSAT) start.  The mutation check
        # found that breaking either safeguard alone is harmless and breaking
        # both is caught by tests/explain/test_timeouts.py.
        status, _ = oracle.check(core)
        if status != "UNSAT":
            core = list(start)
    return core


# ── public API ───────────────────────────────────────────────────────

def is_unsat(problem: Problem, constraints, config: Config = DEFAULT_ORACLE,
             universe=None) -> str:
    """Status ("SAT", "UNSAT" or "TIMEOUT") of *problem* restricted to
    *constraints* (everything else in its universe disabled)."""
    if universe is None:
        universe = problem.constraints()
    return _Oracle(problem, universe, config, None).check(
        list(constraints))[0]


def extract_core(problem: Problem, start=None, *, method: str = "quickxplain",
                 config: Config | None = None, budget_s: float | None = None,
                 check_start: bool = True, universe=None) -> CoreResult:
    """A minimal unsatisfiable core of *problem*, cut from *start*.

    *start*: an UNSAT set of the problem's constraints; default: the
    conflict of ``DEFAULT_SOLVE`` (learning + MRV without AC-3; one extra
    solve).  *check_start*: confirm that *start* is
    UNSAT first (one solver call, counted); if it is not, ``ValueError``.
    *universe*: ``problem.constraints()``, if the caller already has it.
    *config*: the oracle's configuration; default ``default_oracle(method)``.
    """
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {METHODS}")
    if config is None:
        config = default_oracle(method)
    t0 = time.perf_counter()
    if universe is None:
        universe = problem.constraints()
    universe = tuple(universe)
    if start is None:
        r = solve(problem, DEFAULT_SOLVE)
        if r.status != "UNSAT":
            raise ValueError(f"problem is {r.status}, not UNSAT")
        start, check_start = r.conflict, False
    start = _sorted(start)
    if not start:
        raise ValueError("an empty set cannot be unsatisfiable")
    unknown = set(start) - set(universe)
    if unknown:
        raise ValueError(f"not constraints of this problem: "
                         f"{sorted(map(str, unknown))[:3]}")

    deadline = None if budget_s is None else t0 + budget_s
    oracle = _Oracle(problem, universe, config, deadline)
    state: dict = {"core": start, "unproven": False}
    exhausted = False
    try:
        if check_start:
            status, _ = oracle.check(start)
            if status == "SAT":
                raise ValueError("the starting set is satisfiable")
            if status == "TIMEOUT":
                raise ValueError("could not show the starting set is UNSAT "
                                 "within the per-call timeout")
        if method == "quickxplain":
            core = _quickxplain(oracle, start, state)
        else:
            core = _deletion(oracle, start, method == "deletion+refine", state)
    except _BudgetExhausted:
        exhausted = True
        # Deletion's working set is UNSAT unless a refinement (resolver's
        # word, not yet re-checked) shaped it; then fall back to the start.
        if method == "quickxplain" or state.get("refined"):
            core = start
        else:
            core = state["core"]

    return CoreResult(
        status="UNSAT", method=method, core=tuple(_sorted(core)),
        minimal=not (exhausted or state["unproven"]),
        start_size=len(start), calls=oracle.calls, sat_calls=oracle.sat,
        unsat_calls=oracle.unsat, timeouts=oracle.timeouts,
        budget_exhausted=exhausted, wall_s=time.perf_counter() - t0,
        solver_s=oracle.solver_s)


def explain(problem: Problem, *, method: str = "quickxplain",
            solve_config: Config = DEFAULT_SOLVE,
            config: Config | None = None,
            budget_s: float | None = None) -> CoreResult:
    """Solve *problem*; if UNSAT, extract a minimal core from the solver's
    conflict.  For SAT or TIMEOUT the result has that status and no core.
    This is the entry point the renderer and the CLI should call."""
    t0 = time.perf_counter()
    r = solve(problem, solve_config)
    if r.status != "UNSAT":
        return CoreResult(status=r.status, method=method,
                          wall_s=time.perf_counter() - t0)
    out = extract_core(problem, r.conflict, method=method, config=config,
                       budget_s=budget_s, check_start=False)
    out.wall_s = time.perf_counter() - t0
    return out
