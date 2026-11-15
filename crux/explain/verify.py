"""Independent check that a core is unsatisfiable and minimal.

Shares no code with ``core.py``.  For a candidate core K of a problem whose
universe (``Problem.constraints()``) is U, it decides

  1. K is UNSAT: the problem with U - K disabled has no plan;
  2. K is minimal: for every member c, K - {c} has a plan.

How each verdict is reached, strongest first (recorded in the result):

  oracle    the brute-force oracle (``resolver/oracle.py``), which shares no
            code with any solver, when the restricted problem's search space
            is at most ``max_states``.  Restricting to K usually makes the
            closure tiny, so this covers most synthetic cores.
  plan      for "K - {c} is SAT" on bigger problems: plain backtracking (C0)
            returns a plan and the independent checker (``check_plan``)
            accepts it.  A checked plan is a certificate; no trust in the
            solver is needed.
  solvers   for UNSAT on bigger problems: C0 (plain backtracking) and C4
            without LCV (AC-3 + MRV + learning), two different search
            algorithms, both say UNSAT.  The second is also the extractor's
            default oracle, so the verdict's independence rests on C0.  This
            is the weakest verdict (there is no certificate for UNSAT short of
            exhaustive search) and it is labelled as such.

A verdict that cannot be reached (a timeout) is ``None``, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crux.resolver.checker import check_plan
from crux.resolver.oracle import brute_force
from crux.resolver.problem import Config, Constraint, Problem
from crux.resolver.solver import solve

@dataclass
class CoreCheck:
    unsat: bool | None                  # is the core UNSAT on its own?
    unsat_by: str                       # "oracle" | "solvers" | "" (unknown)
    minimal: bool | None                # is every member necessary?
    necessary_by: dict = field(default_factory=dict)  # Constraint -> how
    redundant: tuple[Constraint, ...] = ()   # removable: K - {c} still UNSAT
    unknown: tuple[Constraint, ...] = ()     # could not decide (timeout)
    errors: tuple[str, ...] = ()        # a solver plan the checker rejected

    @property
    def ok(self) -> bool:
        return self.unsat is True and self.minimal is True and not self.errors


def _restrict(problem: Problem, universe: frozenset, keep) -> Problem:
    return problem.with_disabled(problem.disabled | (universe - set(keep)))


def _small_enough(p: Problem, max_states: int) -> bool:
    """Whether the oracle's search space for *p* is at most *max_states*.

    Same number as ``oracle.search_space`` (the product, over the static
    closure, of each package's release count, a package without releases
    counting 1), but it stops as soon as the bound is passed and does not
    build the oracle's admissibility tables: on the real boto3/botocore core
    ``search_space`` takes 2.8 s per call, which made verifying a core of
    hundreds of members take hours.  ``test_verify.py`` checks the two agree.
    """
    g = p.graph
    seen: set[str] = set()
    todo = [c.dep for c in p.active_roots()]
    size = 1
    while todo:
        pkg = todo.pop()
        if pkg in seen:
            continue
        seen.add(pkg)
        vs = g.versions(pkg)
        size *= max(1, len(vs))
        if size > max_states:
            return False
        for v in vs:
            for dep, spec in g.deps(pkg, v):
                con = Constraint.edge(pkg, v, dep, spec)
                if con not in p.disabled:
                    todo.append(con.dep)
    return size <= max_states


def _decide(p: Problem, max_states: int, timeout_s: float, errors: list):
    """("SAT" | "UNSAT" | None, how)."""
    if _small_enough(p, max_states):
        return brute_force(p, max_states=max_states).status, "oracle"
    cfg0 = Config(timeout_s=timeout_s)
    r0 = solve(p, cfg0)
    if r0.status == "SAT":
        bad = check_plan(p, r0.plan)
        if not bad:
            return "SAT", "plan"
        errors.append(f"C0 plan rejected by the checker: {bad[0].message}")
        return None, ""
    if r0.status == "TIMEOUT":
        return None, ""
    r4 = solve(p, Config(ac3=True, mrv=True, learning=True,
                         timeout_s=timeout_s))
    if r4.status == "UNSAT":
        return "UNSAT", "solvers"
    if r4.status == "SAT":
        errors.append("C0 says UNSAT but C4 found a plan")
    return None, ""


def verify_core(problem: Problem, core, *, max_states: int = 200_000,
                timeout_s: float = 30.0, universe=None) -> CoreCheck:
    """Check that *core* is an unsatisfiable, minimal subset of *problem*'s
    constraints.  See the module docstring for how verdicts are reached."""
    core = list(dict.fromkeys(core))
    if universe is None:
        universe = problem.constraints()
    universe = frozenset(universe)
    outside = [c for c in core if c not in universe]
    if outside:
        raise ValueError(f"not constraints of this problem: {outside[:3]}")

    errors: list[str] = []
    status, how = _decide(_restrict(problem, universe, core), max_states,
                          timeout_s, errors)
    unsat = None if status is None else status == "UNSAT"

    necessary_by: dict = {}
    redundant, unknown = [], []
    for c in core:
        rest = [x for x in core if x != c]
        st, by = _decide(_restrict(problem, universe, rest), max_states,
                         timeout_s, errors)
        if st == "SAT":
            necessary_by[c] = by
        elif st == "UNSAT":
            redundant.append(c)
        else:
            unknown.append(c)
    minimal = False if redundant else (None if unknown else True)
    return CoreCheck(unsat=unsat, unsat_by=how if unsat is not None else "",
                     minimal=minimal, necessary_by=necessary_by,
                     redundant=tuple(redundant), unknown=tuple(unknown),
                     errors=tuple(errors))
