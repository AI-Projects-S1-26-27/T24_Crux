"""Edge-case fuzzing: every configuration against the oracle and the checker.

The main random generator (`gen.py`) deliberately stays simple.  This one
produces what it never does: `!=` against a pre-release, wildcards, `~=`,
mixed `!=`/range specs, self-dependencies, exact and different-spec duplicate
roots, roots on packages that have no releases, pre-release-only packages, and
random disabled sets drawn from `Problem.constraints()`.

Adapted from the scratch fuzzer of an independent review, which found that the
checker counted `!=1.0b1` as naming a pre-release while packaging and every
solver did not (40 wrong answers and 18 unsound conflicts in 2,000 instances).
"""

import itertools
import random

import pytest

from crux.data.graph import Graph
from crux.resolver import Config, Constraint, Problem, solve
from crux.resolver.checker import check_plan
from crux.resolver.oracle import OracleLimitExceeded, brute_force

CONFIGS = [Config(ac3=a, mrv=m, lcv=l, learning=le)
           for a, m, l, le in itertools.product((False, True), repeat=4)]
CONFIGS += [Config(ac3=a, mrv=m, learning=True, nogoods=False)
            for a in (False, True) for m in (False, True)]


def _spec(rng, versions):
    k = rng.choice(versions) if versions else "1.0"
    kind = rng.choice(["", "", ">=", "<", ">", "==", "!=", "<=", "range",
                       "wild", "pre", "pre_upper", "neq_pre", "multi",
                       "tilde"])
    if kind == "":
        return ""
    if kind == "range":
        lo, hi = sorted([rng.randint(1, 4), rng.randint(1, 5)])
        return f">={lo}.0,<{hi}.0"
    if kind == "wild":
        return f"=={rng.randint(1, 3)}.*"
    if kind == "pre":
        return f">={rng.randint(1, 3)}.0b1"
    if kind == "pre_upper":
        return f"<{rng.randint(1, 4)}.0a1"
    if kind == "neq_pre":
        return f"!={rng.randint(1, 3)}.0b1"
    if kind == "multi":
        return f"!={k},>={rng.randint(1, 2)}.0"
    if kind == "tilde":
        return f"~={rng.randint(1, 3)}.0"
    return f"{kind}{k}"


def edge_problem(seed):
    rng = random.Random(seed)
    names = [f"p{i}" for i in range(rng.randint(1, 5))]
    versions = {}
    for name in names:
        r = rng.random()
        if r < 0.1:
            vs = []                                          # no releases
        elif r < 0.2:
            vs = rng.sample(["1.0b1", "2.0rc1", "1.0a1", "3.0.dev1"],
                            rng.randint(1, 2))               # pre-release only
        else:
            vs = [f"{i}.0" for i in range(1, rng.randint(1, 3) + 1)]
            if rng.random() < 0.3:
                vs.append(rng.choice(["1.5b1", "2.0b1", "1.0rc1", "4.0a1"]))
            if rng.random() < 0.15:
                vs.append("1.1")
        versions[name] = list(dict.fromkeys(vs))
    targets = names + ["ghost"]
    d = {}
    for name in names:
        if not versions[name]:
            continue
        d[name] = {}
        for v in versions[name]:
            d[name][v] = [[o, _spec(rng, versions.get(o, []))]
                          for o in (rng.choice(targets)        # self allowed
                                    for _ in range(rng.choice(
                                        [0, 0, 1, 1, 2, 3])))]
    roots = [o + _spec(rng, versions.get(o, []))
             for o in (rng.choice(targets) for _ in range(rng.randint(1, 4)))]
    if rng.random() < 0.3:
        roots.append(roots[0])                               # exact duplicate
    g = Graph.from_dict(d)
    p = Problem.from_requirements(g, roots)
    cons = list(p.constraints())
    disabled = set()
    if cons and rng.random() < 0.5:
        disabled = set(rng.sample(cons, rng.randint(1, min(3, len(cons)))))
    return Problem.from_requirements(g, roots, disabled)


def _check(problem):
    try:
        want = brute_force(problem, max_states=20_000)
    except OracleLimitExceeded:
        return None, []
    universe = set(problem.constraints())
    bad = []
    for cfg in CONFIGS:
        r = solve(problem, cfg)
        if r.status != want.status:
            bad.append((cfg, f"{r.status} vs oracle {want.status}"))
        elif r.status == "SAT":
            if check_plan(problem, r.plan):
                bad.append((cfg, "invalid plan"))
        else:
            conflict = set(r.conflict)
            if not conflict <= universe:
                bad.append((cfg, "conflict outside the constraint universe"))
            elif brute_force(problem.with_disabled(
                    set(problem.disabled) | (universe - conflict)),
                    max_states=200_000).status != "UNSAT":
                bad.append((cfg, "unsound conflict"))
    return want.status, bad


@pytest.mark.parametrize("block", range(6))
def test_every_config_agrees_with_the_oracle_on_edge_cases(block):
    statuses = []
    for seed in range(block * 100, block * 100 + 100):
        status, bad = _check(edge_problem(seed))
        assert not bad, (seed, bad[:3])
        if status:
            statuses.append(status)
    # the block is not degenerate: both answers occur
    assert statuses.count("SAT") >= 10 and statuses.count("UNSAT") >= 10


def test_the_generator_really_produces_the_edge_cases():
    seen = set()
    for seed in range(600):
        p = edge_problem(seed)
        cons = p.constraints() + p.roots
        specs = [c.spec for c in cons]
        if any(s.startswith("!=") and "b1" in s for s in specs):
            seen.add("neq_pre")
        if any(".*" in s for s in specs):
            seen.add("wildcard")
        if any(c.kind == "edge" and c.pkg == c.dep for c in cons):
            seen.add("self_dep")
        if len(p.roots) != len(set(p.roots)):
            seen.add("duplicate_root")
        if any(not p.graph.versions(c.dep) for c in p.roots):
            seen.add("root_without_releases")
        if p.disabled:
            seen.add("disabled")
    assert seen == {"neq_pre", "wildcard", "self_dep", "duplicate_root",
                    "root_without_releases", "disabled"}, seen


@pytest.mark.parametrize("cfg", CONFIGS, ids=lambda c: (
    f"ac3={int(c.ac3)}-mrv={int(c.mrv)}-lcv={int(c.lcv)}-"
    f"learn={int(c.learning)}-ng={int(c.nogoods)}"))
def test_the_two_reviewer_repros(cfg):
    # repro 1: excluding a pre-release does not make other pre-releases OK
    d = {"a": {"1.0": [], "1.5b1": []}}
    p = Problem.from_requirements(Graph.from_dict(d), ["a!=1.0b1", "a!=1.0"])
    assert solve(p, cfg).status == brute_force(p).status == "UNSAT"
    # repro 2: the conflict must stay UNSAT with everything else disabled
    d = {"a": {"1.0": [], "1.0rc1": []}}
    p = Problem.from_requirements(Graph.from_dict(d),
                                  ["a!=2.0b1", "a!=1.0", "a==1.0"])
    r = solve(p, cfg)
    assert r.status == brute_force(p).status == "UNSAT"
    rest = set(p.constraints()) - set(r.conflict)
    assert brute_force(p.with_disabled(rest)).status == "UNSAT"
