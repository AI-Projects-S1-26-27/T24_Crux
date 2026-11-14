"""Public types of the resolver: the contract between solver, oracle,
heuristics and the explanation module.

Keep this module small and stable.  Other components are written against it,
so extend it by adding fields, never by changing existing ones.

Model (full context in docs/context.md):
  * variables are packages, domains are their versions (via Graph.candidates);
  * a package is *active* only when a root requirement or a chosen version
    needs it (dynamic CSP); the plan lists exactly the active packages, so
    "not installed" is the absence of a key, not a domain value;
  * every requirement is a ``Constraint`` that can be switched off through
    ``Problem.disabled``, which is what core extraction needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from crux.data.graph import Graph


@dataclass(frozen=True)
class Constraint:
    """One requirement: a root requirement or one dependency edge.

    Hashable and comparable, so it is its own identifier: no separate ids.
    ``spec`` is ``str(SpecifierSet)``, which packaging sorts, so ">=1,<2" and
    "<2,>=1" give equal constraints.

    kind="root": the user asked for ``dep`` within ``spec``.  pkg/version None.
    kind="edge": release ``pkg`` ``version`` requires ``dep`` within ``spec``.
    """

    kind: Literal["root", "edge"]
    dep: str
    spec: str
    pkg: str | None = None
    version: str | None = None

    def __post_init__(self):
        if self.kind == "root" and (self.pkg is not None
                                    or self.version is not None):
            raise ValueError("a root constraint has no pkg or version")
        if self.kind == "edge" and (self.pkg is None or self.version is None):
            raise ValueError("an edge constraint needs pkg and version")

    @classmethod
    def root(cls, dep, spec=""):
        return cls("root", canonicalize_name(dep), str(_as_spec(spec)))

    @classmethod
    def edge(cls, pkg, version, dep, spec=""):
        return cls("edge", canonicalize_name(dep), str(_as_spec(spec)),
                   canonicalize_name(pkg), str(Version(str(version))))

    @property
    def specifier(self) -> SpecifierSet:
        return SpecifierSet(self.spec)

    def __str__(self):
        need = f"{self.dep}{self.spec}" if self.spec else self.dep
        if self.kind == "root":
            return f"root requires {need}"
        return f"{self.pkg} {self.version} requires {need}"


def _as_spec(spec) -> SpecifierSet:
    return spec if isinstance(spec, SpecifierSet) else SpecifierSet(spec or "")


@dataclass(frozen=True)
class Problem:
    """What to resolve.

    ``roots`` are the user's requirements as root ``Constraint`` s.
    ``disabled`` constraints are ignored by every solver and by the checker;
    core extraction calls ``solve`` repeatedly with different ``disabled``.
    "Ignored" is total: a disabled constraint need not hold AND does not
    activate its dependency (that package only joins the plan if some other
    active constraint needs it).
    """

    graph: Graph
    roots: tuple[Constraint, ...]
    disabled: frozenset[Constraint] = frozenset()

    def __post_init__(self):
        for c in self.roots:
            if c.kind != "root":
                raise ValueError(f"roots must be root constraints: {c}")

    @classmethod
    def from_requirements(cls, graph: Graph, requirements,
                          disabled=frozenset()):
        """Build from strings like ``"flask>=3"``.  Names are canonicalized.

        Markers, extras and URLs are out of scope (docs/SCOPE.md) and raise
        ValueError instead of being silently ignored.
        """
        roots = []
        for text in requirements:
            req = Requirement(text)
            if req.marker is not None or req.extras or req.url:
                raise ValueError(
                    f"unsupported requirement (marker/extra/url): {text!r}")
            roots.append(Constraint.root(req.name, req.specifier))
        return cls(graph, tuple(roots), frozenset(disabled))

    def active_roots(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.roots if c not in self.disabled)

    def constraints(self) -> tuple[Constraint, ...]:
        """Every enabled constraint that could ever matter: the active roots
        plus the dependency edges of every release of every package in the
        static closure (reachable from the active roots through any
        release's enabled edges).  Deterministic order.  This is the set a
        core extractor chooses its subsets from."""
        g = self.graph
        out = list(self.active_roots())
        seen: set[str] = set()
        todo = [c.dep for c in out]
        while todo:
            pkg = todo.pop()
            if pkg in seen:
                continue
            seen.add(pkg)
            for v in g.versions(pkg):
                for dep, spec in g.deps(pkg, v):
                    con = Constraint.edge(pkg, v, dep, spec)
                    if con not in self.disabled:
                        out.append(con)
                        todo.append(con.dep)
        return tuple(dict.fromkeys(out))

    def with_disabled(self, disabled) -> "Problem":
        return Problem(self.graph, self.roots, frozenset(disabled))


@dataclass(frozen=True)
class Config:
    """Which techniques are on.  Flags are independent; presets are the
    cumulative configurations from the proposal."""

    ac3: bool = False        # C1: arc consistency before search
    mrv: bool = False        # C2: fewest remaining versions first
    lcv: bool = False        # C3: least constraining version first
    learning: bool = False   # C4: conflict-directed backjumping + nogoods
    timeout_s: float = 60.0
    nogoods: bool = True     # with learning on: also record nogoods (False =
                             # backjumping only, to measure each part alone)

    @classmethod
    def named(cls, name: str, **overrides) -> "Config":
        order = ["ac3", "mrv", "lcv", "learning"]
        if name not in {"C0", "C1", "C2", "C3", "C4"}:
            raise ValueError(f"unknown configuration {name!r}")
        n = int(name[1])
        flags = {flag: i < n for i, flag in enumerate(order)}
        flags.update(overrides)
        return cls(**flags)


@dataclass
class Stats:
    nodes: int = 0          # assignments tried
    backtracks: int = 0     # undone assignments
    ac3_pruned: int = 0     # versions removed by AC-3 arc propagation
    wall_s: float = 0.0
    ac3_versions: int = 0   # versions in the domains AC-3 started from
                            # (pruned / versions = share pruned)
    nogoods: int = 0        # C4: nogoods recorded
    backjumps: int = 0      # C4: jumps that skipped at least one decision


@dataclass
class Result:
    """Outcome of ``solve``.

    SAT:     ``plan`` maps each active package to one Version.
    UNSAT:   ``conflict`` lists constraints that together cannot hold.  It is
             a sound but NOT necessarily minimal set; minimality is the
             explanation module's job, using ``solve`` as its oracle.
    TIMEOUT: neither plan nor conflict.
    """

    status: Literal["SAT", "UNSAT", "TIMEOUT"]
    plan: dict[str, Version] | None = None
    conflict: list[Constraint] | None = None
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self):
        if self.status == "SAT":
            if self.plan is None or self.conflict is not None:
                raise ValueError("SAT needs a plan and no conflict")
        elif self.status == "UNSAT":
            if self.plan is not None or self.conflict is None:
                raise ValueError("UNSAT needs a conflict and no plan")
        elif self.status == "TIMEOUT":
            if self.plan is not None or self.conflict is not None:
                raise ValueError("TIMEOUT has neither plan nor conflict")
        else:
            raise ValueError(f"unknown status {self.status!r}")
