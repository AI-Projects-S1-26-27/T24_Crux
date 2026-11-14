"""Soundness under *partial* timeouts: whatever the oracle fails to decide,
the returned set must still be UNSAT and must not be called minimal.

The mutation check found that "QuickXplain treats a timeout as UNSAT"
survived the other tests, because only all-timeout runs were tested and the
final re-check in ``_quickxplain`` hid it.  These tests protect that re-check.
"""

import pytest

import crux.explain.core as core
from crux.explain import METHODS, extract_core
from crux.resolver.families import thrash, thrash_wide
from crux.resolver.gen import random_problem
from crux.resolver.oracle import brute_force


def _flaky(rule):
    real = core._Oracle.check

    def check(self, subset):
        if rule(list(subset)):
            self.calls += 1
            self.timeouts += 1
            return ("TIMEOUT", None)
        return real(self, subset)
    return check


def _oracle_unsat(p, subset):
    u = set(p.constraints())
    return brute_force(p.with_disabled(p.disabled | (u - set(subset)))
                       ).status == "UNSAT"


RULES = {
    "odd sizes": lambda s: len(s) % 2 == 1,
    "small sets": lambda s: len(s) <= 3,
    "large sets": lambda s: len(s) >= 4,
}


@pytest.mark.parametrize("rule", list(RULES))
@pytest.mark.parametrize("method", METHODS)
def test_partial_timeouts_never_give_a_satisfiable_core(monkeypatch, method,
                                                        rule):
    monkeypatch.setattr(core._Oracle, "check", _flaky(RULES[rule]))
    problems = [thrash(4).problem, thrash_wide(2).problem]
    problems += [p for p in map(random_problem, range(60))
                 if brute_force(p).status == "UNSAT"]
    for p in problems:
        start = list(p.constraints())
        r = extract_core(p, start, method=method, check_start=False)
        assert set(r.core) <= set(start)
        assert _oracle_unsat(p, r.core)
        if r.timeouts:
            assert not r.minimal


# ── an unsound resolver conflict must not corrupt deletion+refine ────

LIES = {
    "one constraint": lambda conflict, enabled: enabled[:1],
    "drop the first half": lambda conflict, enabled: enabled[len(enabled) // 2:],
    "drop roots": lambda conflict, enabled: [c for c in enabled
                                              if c.kind != "root"],
}


@pytest.mark.parametrize("lie", list(LIES))
def test_refinement_survives_an_unsound_conflict(monkeypatch, lie):
    """deletion+refine takes the resolver's UNSAT conflict on trust to drop
    several constraints at once.  If that conflict were unsound, the final
    re-check (and the rule that a refinement must keep every constraint kept
    so far) must still give a minimal core."""
    from crux.explain import verify_core
    from crux.resolver.problem import Result

    real = core.solve

    def lying(problem, config):
        r = real(problem, config)
        if r.status != "UNSAT":
            return r
        enabled = [c for c in problem.constraints()
                   if c not in problem.disabled]
        enabled.sort(key=lambda c: (c.kind != "root", str(c)))
        fake = LIES[lie](r.conflict, enabled) or enabled
        return Result("UNSAT", conflict=fake, stats=r.stats)

    monkeypatch.setattr(core, "solve", lying)
    problems = [thrash(4).problem, thrash_wide(2).problem]
    problems += [p for p in map(random_problem, range(80))
                 if brute_force(p).status == "UNSAT"]
    for p in problems:
        r = extract_core(p, list(p.constraints()), method="deletion+refine",
                         check_start=False)
        assert r.minimal
        v = verify_core(p, r.core)
        assert v.ok, (lie, v.unsat, v.redundant)
