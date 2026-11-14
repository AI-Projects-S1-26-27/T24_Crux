"""Independent plan checker.

Decides whether a plan is a valid answer to a Problem, WITHOUT running or
importing any solver code and without Graph.candidates()/allowed().  It reads
only Graph.versions() and Graph.deps().  Every later component is tested
against this verdict, so keep it simple and keep it separate.

A plan is valid when ALL of these hold (rules are stated in docs/context.md):

  1. every package name is canonical and every version is a real release;
  2. every active root requirement is met by the plan;
  3. for every chosen release, every non-disabled dependency is met;
  4. the plan contains exactly the *active* packages: those reachable from an
     active root through non-disabled dependencies of the chosen releases.
     A disabled constraint is ignored completely: it neither has to hold nor
     activates its dependency.

"Met" means the dependency is in the plan and its version is admissible for
the specifier under the pre-release rule below.

Pre-release rule (the one Graph applies through packaging, re-derived here on
purpose so the checker is independent): a pre-release version is admissible
for a specifier only if the specifier itself names a pre-release in an
*inclusive* clause (any operator except ``!=``), or if NO final release of
that package satisfies the specifier.  Judged per package, not per version,
which is why SpecifierSet.contains() alone is not enough.

``!=1.0b1`` does not "name" a pre-release: excluding one pre-release is not an
invitation to pick others.  packaging 26.0 agrees, and the first version of this
checker got it wrong (it counted ``!=``), so the checker and the oracle disagreed
with every solver on specs like ``a!=1.0b1, a!=1.0``.  Found by an independent
review; ``test_admissible_matches_graph_candidates`` now includes such specs.
"""

from __future__ import annotations

from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from crux.resolver.problem import Constraint, Problem


@dataclass(frozen=True)
class Violation:
    kind: str                       # see KINDS
    message: str
    pkg: str | None = None
    constraint: Constraint | None = None


KINDS = (
    "bad_plan",             # plan is not {str: Version}
    "non_canonical_name",   # key is not a PEP 503 name (or two keys collide)
    "unknown_package",      # package has no releases in the graph
    "unknown_version",      # version is not a release of that package
    "missing_dependency",   # a required package is not in the plan
    "unsatisfied",          # present, but its version is not admissible
    "unneeded_package",     # in the plan but not reachable from active roots
)


def admissible(spec: SpecifierSet, version: Version, releases) -> bool:
    """Is *version* an admissible choice for *spec* among *releases*?

    *releases* is every known version of the package.
    """
    if not spec.contains(version, prereleases=True):
        return False
    if not version.is_prerelease:
        return True
    if any(s_version.is_prerelease for s_version in _spec_versions(spec)):
        return True
    return not any(
        not r.is_prerelease and spec.contains(r, prereleases=True)
        for r in releases)


def _spec_versions(spec: SpecifierSet):
    """Versions named by the specifier's inclusive clauses (not ``!=``)."""
    for s in spec:
        if s.operator == "!=":
            continue
        try:
            yield Version(s.version.rstrip(".*"))
        except Exception:
            continue


def check_plan(problem: Problem, plan) -> list[Violation]:
    """Return every violation found; an empty list means the plan is valid."""
    out: list[Violation] = []
    graph = problem.graph

    # ── 1. shape, names, versions ────────────────────────────────────
    if not isinstance(plan, dict):
        return [Violation("bad_plan", "plan must be a dict")]
    chosen: dict[str, Version] = {}
    for key, ver in plan.items():
        if not isinstance(key, str) or not isinstance(ver, Version):
            out.append(Violation(
                "bad_plan", f"entry {key!r}: {ver!r} is not str -> Version"))
            continue
        canon = canonicalize_name(key)
        if canon != key or canon in chosen:
            out.append(Violation(
                "non_canonical_name",
                f"{key!r} is not the canonical name {canon!r}"
                if canon != key else f"{key!r} appears twice", pkg=canon))
            if canon in chosen:
                continue
        chosen[canon] = ver

    releases = {}      # pkg -> set of versions, only for packages we look at

    def known(pkg):
        if pkg not in releases:
            releases[pkg] = frozenset(graph.versions(pkg))
        return releases[pkg]

    for pkg, ver in chosen.items():
        if not known(pkg):
            out.append(Violation(
                "unknown_package", f"{pkg} has no releases in the graph",
                pkg=pkg))
        elif ver not in known(pkg):
            out.append(Violation(
                "unknown_version", f"{pkg} {ver} is not a known release",
                pkg=pkg))

    # ── 2-4. walk from the active roots through chosen releases ──────
    reached: set[str] = set()
    todo: list[Constraint] = list(problem.active_roots())
    seen_constraints: set[Constraint] = set()
    # Reachability is only known if every chosen release could be expanded.
    # A missing or unknown package hides its dependencies, which would then
    # look "unneeded"; that would be noise, so it is not reported.
    complete = not any(v.kind == "bad_plan" for v in out)

    while todo:
        con = todo.pop()
        if con in seen_constraints:
            continue
        seen_constraints.add(con)
        dep = con.dep
        reached.add(dep)
        ver = chosen.get(dep)
        if ver is None:
            complete = False
            out.append(Violation(
                "missing_dependency",
                f"{con}: {dep} is not in the plan", pkg=dep, constraint=con))
            continue
        if not admissible(con.specifier, ver, known(dep)):
            out.append(Violation(
                "unsatisfied",
                f"{con}: plan has {dep} {ver}", pkg=dep, constraint=con))
        if ver not in known(dep):
            complete = False
            continue                      # already reported as unknown_version
        for sub_dep, sub_spec in graph.deps(dep, ver):
            edge = Constraint.edge(dep, ver, sub_dep, sub_spec)
            if edge not in problem.disabled:
                todo.append(edge)

    if complete:
        for pkg in sorted(set(chosen) - reached):
            out.append(Violation(
                "unneeded_package",
                f"{pkg} {chosen[pkg]} is not required by any active root",
                pkg=pkg))
    return out


def is_valid(problem: Problem, plan) -> bool:
    return not check_plan(problem, plan)
