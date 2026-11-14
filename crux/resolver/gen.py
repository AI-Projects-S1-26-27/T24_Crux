"""Random small problems for correctness testing.

``random_problem(seed)`` is deterministic: the same seed gives the same
problem.  Instances are small enough for the brute-force oracle (the
proposal's "up to 8 packages, 6 versions each").  Versions are ``"1.0"`` ..
``"6.0"`` plus occasional pre-releases, so the pre-release rule is exercised.
Dependencies may form cycles and may point at packages with no releases.

This is for correctness tests only.  The benchmark's planted-conflict
generator is a different tool, owned by the benchmarking member.
"""

from __future__ import annotations

import random

from crux.data.graph import Graph
from crux.resolver.oracle import search_space
from crux.resolver.problem import Problem


def _spec(rng: random.Random, top: int) -> str:
    k = rng.randint(1, top)
    kind = rng.choice(["", ">=", ">=", "<", "<", ">", "==", "!=",
                       "range", "pre", "pre_upper"])
    if kind == "":
        return ""
    if kind == "range":
        lo = rng.randint(1, top)
        hi = rng.randint(lo, top + 1)
        return f">={lo}.0,<{hi}.0"
    if kind == "pre":
        return f">={k}.0b1"
    if kind == "pre_upper":
        return f"<{k}.0a1"
    return f"{kind}{k}.0"


def random_graph_dict(rng: random.Random, n_packages: int,
                      max_versions: int, max_deps: int = 3,
                      ghost_prob: float = 0.05,
                      prerelease_prob: float = 0.15) -> dict:
    names = [f"p{i}" for i in range(n_packages)]
    d: dict[str, dict[str, list]] = {}
    for name in names:
        n_ver = rng.randint(1, max_versions)
        versions = [f"{i}.0" for i in range(1, n_ver + 1)]
        if rng.random() < prerelease_prob:
            versions.append(f"{rng.randint(1, n_ver)}.0b1")
        if rng.random() < prerelease_prob / 2:
            versions.append(f"{rng.randint(1, n_ver)}.5rc1")
        d[name] = {}
        for v in versions:
            deps = []
            for _ in range(rng.randint(0, max_deps)):
                if rng.random() < ghost_prob:
                    deps.append(["ghost", ""])
                    continue
                other = rng.choice([n for n in names if n != name]
                                   or [name])
                if other != name:
                    deps.append([other, _spec(rng, max_versions)])
            d[name][v] = deps
    return d


def random_problem(seed: int, *, max_packages: int = 8,
                   max_versions: int = 6, max_states: int = 50_000,
                   max_roots: int = 3, max_deps: int = 3) -> Problem:
    """A deterministic random problem whose oracle search space is at most
    ``max_states`` (instances over the bound are redrawn smaller).
    Fewer ``max_deps`` gives more satisfiable instances."""
    rng = random.Random(seed)
    n_packages = rng.randint(2, max_packages)
    while True:
        d = random_graph_dict(rng, n_packages, max_versions,
                              max_deps=max_deps)
        names = sorted(d)
        roots = []
        for name in rng.sample(names, rng.randint(1, min(max_roots, len(names)))):
            roots.append(name + _spec(rng, max_versions))
        problem = Problem.from_requirements(Graph.from_dict(d), roots)
        if search_space(problem) <= max_states:
            return problem
        n_packages = max(2, n_packages - 1)
        max_versions = max(2, max_versions - (1 if rng.random() < 0.5 else 0))
