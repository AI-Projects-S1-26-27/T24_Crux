"""Planted-conflict generator: unsatisfiable instances whose minimal core is
known *and proven*.

Each instance hides one conflict ("gadget") inside optional harmless background
packages.  The generator records the gadget's constraints as the planted core K.

Why K is the right answer, and the only one
-------------------------------------------
A minimal unsatisfiable core is not unique in general, so "the core" needs a
guarantee.  For an instance I and a set K of its constraints:

    K is the ONLY minimal unsatisfiable subset of I
    iff  K alone is unsatisfiable,
    and  for every k in K, I without k is satisfiable.

(If some minimal core M missed a k in K, then M would sit inside I - {k},
which is satisfiable: impossible.  So every minimal core contains K; K is
itself unsatisfiable, so the minimal core is K.)

``prove`` checks both halves without trusting any solver under test:

* "K alone is UNSAT": the brute-force oracle (crux.resolver.oracle), which
  never prunes and shares no code with the solvers, enumerates every
  assignment of the instance with everything outside K disabled.
* "I - {k} is SAT", for each k: an explicit install plan, checked by the
  independent plan checker (crux.resolver.checker).  A valid plan is a
  certificate, whoever found it.  Plans are built from the oracle's plan for
  the gadget alone plus the background's known working versions.

Consequence used by the harness: every sound conflict a solver reports must
contain K, because every unsatisfiable subset contains a minimal core.

Gadgets (``kind``)
------------------
direct      two requirements need disjoint ranges of one package C
deep        the same, but each side reaches C through a chain of packages
            (``depth`` = longest chain; the other side is 1..depth)
impossible  a chain of ``depth`` packages ends in a requirement no release
            meets: no release at all ("missing"), or none new enough ("too_new")
partial     older releases of A clash with B over C; newer releases of A need
            a release of D that does not exist
threeway    three requirements on C, every pair compatible, all three not

Background (``noise``): none | small (6-10 packages) | large (25-40 packages).
Background packages form a DAG with a known working version each; gadget
releases get extra dependencies on them, so the conflict is buried among
constraints that do not matter.  Background never depends on the gadget.
Edges per background release are drawn so the average is about 2 with a
quarter of releases having none, roughly the pilot's 2.2 and 25%
(docs/PILOT.md).  This is a rough shape match, not a fitted model.

CLI:  python -m crux.benchmark.planted --out benchmarks/planted/instances.jsonl
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from crux.benchmark.instance import Instance, dump_jsonl, load_jsonl
from crux.data.graph import Graph
from crux.resolver.checker import check_plan
from crux.resolver.oracle import brute_force, search_space
from crux.resolver.problem import Constraint, Problem

KINDS = ("direct", "deep", "impossible", "partial", "threeway")
NOISE = ("none", "small", "large")
CORE_STATE_CAP = 2_000_000      # the oracle's default cap

WORDS = (
    "acorn amber aster atlas basalt beacon birch bramble cedar cinder cobalt "
    "comet coral cypress dagger delta drift dune echo ember fable fennel fjord "
    "flint garnet glyph granite grove harbor hazel helix heron indigo inlet "
    "iris ivory jade jasper juniper kelp kestrel knoll larch lark lotus lumen "
    "maple marble meadow mesa mica mosaic nectar nimbus north oasis ochre onyx "
    "opal orbit osprey pebble pine prism pylon quarry quartz quill raven reef "
    "ripple rowan sable saffron sage slate sorrel spruce summit talon thistle "
    "thorn tide topaz tundra umber upland vapor velvet vertex violet wharf "
    "willow wren yarrow yew zenith zephyr zinc alder bison canyon dahlia egret "
    "falcon gale hollow islet jetty kiln lagoon mangrove nettle otter "
    "pumice quince rapids sequoia tamarack urchin vireo walnut"
).split()


@dataclass(frozen=True)
class Params:
    kind: str
    depth: int = 1
    noise: str = "none"
    seed: int = 0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")
        if self.noise not in NOISE:
            raise ValueError(f"unknown noise {self.noise!r}")

    @property
    def id(self) -> str:
        return f"planted/{self.kind}/d{self.depth}/{self.noise}/s{self.seed}"


class PlantedProofError(AssertionError):
    """The recorded core is not the unique minimal core of the instance."""


# ── small helpers ────────────────────────────────────────────────────

def _versions(rng: random.Random, n: int) -> list[str]:
    """n ascending release versions with realistic patch/minor/major steps."""
    major, minor, patch = rng.choice((0, 1, 1, 1, 2, 3)), rng.randint(0, 5), 0
    out = [f"{major}.{minor}.{patch}"]
    while len(out) < n:
        r = rng.random()
        if r < 0.5:
            patch += 1
        elif r < 0.85:
            minor, patch = minor + 1, 0
        else:
            major, minor, patch = major + 1, 0, 0
        out.append(f"{major}.{minor}.{patch}")
    return out


def _bump(v: str) -> str:
    """A version strictly greater than v (for 'needs a release not yet out')."""
    major, minor, _ = (int(x) for x in v.split("."))
    return f"{major}.{minor + 1}.0"


def _admitted(versions, spec: str) -> list[str]:
    s = SpecifierSet(spec)
    return [v for v in versions if s.contains(Version(v))]


def _subset_spec(rng: random.Random, vs: list[str]) -> str:
    """A specifier admitting a non-empty contiguous run of vs (ascending)."""
    n = len(vs)
    form = rng.choice(("any", "ge", "ge", "lt", "range"))
    if form == "any" or n == 1:
        return ""
    if form == "ge":
        return f">={vs[rng.randrange(n)]}"
    if form == "lt":
        return f"<{vs[rng.randrange(1, n)]}"
    lo = rng.randrange(n - 1)
    hi = rng.randrange(lo + 1, n)
    return f">={vs[lo]},<{vs[hi]}"


def _spec_admitting(rng: random.Random, vs: list[str], w: str) -> str:
    """A specifier over vs that admits version w."""
    i = vs.index(w)
    form = rng.choice(("any", "ge", "ge", "lt", "range", "ne"))
    if form == "ge":
        return f">={vs[rng.randrange(i + 1)]}"
    if form == "lt" and i + 1 < len(vs):
        return f"<{vs[rng.randrange(i + 1, len(vs))]}"
    if form == "range" and i + 1 < len(vs):
        return f">={vs[rng.randrange(i + 1)]},<{vs[rng.randrange(i + 1, len(vs))]}"
    if form == "ne" and len(vs) > 1:
        return f"!={rng.choice([v for v in vs if v != w])}"
    return ""


def _spec_any(rng: random.Random, vs: list[str]) -> str:
    """Any plausible specifier over vs (may admit nothing useful)."""
    return _spec_admitting(rng, vs, rng.choice(vs))


class _Build:
    """Accumulates one instance: graph, roots and the planted core."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.graph: dict[str, dict[str, list[list[str]]]] = {}
        self.roots: list[tuple[str, str]] = []
        self.core: set[Constraint] = set()
        self.gadget: set[str] = set()
        self._names = iter(rng.sample(WORDS, len(WORDS)))

    def package(self, n: int, gadget: bool = True) -> tuple[str, list[str]]:
        name = next(self._names)
        vs = _versions(self.rng, n)
        self.graph[name] = {v: [] for v in vs}
        if gadget:
            self.gadget.add(name)
        return name, vs

    def missing(self) -> str:
        """A package name with no releases at all."""
        name = next(self._names)
        self.gadget.add(name)
        return name

    def edge(self, pkg, ver, dep, spec, core: bool):
        self.graph[pkg][ver].append([dep, spec])
        if core:
            self.core.add(Constraint.edge(pkg, ver, dep, spec))

    def root(self, dep, spec, core: bool):
        self.roots.append((dep, spec))
        if core:
            self.core.add(Constraint.root(dep, spec))

    def require(self, requirers, targets, core: bool = True):
        """Make each requirer depend on every target.

        ``requirers``: "root", or (pkg, admissible versions, all versions).
        ``targets``: [(dep, spec)].  Only admissible versions' edges are in
        the core; the package's other versions get a plausible edge or none,
        which is realistic and never matters (they cannot be chosen).
        """
        if requirers == "root":
            for dep, spec in targets:
                self.root(dep, spec, core)
            return
        pkg, adm, all_vs = requirers
        for v in all_vs:
            for dep, spec in targets:
                if v in adm:
                    self.edge(pkg, v, dep, spec, core)
                elif self.rng.random() < 0.5 and dep in self.graph:
                    self.edge(pkg, v, dep,
                              _spec_any(self.rng, list(self.graph[dep])), False)

    def chain(self, requirer, depth: int, n_versions: int):
        """``depth`` packages in a line below ``requirer``.  Returns the last
        package as (pkg, admissible versions, all versions)."""
        cur = requirer
        for _ in range(depth):
            pkg, vs = self.package(n_versions)
            spec = _subset_spec(self.rng, vs)
            self.require(cur, [(pkg, spec)])
            cur = (pkg, _admitted(vs, spec), vs)
        return cur

    def top(self, n_sides: int):
        """Where the sides hang from: the roots, or one root package 'app'
        whose admissible releases need every side (both are common)."""
        if self.rng.random() < 0.5:
            return ["root"] * n_sides, "roots"
        app, vs = self.package(self.rng.randint(1, 3))
        spec = _subset_spec(self.rng, vs)
        self.root(app, spec, core=True)
        return [(app, _admitted(vs, spec), vs)] * n_sides, "app"


# ── gadgets ──────────────────────────────────────────────────────────

def _contested(b: _Build):
    """Package C and two specs whose admitted versions are disjoint."""
    c, vs = b.package(b.rng.randint(4, 8))
    i = b.rng.randrange(1, len(vs))          # left side admits vs[:i]
    j = b.rng.randrange(i, len(vs))          # right side admits vs[j:]
    return c, f"<{vs[i]}", f">={vs[j]}"


def _sides(b: _Build, depths, n_versions):
    tops, style = b.top(len(depths))
    ends = []
    for top, d in zip(tops, depths):
        if d == 0:
            ends.append(top)
        else:
            ends.append(b.chain(top, d, n_versions))
    return ends, style


def _direct(b: _Build, p: Params):
    c, left, right = _contested(b)
    (a_end, b_end), style = _sides(b, (1, 1), b.rng.randint(2, 5))
    b.require(a_end, [(c, left)])
    b.require(b_end, [(c, right)])
    return {"roots_style": style}


def _deep(b: _Build, p: Params):
    other = b.rng.randint(1, p.depth)
    depths = (p.depth, other) if b.rng.random() < 0.5 else (other, p.depth)
    n_versions = 3 if p.depth <= 3 else 2     # keeps the oracle proof small
    c, left, right = _contested(b)
    (a_end, b_end), style = _sides(b, depths, n_versions)
    b.require(a_end, [(c, left)])
    b.require(b_end, [(c, right)])
    return {"roots_style": style, "side_depths": list(depths)}


def _impossible(b: _Build, p: Params):
    variant = "missing" if p.seed % 2 == 0 else "too_new"
    if variant == "missing":
        target, spec = b.missing(), _subset_spec(b.rng, _versions(b.rng, 3))
    else:
        target, vs = b.package(b.rng.randint(2, 6))
        spec = f">={_bump(vs[-1])}"
    if p.depth == 0:
        b.root(target, spec, core=True)
        return {"variant": variant, "roots_style": "roots"}
    (end,), style = _sides(b, (p.depth,), 3 if p.depth <= 2 else 2)
    b.require(end, [(target, spec)])
    return {"variant": variant, "roots_style": style}


def _partial(b: _Build, p: Params):
    c, left, right = _contested(b)
    d, d_vs = b.package(b.rng.randint(2, 5))
    a, a_vs = b.package(b.rng.randint(3, 6))
    bb, bb_vs = b.package(b.rng.randint(2, 4))
    a_spec = f">={a_vs[b.rng.randrange(len(a_vs) - 1)]}"   # admits >= 2
    bb_spec = _subset_spec(b.rng, bb_vs)
    b.root(a, a_spec, core=True)
    b.root(bb, bb_spec, core=True)
    a_adm = _admitted(a_vs, a_spec)
    cut = b.rng.randrange(1, len(a_adm))
    old, new = a_adm[:cut], a_adm[cut:]
    b.require((a, old, old), [(c, left)])
    b.require((a, new, new), [(d, f">={_bump(d_vs[-1])}")])
    b.require((bb, _admitted(bb_vs, bb_spec), bb_vs), [(c, right)])
    return {"roots_style": "roots", "old": len(old), "new": len(new)}


def _threeway(b: _Build, p: Params):
    c, vs = b.package(b.rng.randint(4, 8))
    m = b.rng.randrange(1, len(vs) - 1)      # needs a version below and above
    specs = (f"<{vs[m + 1]}", f">={vs[m]}", f"!={vs[m]}")
    ends, style = _sides(b, (1, 1, 1), b.rng.randint(2, 4))
    for end, spec in zip(ends, specs):
        b.require(end, [(c, spec)])
    return {"roots_style": style}


_GADGETS = {"direct": _direct, "deep": _deep, "impossible": _impossible,
            "partial": _partial, "threeway": _threeway}


# ── background ───────────────────────────────────────────────────────

_BG_VERSIONS = ((1, 12), (2, 14), (3, 14), (4, 12), (6, 12), (8, 10),
                (12, 10), (20, 8), (30, 8))
_BG_DEGREE = ((0, 25), (1, 13), (2, 20), (3, 18), (4, 13), (5, 11))
_NOISE = {"small": ((6, 10), 1, 0.3), "large": ((25, 40), 3, 0.5)}


def _weighted(rng, table):
    values, weights = zip(*table)
    return rng.choices(values, weights)[0]


def _background(b: _Build, noise: str) -> dict:
    """Satisfiable background with a known working version per package."""
    if noise == "none":
        return {}
    (lo, hi), n_roots, p_edge = _NOISE[noise]
    rng = b.rng
    pkgs = []
    for _ in range(rng.randint(lo, hi)):
        name, vs = b.package(_weighted(rng, _BG_VERSIONS), gadget=False)
        pkgs.append((name, vs, rng.choice(vs)))
    witness = {name: w for name, _, w in pkgs}
    # DAG: package i only depends on packages after it
    for i, (name, vs, w) in enumerate(pkgs):
        later = pkgs[i + 1:]
        for v in vs:
            k = min(_weighted(rng, _BG_DEGREE), len(later))
            for dep, dvs, dw in rng.sample(later, k):
                if v == w or rng.random() < 0.7:
                    spec = _spec_admitting(rng, dvs, dw)
                else:
                    spec = _spec_any(rng, dvs)      # may be a dead end
                b.edge(name, v, dep, spec, core=False)
    for dep, dvs, dw in rng.sample(pkgs, min(n_roots, len(pkgs))):
        b.root(dep, _spec_admitting(rng, dvs, dw), core=False)
    # bury the gadget: some gadget releases also need background packages
    for g in sorted(b.gadget):
        for v in b.graph.get(g, {}):
            if rng.random() < p_edge:
                for dep, dvs, dw in rng.sample(pkgs, rng.randint(1, 2)):
                    b.edge(g, v, dep, _spec_admitting(rng, dvs, dw), core=False)
    return witness


# ── generation ───────────────────────────────────────────────────────

def generate(p: Params) -> Instance:
    """Build the instance for ``p`` (deterministic) and prove its core."""
    rng = random.Random(p.id)
    b = _Build(rng)
    info = _GADGETS[p.kind](b, p)
    witness = _background(b, p.noise)
    graph = {pkg: {v: deps for v, deps in vs.items()}
             for pkg, vs in b.graph.items()}
    n_versions = sum(len(vs) for vs in graph.values())
    n_edges = sum(len(d) for vs in graph.values() for d in vs.values())
    inst = Instance(
        id=p.id, graph=graph, roots=tuple(b.roots), expected="UNSAT",
        core=frozenset(b.core),
        meta={
            "source": "planted", "kind": p.kind, "depth": p.depth,
            "noise": p.noise, "seed": p.seed, **info,
            "packages": len(graph), "versions": n_versions, "edges": n_edges,
            "core_size": len(b.core),
            "core_packages": sorted({c.dep for c in b.core}
                                    | {c.pkg for c in b.core if c.pkg}),
            "background": {k: witness[k] for k in sorted(witness)},
        },
    )
    proof = prove(inst)
    inst.meta["proof"] = proof
    return inst


def _walk(problem: Problem, assign: dict[str, Version]) -> dict[str, Version]:
    """Restrict a full assignment to the packages the plan activates."""
    g = problem.graph
    active, stack = {}, [c.dep for c in problem.active_roots()]
    while stack:
        pkg = stack.pop()
        if pkg in active or pkg not in assign:
            continue
        active[pkg] = assign[pkg]
        for dep, spec in g.deps(pkg, assign[pkg]):
            if Constraint.edge(pkg, assign[pkg], dep, spec) not in problem.disabled:
                stack.append(dep)
    return active


def prove(inst: Instance, *, max_states: int = CORE_STATE_CAP) -> dict:
    """Check that ``inst.core`` is the unique minimal core (module docstring).
    Raises PlantedProofError otherwise; returns a summary of the evidence."""
    graph = Graph.from_dict(inst.graph)
    full = inst.problem(graph)
    everything = set(full.constraints())
    core = set(inst.core)
    stray = core - everything
    if stray:
        raise PlantedProofError(f"core lists constraints not in the instance: "
                                f"{sorted(map(str, stray))[:3]}")

    # 1. K alone is unsatisfiable (oracle, exhaustive)
    alone = full.with_disabled(everything - core)
    space = search_space(alone)
    res = brute_force(alone, max_states=max_states)
    if res.status != "UNSAT":
        raise PlantedProofError(f"{inst.id}: the core alone has a plan {res.plan}")

    # 2. for each k in K, I - {k} has a checker-valid plan
    background = {k: Version(v) for k, v in inst.meta.get("background", {}).items()}
    gadget_noise = {c for c in everything
                    if (c.kind == "root" and c.dep in background)
                    or (c.kind == "edge" and c.dep in background
                        and c.pkg not in background)}
    for k in sorted(core, key=str):
        without_k = full.with_disabled({k})
        gadget_only = full.with_disabled({k} | gadget_noise)
        sub = brute_force(gadget_only, max_states=max_states)
        if sub.status != "SAT":
            raise PlantedProofError(f"{inst.id}: removing {k} leaves the "
                                    "gadget unsatisfiable, so it is not needed")
        plan = _walk(without_k, {**background, **sub.plan})
        bad = check_plan(without_k, plan)
        if bad:
            raise PlantedProofError(f"{inst.id}: witness for removing {k} "
                                    f"fails the checker: {bad[:2]}")
    return {"core_alone": "UNSAT", "oracle_states": res.states,
            "search_space": space, "witnesses_checked": len(core)}


def suite() -> list[Params]:
    """The committed benchmark: 198 instances."""
    out = []
    for noise in NOISE:
        out += [Params("direct", 1, noise, s) for s in range(10)]
        out += [Params("deep", d, noise, s) for d in range(2, 7) for s in range(4)]
        out += [Params("impossible", d, noise, s) for d in range(4) for s in range(3)]
        out += [Params("partial", 1, noise, s) for s in range(12)]
        out += [Params("threeway", 1, noise, s) for s in range(12)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="benchmarks/planted/instances.jsonl")
    ap.add_argument("--verify", metavar="JSONL",
                    help="re-prove every instance in an existing file")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    if args.verify:
        insts = load_jsonl(args.verify)
        for inst in insts:
            prove(inst)
            regen = generate(Params(inst.meta["kind"], inst.meta["depth"],
                                    inst.meta["noise"], inst.meta["seed"]))
            if regen.to_json() != inst.to_json():
                raise PlantedProofError(f"{inst.id}: does not regenerate identically")
        print(f"verified {len(insts)} instances "
              f"({time.perf_counter() - t0:.1f}s): every core is the unique "
              "minimal core, and every instance regenerates identically")
        return 0
    insts = [generate(p) for p in suite()]
    n = dump_jsonl(insts, args.out)
    print(f"wrote {n} proven instances to {args.out} "
          f"({time.perf_counter() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
