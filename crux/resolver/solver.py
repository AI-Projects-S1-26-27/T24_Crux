"""Solver entry point: ``solve(problem, config)``.

Configurations (see Config.named): C0 plain backtracking, C1 + AC-3, C2 + MRV,
C3 + LCV, C4 + conflict-directed backjumping with nogoods.  The flags are
independent, so any combination runs.
"""

from __future__ import annotations

import time

from crux.resolver.backtrack import Search
from crux.resolver.learning import LearningSearch
from crux.resolver.problem import Config, Problem, Result


def solve(problem: Problem, config: Config = Config()) -> Result:
    """Resolve *problem* under *config*.

    Honours ``problem.disabled`` and ``config.timeout_s`` (a TIMEOUT result,
    never an exception).  All configurations must agree on SAT/UNSAT; they
    differ only in effort (``Result.stats``) and in how tight the UNSAT
    ``conflict`` is.
    """
    t0 = time.perf_counter()
    search = (LearningSearch if config.learning else Search)(problem, config)
    result = search.run(t0)
    result.stats.wall_s = time.perf_counter() - t0
    return result
