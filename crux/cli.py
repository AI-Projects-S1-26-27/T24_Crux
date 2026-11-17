"""Command-line interface: resolve requirements and explain failures.

    crux resolve "flask>=3" "requests<3"             # against data/crux.db
    crux resolve a b --graph tests/fixtures/toy_abc.json
    crux resolve --instance benchmarks/corpus/instances.jsonl.gz --id ID
    crux resolve --instance benchmarks/planted/instances.jsonl   # list ids

The same commands work as ``python -m crux resolve ...``.

On success the plan goes to stdout, one ``name==version`` per line, so
``crux resolve flask > requirements.txt`` works. With no solution, stdout gets
the short explanation from ``crux.explain.message``: the same text the
benchmark measures (docs/RENDER.md section 3). Statistics and errors go to
stderr, so they never mix with the plan.

Exit codes: 0 resolved, 1 no solution, 2 bad input, 3 timeout.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from crux.data.graph import Graph
from crux.explain import message
from crux.resolver import Config, Problem, solve

OK, NO_SOLUTION, BAD_INPUT, TIMEOUT = 0, 1, 2, 3

# C0..C4 from the proposal, plus C4 without LCV, which the benchmark uses:
# LCV picks very old releases on real data (docs/context.md 6.3).
CONFIGS = {
    **{f"C{i}": Config.named(f"C{i}") for i in range(5)},
    "C4-noLCV": Config(ac3=True, mrv=True, learning=True),
}
DEFAULT_CONFIG = "C4-noLCV"
DEFAULT_DB = "data/crux.db"


class InputError(Exception):
    """A problem with the user's input: reported in one line, exit 2."""


# ── building the problem ─────────────────────────────────────────────

def _instance(path: str, wanted: str | None):
    """The instance with id *wanted* from a JSONL file, or None to list."""
    from crux.benchmark.instance import load_jsonl
    if not Path(path).exists():
        raise InputError(f"no such instance file: {path}")
    instances = load_jsonl(path)
    if wanted is None:
        return None, instances
    for inst in instances:
        if inst.id == wanted:
            return inst, instances
    raise InputError(f"no instance with id {wanted!r} in {path} "
                     f"(run without --id to list them)")


def _graph(args) -> Graph:
    """The graph from --graph or --db."""
    if args.graph:
        import json
        path = Path(args.graph)
        if not path.exists():
            raise InputError(f"no such graph file: {path}")
        return Graph.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if not Path(args.db).exists():
        raise InputError(
            f"no database at {args.db}. Build it with `python -m "
            f"crux.data.run_ingest --limit 30`, `python -m crux.data.clean`, "
            f"or use --graph / --instance instead")
    try:
        return Graph.load(args.db)
    except RuntimeError as exc:          # the DB exists but is not cleaned
        raise InputError(str(exc)) from exc


def build_problem(args) -> Problem:
    if args.instance:
        inst, _ = _instance(args.instance, args.id)
        if not args.requirements:        # use the instance's own request
            return inst.problem()
        graph = inst.problem().graph
    else:
        if not args.requirements:
            raise InputError("give at least one requirement, e.g. "
                             "crux resolve \"requests>=2\"")
        graph = _graph(args)
    try:
        return Problem.from_requirements(graph, args.requirements)
    except ValueError as exc:
        # bad syntax (packaging's InvalidRequirement is a ValueError), or an
        # extra, marker or URL, which Crux does not support (docs/SCOPE.md)
        raise InputError(str(exc)) from exc


# ── output ───────────────────────────────────────────────────────────

def format_plan(plan) -> str:
    return "\n".join(f"{name}=={plan[name]}" for name in sorted(plan))


def format_stats(name: str, result) -> str:
    s = result.stats
    return (f"[{name}] {result.status}: {s.nodes} nodes, {s.backtracks} "
            f"backtracks, {s.backjumps} backjumps, {s.ac3_pruned} versions "
            f"pruned by AC-3, {s.wall_s:.3f} s")


# ── commands ─────────────────────────────────────────────────────────

def cmd_resolve(args) -> int:
    if args.instance and args.id is None:        # list the ids and stop
        _, instances = _instance(args.instance, None)
        for inst in instances:
            print(f"{inst.id}\t{inst.expected or '?'}")
        return OK

    problem = build_problem(args)
    config = dataclasses.replace(CONFIGS[args.config], timeout_s=args.timeout)
    result = solve(problem, config)
    if args.stats:
        print(format_stats(args.config, result), file=sys.stderr)

    if result.status == "SAT":
        print(format_plan(result.plan))
        return OK
    if result.status == "UNSAT":
        # message() solves again with the explanation module's own settings
        # and extracts a minimal core, so the text is exactly what the
        # benchmark measured. Costs one more solve, only on failure.
        print(message(problem, budget_s=args.timeout,
                      width=args.width or None))
        return NO_SOLUTION
    print(f"Crux could not decide within {args.timeout:g} s whether these "
          f"requirements can be met (try --timeout).", file=sys.stderr)
    return TIMEOUT


# ── argument parsing ─────────────────────────────────────────────────

def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crux",
        description="Resolve Python requirements against a PyPI snapshot and "
                    "explain why, when they cannot all hold.")
    commands = parser.add_subparsers(dest="command", required=True)

    r = commands.add_parser(
        "resolve", help="find one version of every needed package",
        description="Print an install plan, or a short explanation of why "
                    "there is none. Exit code: 0 resolved, 1 no solution, "
                    "2 bad input, 3 timeout.")
    r.add_argument("requirements", nargs="*", metavar="REQ",
                   help='a requirement such as "flask>=3" (no extras, '
                        'markers or URLs)')
    source = r.add_mutually_exclusive_group()
    source.add_argument("--db", default=DEFAULT_DB,
                        help=f"snapshot database (default {DEFAULT_DB})")
    source.add_argument("--graph", metavar="FILE",
                        help="a JSON graph {pkg: {version: [[dep, spec]]}}")
    source.add_argument("--instance", metavar="FILE",
                        help="a benchmark JSONL file; without --id, lists "
                             "its instance ids")
    r.add_argument("--id", help="instance id within --instance")
    r.add_argument("--config", default=DEFAULT_CONFIG, choices=list(CONFIGS),
                   help=f"search configuration (default {DEFAULT_CONFIG})")
    r.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS",
                   help="time limit for the search (default 60)")
    r.add_argument("--width", type=int, default=80,
                   help="wrap explanations at this width; 0 = no wrapping")
    r.add_argument("--stats", action="store_true",
                   help="print search statistics to stderr")
    r.set_defaults(func=cmd_resolve)
    return parser


def _utf8_output() -> None:
    """Write UTF-8 even where the default is a legacy code page.

    Explanations of deep conflicts contain "→". On Windows, output that is
    redirected (``> plan.txt``) or piped uses cp1252, which has no "→",
    and print() would crash.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv=None) -> int:
    _utf8_output()
    parser = make_parser()
    args = parser.parse_args(argv)
    if getattr(args, "id", None) and not getattr(args, "instance", None):
        parser.error("--id needs --instance")
    try:
        return args.func(args)
    except InputError as exc:
        print(f"crux: error: {exc}", file=sys.stderr)
        return BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
