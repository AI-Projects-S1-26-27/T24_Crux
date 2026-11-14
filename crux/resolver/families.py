"""Scalable problem families for the C0 to C4 ablation.

Each family is a function ``build(size, seed) -> Case`` that grows with
``size``.  Where possible the answer is known by construction (``expected``),
so correctness at a scale the brute-force oracle cannot reach is still
checked.  The families are chosen so that each technique has something to
win on:

  thrash        irrelevant choices are made before a late conflict, so plain
                backtracking re-tries every combination of them (k**n).
                What conflict-driven backjumping and good ordering fix.
  thrash_wide   the same, but the culprits have as many versions as the free
                packages, so MRV has no domain-size signal; only backjumping
                (C4) fixes it.
  pinned_pair   boto3/botocore-style: every release of one package pins the
                same release of another, and a root constraint rules out all
                but the oldest.  Plain search scans the releases one by one.
  chain         n packages in a line.  Easy control: nodes == n.  Shows the
                harness overhead and that nothing regresses on easy input.
  random_csp    random interval constraints between packages, near the hard
                region.  Answer not known by construction.

These are synthetic and deliberately simple.  The benchmark's real-failure
corpus and planted-conflict generator belong to the benchmarking member.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from crux.data.graph import Graph
from crux.resolver.problem import Problem


@dataclass(frozen=True)
class Case:
    family: str
    size: int
    seed: int
    problem: Problem
    expected: str | None        # "SAT", "UNSAT", or None if unknown

    @property
    def name(self) -> str:
        return f"{self.family}/{self.size}/s{self.seed}"


@dataclass(frozen=True)
class Family:
    name: str
    build: Callable[[int, int], Case]
    sizes: tuple[int, ...]      # default sizes for a benchmark run
    doc: str
    seeded: bool = False        # False: same instance for every seed


def _problem(d, roots):
    return Problem.from_requirements(Graph.from_dict(d), roots)


# ── thrash ───────────────────────────────────────────────────────────

def thrash(size: int, seed: int = 0, k: int = 3) -> Case:
    """``size`` free packages with ``k`` versions each, then a two-package
    conflict that is discovered only after all of them are assigned.

    x needs y<2 and z needs y>=3, so no plan exists, and none of the free
    packages is involved.  Roots are listed free-first, so plain search
    assigns all of them (k**size combinations) before it meets the conflict.
    """
    d: dict = {}
    roots = []
    for i in range(size):
        d[f"f{i}"] = {str(v): [] for v in range(1, k + 1)}
        roots.append(f"f{i}")
    d["x"] = {"1": [["y", "<2"]]}
    d["z"] = {"1": [["y", ">=3"]]}
    d["y"] = {"1": [], "2": [], "3": []}
    roots += ["x", "z"]
    return Case("thrash", size, seed, _problem(d, roots), "UNSAT")


def thrash_wide(size: int, seed: int = 0, k: int = 3) -> Case:
    """``thrash``, but the two conflicting packages have ``k`` versions each,
    the same as every free package.

    MRV orders by domain size, and here every domain has the same size until
    the conflict is already built, so there is no signal to reorder on and the
    free packages are still assigned first.  Every version of x needs y<2 and
    every version of z needs y>=3, so the conflict involves x, z and y only.
    Plain search, AC-3, MRV and LCV all re-try every combination of the free
    packages (k**size); conflict-directed backjumping jumps over them.
    """
    d: dict = {}
    roots = []
    for i in range(size):
        d[f"f{i}"] = {str(v): [] for v in range(1, k + 1)}
        roots.append(f"f{i}")
    d["x"] = {str(v): [["y", "<2"]] for v in range(1, k + 1)}
    d["z"] = {str(v): [["y", ">=3"]] for v in range(1, k + 1)}
    d["y"] = {"1": [], "2": [], "3": []}
    roots += ["x", "z"]
    return Case("thrash_wide", size, seed, _problem(d, roots), "UNSAT")


# ── pinned_pair ──────────────────────────────────────────────────────

def pinned_pair(size: int, seed: int = 0) -> Case:
    """boto3 release v requires botocore==v; the root wants botocore<=1.

    Only boto3 1 / botocore 1 works.  Plain search tries boto3 from the
    newest release down, ``size`` releases, finding each one dead.
    """
    d = {
        "boto3": {str(v): [["botocore", f"=={v}"]]
                  for v in range(1, size + 1)},
        "botocore": {str(v): [] for v in range(1, size + 1)},
    }
    return Case("pinned_pair", size, seed,
                _problem(d, ["boto3", "botocore<=1"]), "SAT")


# ── chain ────────────────────────────────────────────────────────────

def chain(size: int, seed: int = 0) -> Case:
    d = {f"p{i}": {"1": [[f"p{i + 1}", ""]] if i < size - 1 else []}
         for i in range(size)}
    return Case("chain", size, seed, _problem(d, ["p0"]), "SAT")


# ── random_csp ───────────────────────────────────────────────────────

def random_csp(size: int, seed: int = 0, k: int = 4,
               density: float | None = None, tightness: float = 0.4) -> Case:
    """``size`` packages with ``k`` versions each, all required by the root.

    For every pair i < j, with probability ``density``, each release of
    package i requires package j to lie in a random interval of releases
    that excludes about ``tightness`` of j's versions.  Expected status is
    unknown.

    ``density`` defaults to ``3.6 / size`` (about 3.6 constraining
    neighbours per package).  Measured with plain backtracking at k=4,
    tightness=0.4, that sits at the SAT/UNSAT transition for sizes 16 to
    24: mixed answers, 10**3 to 10**5 nodes, timeouts at size 24.  With
    k=5 the instances are trivially satisfiable (no backtracking at all),
    so keep k=4 unless you re-measure.
    """
    if density is None:
        density = min(1.0, 3.6 / size)
    rng = random.Random(seed)
    width = max(1, round(k * (1 - tightness)))
    d: dict = {}
    for i in range(size):
        d[f"q{i}"] = {str(v): [] for v in range(1, k + 1)}
    for i in range(size):
        for j in range(i + 1, size):
            if rng.random() >= density:
                continue
            for v in range(1, k + 1):
                lo = rng.randint(1, k - width + 1)
                d[f"q{i}"][str(v)].append(
                    [f"q{j}", f">={lo},<={lo + width - 1}"])
    roots = [f"q{i}" for i in range(size)]
    return Case("random_csp", size, seed, _problem(d, roots), None)


FAMILIES: dict[str, Family] = {
    f.name: f for f in (
        Family("thrash", thrash, (3, 5, 7, 9),
               "free choices before a late conflict (UNSAT)"),
        Family("thrash_wide", thrash_wide, (3, 5, 7, 9),
               "like thrash, but MRV has no domain-size signal (UNSAT)"),
        Family("pinned_pair", pinned_pair, (10, 50, 200, 1000),
               "boto3/botocore-style linear scan (SAT)"),
        Family("chain", chain, (10, 100, 1000),
               "easy control, nodes == size (SAT)"),
        Family("random_csp", random_csp, (12, 16, 20, 24),
               "random interval constraints near the hard region",
               seeded=True),
    )
}
