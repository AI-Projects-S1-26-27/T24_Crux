"""Plain-English messages for unsatisfiable resolution problems.

The message the proposal promises: a headline naming the user's
requirements that cannot hold together, then one short line for each place
where the requirements finally collide.  For the proposal's example:

    No solution. Your requirements a>=1.0 and b>=1.0 cannot both hold:
      every allowed version of a (1.0 to 1.4) needs c<2.0, but every
      allowed version of b (1.0 to 1.3) needs c>=3.1.

(one line in practice; wrapped here).  How a line is built from the core:

* The core is grouped (``group.py``).  A *target* is a package that core
  requirements point at but that has no requirements of its own in the
  core: it is where the conflict lands (``c`` above, ``botocore`` in the
  boto3 conflict).
* Every requirement on a target becomes one clause.  A requirement from a
  package the user asked for says which versions it covers ("every
  allowed version of a (1.0 to 1.4)", or "bramble 3.3.1" when releases of
  one package fail for different reasons).  A requirement from deeper in
  the graph is attributed to the user's requirement it comes from, with
  the packages in between: "jetty>=4.0.0 needs spruce<1.6.1 (through
  saffron)".  The user's own requirement on a target is "you asked for
  botocore<=1.12.7".
* Two or more clauses whose ranges share no version are one *clash*
  line.  A clause that no release satisfies at all gets its own line,
  saying why the release is missing when the data layer knows
  (``Graph.why_missing``).

Every group of the core is accounted for: a group that is neither on a
line nor on the way to one is listed on an "also involved" line, so the
message never hides part of the conflict.  ``Message.complete`` and
``Message.checked`` say whether that fallback was needed and whether every
clash was confirmed against the graph.

Context: docs/EXPLAIN.md sections 3 and 9.
"""

from __future__ import annotations

import textwrap
from collections import deque
from dataclasses import dataclass, field

from packaging.specifiers import SpecifierSet

from crux.data.config import MARKER_ENV, TARGET_PYTHON
from crux.data.graph import Graph
from crux.explain.group import Group, group_core

_ARCH = {"x86_64": "x86-64", "aarch64": "ARM64"}
TARGET = (f"CPython {TARGET_PYTHON} on "
          f"{MARKER_ENV.get('platform_system', 'Linux')} "
          f"{_ARCH.get(MARKER_ENV.get('platform_machine', ''), MARKER_ENV.get('platform_machine', ''))}").strip()

# why_missing() reasons, as (one release, several releases)
REASONS = {
    "no_wheel": ("has no wheel for {target}", "have no wheel for {target}"),
    "yanked": ("was yanked", "were yanked"),
    "py_version": ("requires a Python other than {py}",
                   "require a Python other than {py}"),
    "after_cutoff": ("was released after the snapshot date",
                     "were released after the snapshot date"),
    "after_as_of": ("was not yet released at the cutoff date",
                    "were not yet released at the cutoff date"),
    "prerelease": ("is a pre-release, so it must be asked for explicitly",
                   "are pre-releases, so they must be asked for explicitly"),
    "no_metadata": ("has no readable metadata", "have no readable metadata"),
}
_OTHER = ("is excluded from the snapshot ({reason})",
          "are excluded from the snapshot ({reason})")

_OP_ORDER = {">=": 0, ">": 0, "==": 1, "===": 1, "~=": 1, "<=": 2, "<": 2,
             "!=": 3}


# ── text helpers ────────────────────────────────────────────────────────

def pretty_spec(spec) -> str:
    """A specifier with lower bounds first: ``>=1.0,<2.0``, not packaging's
    sorted ``<2.0,>=1.0``."""
    parts = sorted(SpecifierSet(str(spec or "")),
                   key=lambda s: (_OP_ORDER.get(s.operator, 9), s.version))
    return ",".join(str(s) for s in parts)


def req(name: str, spec) -> str:
    return f"{name}{pretty_spec(spec)}"


def _and(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def span(versions) -> str:
    """'1.0', '1.0 and 1.1', '1.0, 1.1 and 1.2', '1.0 to 1.9 (10 releases)'."""
    vs = list(versions)
    if len(vs) <= 3:
        return _and(vs)
    return f"{vs[0]} to {vs[-1]} ({len(vs)} releases)"


def _inner(text: str) -> str:
    """A span placed inside parentheses: no nested parentheses."""
    return text.replace(" (", ", ").replace(")", "")


def _reason(reason: str, plural: bool) -> str:
    one, many = REASONS.get(reason, _OTHER)
    return (many if plural else one).format(
        target=TARGET, py=TARGET_PYTHON, reason=reason)


# when one version has several reasons, the one the message gives: the data
# layer's own drop reason first, then a date cut-off, then the pre-release rule
_LATER = {"after_as_of": 1, "prerelease": 2}


def one_reason_per_version(rows) -> list[tuple[str, str]]:
    """(version, reason) rows, newest first, with each version once."""
    best: dict[str, str] = {}
    order: list[str] = []
    for v, r in rows:
        if v not in best:
            order.append(v)
            best[v] = r
        elif (_LATER.get(r, 0), r) < (_LATER.get(best[v], 0), best[v]):
            best[v] = r
    return [(v, best[v]) for v in order]


def why_unavailable(graph: Graph, gr: Group) -> str:
    """Why no release of the group's dependency matches it."""
    t = gr.dep
    rows = one_reason_per_version(gr.missing)
    if len(rows) == 1:
        v, r = rows[0]
        return f"{t} {v} {_reason(r, False)}"
    if rows:
        count: dict[str, int] = {}
        for _, r in rows:
            count[r] = count.get(r, 0) + 1
        reasons = sorted(count, key=lambda r: (-count[r], r))
        n = len(rows)
        if len(reasons) == 1:
            return (f"the {n} matching releases of {t} all "
                    f"{_reason(reasons[0], True)}")
        parts = [f"{count[r]} {_reason(r, count[r] > 1)}" for r in reasons]
        return (f"none of the {n} matching releases of {t} is usable: "
                + _and(parts))
    usable = graph.candidates(t, "")
    if usable:
        return f"no release of {t} matches (the newest in the snapshot is {usable[0]})"
    if gr.dep_known:
        return f"{t} has only pre-releases in the snapshot, and none matches"
    return f"{t} has no releases in the snapshot"


# ── structure ───────────────────────────────────────────────────────────

@dataclass
class Clause:
    """One requirement on a target, ready to print."""
    text: str
    group: Group
    origin: int                 # index of the user's requirement it comes from
    allowed: frozenset          # versions of the target it permits
    unavailable: bool
    is_root: bool
    depth: int


@dataclass
class Message:
    """A rendered explanation.

    text      the message, lines joined with newlines.
    lines     the same, as a list (headline first).
    complete  every group of the core appears on a line or on the way to
              one; False means an "also involved" line had to be added.
    checked   every clash line was confirmed: its clauses really share no
              version of the target.
    groups    the grouped core the message was built from.
    """
    lines: list[str]
    complete: bool = True
    checked: bool = True
    groups: tuple[Group, ...] = ()
    named: set[str] = field(default_factory=set)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def wrapped(self, width: int) -> str:
        """The text with each line wrapped to *width* columns: the headline
        continues flush left, a clause continues two spaces further in.  ``lines`` stays unwrapped: it is
        what the benchmark counts."""
        out = []
        for line in self.lines:
            lead = len(line) - len(line.lstrip(" "))
            out.extend(textwrap.wrap(line.strip(), width,
                                     initial_indent=" " * lead,
                                     subsequent_indent=" " * (lead + 2 if lead else 0),
                                     break_on_hyphens=False,
                                     break_long_words=False) or [""])
        return "\n".join(out)

    @property
    def statements(self) -> int:
        """Distinct requirement statements: the groups (benchmark measure)."""
        return len(self.groups)


def _root_text(gr: Group) -> str:
    """How to refer to the user's requirement(s) on one package."""
    if gr.range is not None and len(gr.specs) == 1:
        return req(gr.dep, gr.specs[0])
    if gr.range is not None:
        return req(gr.dep, gr.range)
    return _and([req(gr.dep, s) for s in gr.specs])


def _subject(gr: Group) -> tuple[str, bool]:
    """Which releases of a package the user asked for an edge group covers,
    and whether that subject is plural."""
    if gr.covers_all and gr.n_allowed == 1:
        return f"the only allowed version of {gr.pkg} ({gr.first})", False
    if gr.covers_all:
        return (f"every allowed version of {gr.pkg} "
                f"({_inner(span(gr.versions))})"), False
    return f"{gr.pkg} {span(gr.versions)}", len(gr.versions) > 1


def _needs(gr: Group, plural: bool = False) -> str:
    verb = "need" if plural else "needs"
    if gr.common_spec is not None:
        return f"{verb} {req(gr.dep, gr.common_spec)}"
    return (f"{verb} {req(gr.dep, gr.range or '')} "
            "(the exact range differs by release)")


def _paths(groups) -> dict[str, list[str]]:
    """Shortest path of package names from a requested package to each
    package, through core edges."""
    adj: dict[str, list[str]] = {}
    for g in groups:
        if g.kind == "edge":
            adj.setdefault(g.pkg, []).append(g.dep)
    start = sorted({g.dep for g in groups if g.kind == "root"})
    path = {p: [p] for p in start}
    todo = deque(start)
    while todo:
        p = todo.popleft()
        for q in sorted(adj.get(p, ())):
            if q not in path:
                path[q] = path[p] + [q]
                todo.append(q)
    return path


def render_groups(graph, groups: tuple[Group, ...], *,
                  minimal: bool = True) -> Message:
    """Render already-grouped core *groups* (from ``group_core``)."""
    g: Graph = getattr(graph, "graph", graph)
    roots = {x.dep: x for x in groups if x.kind == "root"}
    edges = {(x.pkg, x.dep): x for x in groups if x.kind == "edge"}
    has_edges = {x.pkg for x in edges.values()}
    paths = _paths(groups)
    used: set[Group] = set(roots.values())

    # headline: the user's requirements, which are what they can change
    reqs = [req(c.dep, c.spec) for r in roots.values() for c in r.members]
    order = {dep: i for i, dep in enumerate(roots)}
    if len(reqs) == 1:
        head = f"No solution. Your requirement {reqs[0]} cannot be met:"
    elif len(reqs) == 2:
        head = f"No solution. Your requirements {_and(reqs)} cannot both hold:"
    else:
        head = f"No solution. Your requirements {_and(reqs)} cannot all hold:"

    # clauses, by target
    by_target: dict[str, list[Clause]] = {}
    for gr in groups:
        if gr.dep in has_edges:
            continue                       # not where the conflict lands
        if gr.kind == "root":
            text = f"you asked for {_root_text(gr)}"
            allowed = g.allowed(gr.dep, ",".join(gr.specs)) \
                if gr.range is not None else frozenset()
            cl = Clause(text, gr, order[gr.dep], allowed, gr.unavailable, True, 0)
        else:
            path = paths.get(gr.pkg)
            if path is None or len(path) == 1:
                who, plural = _subject(gr)
                text = f"{who} {_needs(gr, plural)}"
            else:
                first = edges.get((path[0], path[1]))
                if first is None or first.covers_all:
                    who, plural = _root_text(roots[path[0]]), False
                else:
                    who = f"{path[0]} {span(first.versions)}"
                    plural = len(first.versions) > 1
                via = []
                for a, b in zip(path[1:], path[2:] + [gr.dep]):
                    step = edges.get((a, b))
                    via.append(a if step is None or step.covers_all
                               else f"{a} {span(step.versions)}")
                    if step is not None:
                        used.add(step)
                if first is not None:
                    used.add(first)
                text = f"{who} {_needs(gr, plural)} (through {' → '.join(via)})"
            allowed = frozenset().union(*(g.allowed(gr.dep, s) for s in gr.specs))
            origin = order.get((path or [gr.pkg])[0], len(order))
            cl = Clause(text, gr, origin, allowed, gr.unavailable, False,
                        gr.depth or 0)
        used.add(gr)
        by_target.setdefault(gr.dep, []).append(cl)

    lines, checked = [head], True
    for t in sorted(by_target):
        cls = sorted(by_target[t],
                     key=lambda c: (c.is_root, c.origin, c.depth, c.text))
        clash = [c for c in cls if not c.unavailable]
        for c in cls:
            if not c.unavailable:
                continue
            if c.is_root and c.group.range is None:
                specs = [pretty_spec(x) for x in c.group.specs]
                lines.append(f"  no version of {t} satisfies "
                             f"{'both' if len(specs) == 2 else 'all of'} "
                             f"{_and(specs)}.")
            elif c.is_root:          # the headline already names the request
                lines.append(f"  {why_unavailable(g, c.group)}.")
            else:
                lines.append(f"  {c.text}, but {why_unavailable(g, c.group)}.")
        if len(clash) >= 2:
            common = frozenset.intersection(*(c.allowed for c in clash))
            checked &= not common
            if len(clash) == 2:
                lines.append(f"  {clash[0].text}, but {clash[1].text}.")
            else:
                lines.append(f"  {_and([c.text for c in clash])}; no version of "
                             f"{t} satisfies all of them.")
        elif len(clash) == 1 and not any(c.unavailable for c in cls):
            # a lone requirement on a target is not a conflict by itself
            checked = False
            lines.append(f"  {clash[0].text}.")

    rest = [x for x in groups if x not in used]
    for x in rest:
        if x.kind == "root":
            what = f"you asked for {_root_text(x)}"
        else:
            who, plural = _subject(x)
            what = f"{who} {_needs(x, plural)}"
        lines.append(f"  Also involved: {what}.")
    if not minimal:
        lines.append("  (Crux could not prove that this is the smallest set of "
                     "conflicting requirements.)")
    named = {x.dep for x in groups} | {x.pkg for x in groups if x.pkg}
    return Message(lines, complete=not rest, checked=checked, groups=groups,
                   named=named)


def render_core(problem, core, *, minimal: bool = True) -> Message:
    """Group *core* and render it."""
    return render_groups(problem, group_core(problem, core), minimal=minimal)


def render(problem, result, *, width: int | None = None) -> str:
    """The message for a ``CoreResult`` from ``explain(problem)``.  With
    *width*, lines are wrapped to that many columns."""
    if result.status == "SAT":
        return "Crux found versions that satisfy every requirement."
    if result.status == "TIMEOUT":
        return ("Crux could not decide within the time limit whether these "
                "requirements can be met.")
    if not result.core:
        return ("No solution, but Crux could not find which requirements "
                "conflict within its time budget.")
    m = render_core(problem, result.core, minimal=result.minimal)
    return m.wrapped(width) if width else m.text


def message(problem, *, method: str = "quickxplain",
            budget_s: float | None = 60.0, width: int | None = None) -> str:
    """Solve *problem* and explain the result: the one call the CLI and the
    benchmark make."""
    from crux.explain.core import explain
    return render(problem, explain(problem, method=method, budget_s=budget_s),
                  width=width)
