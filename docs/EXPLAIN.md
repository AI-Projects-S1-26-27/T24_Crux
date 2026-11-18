# Crux core extraction: minimal unsatisfiable cores

What this component does, how to use it, how it works, and what it
guarantees. Component owner: **Yash Kejriwal** (proposal 4.2: "Core
extraction (deletion, QuickXplain), minimality verifier").

Related documents: `docs/context.md` (the resolver this builds on),
`docs/RENDER.md` (the message renderer built on top of it),
`docs/BENCHMARK.md` (the benchmark that scores it).

---

## 0. At a glance

| What | Where |
|---|---|
| Core extraction: deletion, deletion with refinement, QuickXplain | `crux/explain/core.py` |
| `explain(problem)`: solve, then extract a core on failure | `crux/explain/core.py` |
| Independent minimality verifier | `crux/explain/verify.py` |
| Benchmark of the methods (CLI) | `crux/explain/corebench.py` |
| Tests (98) | `tests/explain/test_core.py`, `test_verify.py`, `test_timeouts.py`, `test_corebench.py` |

**Key facts (details below).**
- Every core reported as minimal is minimal: confirmed by an independent
  verifier on every UNSAT instance among `random_problem` seeds 0 to 999 (705),
  on the 442 UNSAT instances among 600 edge-case seeds, in all 2,919 benchmark
  extractions of section 8, and on the team's planted and real corpora
  (section 8.4).
- Against the planted benchmark (198 conflicts, each with a proven unique
  minimal core) every method returns exactly the planted core: **198 of 198**.
- Known cores are found exactly: `toy_abc.json` (11 constraints), `thrash`
  (4 at any size), `thrash_wide` (8 at any size), derived real direct pins (3).
- A core that could not be proven minimal (a solver timeout or an exhausted
  time budget) is labelled `minimal=False`; it is never reported as minimal.
- **QuickXplain is not always cheaper than deletion.** It wins when the core is
  a small part of the starting set, and needs about twice deletion's calls when
  nearly every constraint is needed.
- Real cores can be large: the `boto3>=1.36.6 + botocore<=1.12.7` conflict
  has a 432-constraint core (2 roots and one edge for each of 430 boto3
  releases).

---

## 1. What it does

When a set of requirements cannot be installed together, the resolver
(`crux/resolver/`) answers UNSAT with a list of constraints that cannot all
hold (`Result.conflict`). That list is **sound but not minimal**: it can
contain constraints that have nothing to do with the conflict.

This component cuts it down to a **minimal unsatisfiable core** (proposal
3.4):

> A subset of constraints that is unsatisfiable on its own and becomes
> satisfiable if any one member is removed.

Every member of the core is needed for the conflict and nothing else is in
it, so it is exactly what an explanation should mention. The renderer
(`docs/RENDER.md`) turns it into a short English message.

Constraints are the resolver's `Constraint` objects (`crux/resolver/problem.py`):
a root requirement (`root requires flask>=3`) or one dependency edge
(`flask 3.0.0 requires werkzeug>=3`). They are hashable and are their own
identifiers.

Course link (proposal section 2): resolution-based refutation. "No solution"
means the constraint clauses are unsatisfiable; the core is a set of premises
that is still unsatisfiable and from which no premise can be dropped.

**Example** (`tests/fixtures/toy_abc.json`): requirements `a` and `b`; every
version of `a` (1.0 to 1.4) needs `c<2.0`, every version of `b` (1.0 to 1.3)
needs `c>=3.1`. The minimal core is 11 constraints: the two roots and all nine
edges to `c`. Each edge is necessary, because a version of `a` or `b` without
its edge would be a way out. The renderer groups the nine edges, and
`crux.explain.message` prints:

```
No solution. Your requirements a and b cannot both hold:
  every allowed version of a (1.0 to 1.4, 5 releases) needs c<2.0, but every allowed version of b (1.0 to 1.3, 4 releases) needs c>=3.1.
```

### Where it fits

```
requirements ─> resolver (solve) ─ SAT ─> install plan
                      │
                    UNSAT: conflict (sound, not minimal)
                      │
                      v
          core extraction (this component) ─> minimal core
                      │
                      v
          group.py / render.py (docs/RENDER.md) ─> English message ─> crux CLI
```

Callers: the renderer (`crux.explain.render.message` calls `explain`), the
CLI (`crux/cli.py`, through `message`), and the benchmark
(`crux/benchmark/score_cores.py`, `real_cores.py`, `crux_messages.py`).

---

## 2. Quick start

```bash
pip install -e ".[dev]"
python -m pytest tests/explain/test_core.py tests/explain/test_verify.py \
    tests/explain/test_timeouts.py tests/explain/test_corebench.py -q   # 98 tests
```

```python
from crux.data.graph import Graph
from crux.resolver import Problem
from crux.explain import explain, verify_core

g = Graph.from_dict({"a": {"1.0": [["c", "<2.0"]]},
                     "b": {"1.0": [["c", ">=3.1"]]},
                     "c": {"1.0": [], "3.1": []}})
p = Problem.from_requirements(g, ["a", "b"])
r = explain(p)                     # method="quickxplain" by default
print(r.status, r.minimal, r.calls)
for c in r.core:
    print(" ", c)
print(verify_core(p, r.core).ok)
```

```
UNSAT True 6
  root requires a
  root requires b
  a 1.0 requires c<2.0
  b 1.0 requires c>=3.1
True
```

Benchmark (results go to `results/`, which is gitignored):

```bash
# synthetic families (several minutes, mostly the random_csp cases)
python -m crux.explain.corebench --out results/core
# real conflicts derived from the pilot database (build it first: README)
python -m crux.explain.corebench --out results/core-real --no-synthetic \
    --db data/crux.db --budget 120 --skip conflict:boto3
# the large real boto3 conflict on its own (about 4 min)
python -m crux.explain.corebench --out results/core-boto3 --no-synthetic \
    --db data/crux.db --budget 600 --only conflict:boto3 --starts C4 \
    --methods deletion,quickxplain
```

`--starts` and `--methods` select a subset; `--only` and `--skip` filter
cases by name; `--no-verify` skips the verifier.

---

## 3. The API

`from crux.explain import explain, extract_core, verify_core, is_unsat, METHODS`

| Call | Returns | Use |
|---|---|---|
| `explain(problem, method="quickxplain", budget_s=None)` | `CoreResult` | **The entry point.** Solves; if UNSAT, extracts a core from the solver's conflict. For SAT or TIMEOUT: that status and an empty core. |
| `extract_core(problem, start=None, method=..., config=None, budget_s=None, check_start=True, universe=None)` | `CoreResult` | A core cut from a given UNSAT starting set (default: the conflict of `DEFAULT_SOLVE`, section 5.4). |
| `verify_core(problem, core, max_states=200_000, timeout_s=30)` | `CoreCheck` | Independent check: UNSAT on its own and every member necessary. |
| `is_unsat(problem, constraints)` | `"SAT"` / `"UNSAT"` / `"TIMEOUT"` | Status of the problem restricted to *constraints*. |
| `METHODS` | `("deletion", "deletion+refine", "quickxplain")` | |

`CoreResult` fields: `status` (the problem's status), `method`, `core` (tuple
of `Constraint`, roots first, then by text; deterministic), `minimal` (True
**only** when proven: no timeout, budget not exhausted), `start_size`, `calls`,
`sat_calls`, `unsat_calls`, `timeouts` (`calls` is their sum),
`budget_exhausted`, `wall_s`, `solver_s`, and `size`.

`CoreCheck` fields: `unsat` (True/False/None), `unsat_by` (`"oracle"`,
`"solvers"`, or `""`), `minimal` (True/False/None), `necessary_by`
(`{Constraint: "oracle" | "plan"}`), `redundant` (members that can be
dropped), `unknown` (undecided, timeouts), `errors`, and `ok`.

**Notes for callers.**
- A core always contains at least one root: with no roots there is nothing to
  install, which is trivially satisfiable.
- Group a core's edges by `(c.pkg, c.dep)` when presenting it, not by
  identical specifier: on the real boto3 conflict each of 430 releases has a
  different botocore range (section 8.3). `crux.explain.group` does this.
- Show `minimal=False` honestly ("could not prove this is the smallest set").
- To score against a known core, compare `set(r.core)` with it, or use
  `corebench.jaccard(a, b)`. Build expected cores with `Constraint.root(...)`
  and `Constraint.edge(...)`, which normalise names and specifiers. Exact match
  is the right test only when the expected core is the *unique* minimal core;
  otherwise any minimal core is a correct answer.
- On large real conflicts, prefer `method="deletion"` or `"quickxplain"` and
  pass a `budget_s` (for example 60); see section 5.5.

---

## 4. Files

```
crux/explain/core.py       extraction: deletion, deletion+refine, quickxplain, explain()
crux/explain/verify.py     independent verifier
crux/explain/corebench.py  benchmark (CLI)
tests/explain/test_core.py        extraction correctness, cost, timeouts, bad input
tests/explain/test_verify.py      the verifier rejects bad cores; fallback agrees with the oracle
tests/explain/test_timeouts.py    soundness under partial timeouts and unsound conflicts
tests/explain/test_corebench.py   the harness runs and scores known cores
docs/EXPLAIN.md            this document
```

`crux.explain` is registered in the explicit package list in `pyproject.toml`.
The component only calls the resolver (`solve`, the oracle and the plan
checker); it does not modify resolver or data code. The renderer's files in
the same package (`group.py`, `render.py`, `ideal.py`) are documented in
`docs/RENDER.md`.

---

## 5. How it works

### 5.1 The oracle: `solve` with disabled constraints

"Subset S is UNSAT" is decided by `solve(problem.with_disabled(U - S))`, where
**U = `problem.constraints()` computed once** at the start. A disabled
constraint is ignored totally by every solver and the checker: it need not
hold and it does not activate its dependency (`problem.py`). The caller's own
`problem.disabled` is always kept disabled.

U is computed once because disabling edges shrinks the static closure:
`problem.with_disabled(...).constraints()` is a *smaller* set, and recomputing
U per call would leave constraints outside the new closure enabled.

### 5.2 Why the algorithms are correct: monotonicity

If S is a subset of S' and S' has a plan P, then P cut down to the packages
reachable from S's roots through S's edges is a plan for S: every
S-constraint it walks is an S'-constraint that P satisfies, and the cut-down
plan has exactly the S-active packages. So **adding constraints never turns
UNSAT into SAT**, and removing them never turns SAT into UNSAT. Every method
below depends on this; `test_unsatisfiability_is_monotone` checks it against
the brute-force oracle.

### 5.3 The three methods

- **deletion.** Walk the starting set; drop constraint c; if the rest is still
  UNSAT, c goes, otherwise it stays. One solver call per starting constraint.
  Minimal by construction: c was kept because `core_then - {c}` is SAT; the
  final core is a subset of `core_then`, so `final - {c}` is SAT by
  monotonicity.
- **deletion+refine** (clause-set refinement, Liffiton & Sakallah 2008). As
  deletion, but when a trial is UNSAT, the solver's own conflict for that
  trial (usually much smaller) replaces the working set, so several
  constraints go per call. Because this relies on the resolver's conflict
  being UNSAT on its own, two safeguards make the result independent of it:
  (a) a refinement is accepted only if it keeps every constraint kept so far
  (a sound conflict always does: a smaller UNSAT set without a kept c would
  contradict the SAT call that kept c), so no constraint is left untested;
  (b) whenever a refinement happened, the final core is checked once more; if
  it is SAT, plain deletion is rerun from the start, and if the check times
  out the start is returned, unproven. That extra call is made only when
  refinement has already saved at least one, so refine never costs more calls
  than plain deletion.
- **quickxplain** (Junker 2004). `qx(B, D, C)`: if D is non-empty and B is
  already UNSAT, return nothing; if C has one element, return it; otherwise
  split C into halves C1, C2, compute `D2 = qx(B + C1, C1, C2)` and
  `D1 = qx(B + D2, D2, C1)`, return `D1 + D2`. For a core of k out of n it
  needs about 2k log2(n/k) + 2k calls; with k close to n it needs about 2n.

Different methods, and different orders of the same starting set, can return
different minimal cores when the problem has several. Each is minimal. Output
is deterministic for a given input.

### 5.4 Which conflict to start from

Any UNSAT starting set works; minimality is relative to the problem, not the
start. The start only changes the cost. The default (`DEFAULT_SOLVE`) is the
conflict of **learning + MRV without AC-3**. Measured on the 215 UNSAT
instances among `random_problem` seeds 0 to 299:

| Starting conflict | mean size | median |
|---|---|---|
| C0 (plain backtracking: roots + every edge tried) | 5.6 | 3 |
| C1 (AC-3) | 10.3 | 10 |
| C4 preset (AC-3 + MRV + LCV + learning) | 10.28 | 10 |
| **learning + MRV, no AC-3 (the default start; identical with LCV)** | **1.86** | **1** |
| learning only | 2.04 | 1 |
| true minimal core | 1.79 | 1 |

The C4 preset's conflict is large because, when AC-3 deleted a version the
refutation relied on, `learning.py` (`_explain`) adds **all** of AC-3's
justifying edges ("coarse but sound"). `docs/context.md` 5.7 quotes "C4 2.0 vs
C0 5.5"; that figure is for learning alone (`Config(learning=True)`).

### 5.5 Which configuration answers the oracle calls

Every configuration gives the same SAT/UNSAT, so the choice is about speed,
and about conflict tightness where the conflict is used:

- **deletion, quickxplain** use only the status: `DEFAULT_ORACLE` = **AC-3 +
  MRV + learning**, no LCV.
- **deletion+refine** uses the conflict: `REFINE_ORACLE` = learning + MRV
  **without** AC-3 (tight conflicts, section 5.4).

Measured per solver call (warm Graph caches):

| Oracle configuration | real boto3/botocore conflict, s per call | `thrash_wide(9)` from the universe, s per call |
|---|---|---|
| C0 | 0.053 | - |
| AC-3 (C1) | 0.085 | - |
| AC-3 + MRV | 0.084 to 0.098 | 0.63 |
| MRV only | 0.099 | - |
| learning + MRV (no AC-3) | 8.3 | - |
| **AC-3 + MRV + learning** | **0.101** | **0.0007** |

On `random_csp(16, seed 9)` from the universe, QuickXplain with AC-3 + MRV
hit three per-call timeouts and its budget; with AC-3 + MRV + learning it
finished in 1.6 s. Each part of the default covers a case where the others are
slow:
- learning without AC-3 is about 100x slower on boto3;
- AC-3 + MRV without learning is exponential on `thrash_wide` subsets that
  still contain the free packages (only backjumping fixes that family,
  `docs/context.md` 6.2);
- QuickXplain's intermediate subsets (about half the constraints of a
  near-hard random CSP) can be hard instances in their own right.

Learning without AC-3 is slow on boto3 because `learning.py` `_explain` loops
over every version of the dead-end package (about 2,400 `botocore` releases)
and, per excluded version, builds `Constraint.specifier` (a new
`SpecifierSet` each time) before calling `Graph.allowed()`. A profile of 22
calls puts 98% of the time there; the exact cause within `_explain` has not
been isolated. With AC-3 on, AC-3 removes the impossible boto3 releases before
search, so `_explain` rarely runs.

**Consequences.** On boto3-sized conflicts `deletion+refine` is slow (it needs
learning without AC-3), so use `deletion` or `quickxplain` there. The first
solve in `explain()` (learning without AC-3, for the tight starting conflict)
costs about 24 s cold on that case.

### 5.6 Timeouts, the budget, and what is guaranteed

- A solver call that times out is **unknown**. Deletion keeps the constraint
  under test (keeping is always sound for unsatisfiability). QuickXplain treats
  unknown as "not UNSAT", so it only drops constraints after a proven UNSAT;
  by induction every call returns a set that is UNSAT with its background. As
  a second safeguard, when any call timed out it re-checks its final answer
  and falls back to the start if that is not shown UNSAT.
- `budget_s` limits the whole extraction. When it runs out, deletion returns
  its current working set (UNSAT, because every set it moved to was shown
  UNSAT), or the start if an unchecked refinement shaped that set; QuickXplain
  returns the start.
- In every such case `minimal=False`. **A returned core is always UNSAT, and
  it is called minimal only when that was proven.**

### 5.7 The verifier

`verify.py` shares no code with `core.py`. For a candidate core K it decides
(1) K is UNSAT and (2) for every c, K - {c} is SAT, using, strongest first:

| Verdict | How |
|---|---|
| `oracle` | the brute-force oracle (`resolver/oracle.py`, which shares no code with any solver) when the restricted problem's search space is at most `max_states` (200,000). Restricting to K usually makes the closure tiny. |
| `plan` (SAT only) | plain backtracking (C0) returns a plan **and the independent checker `check_plan` accepts it**. A checked plan is a certificate; no trust in the solver is needed. |
| `solvers` (UNSAT only) | C0 (plain backtracking) and C4 without LCV (AC-3 + MRV + learning), two different search algorithms, both say UNSAT. The second is also the extractor's default oracle, so the verdict's independence rests on C0. The weakest verdict, labelled as such: UNSAT has no certificate short of exhaustive search. |

A verdict that cannot be reached (a timeout) is `None`, never a guess. A
solver plan the checker rejects is recorded in `errors` and gives no verdict.

Whether the oracle is usable is decided by `_small_enough`, which computes the
same number as `oracle.search_space` (the product of release counts over the
static closure) but stops at the bound and skips the oracle's admissibility
tables, which take seconds per call on real boto3-sized problems. A test checks
the two agree on 300 random and edge-case instances.

---

## 6. Tests

All offline, using `Graph.from_dict`, the resolver's generators and
`tests/fixtures/`. What they check:

- **Known cores**: `toy_abc` (11); `thrash` (2 roots + 2 edges) at n = 1, 3, 6,
  from the default start and from the whole universe; `thrash_wide` (8) at
  n = 1, 4, 8; a root on a missing package or with an empty range is a core of
  one.
- **Random instances**: every UNSAT case of `random_problem` seeds 0 to 999
  (705), every method, verified by the oracle; from C0, C4 and universe starts
  on seeds 0 to 199; shuffled starts.
- **Edge cases**: the generator of `tests/resolver/test_edge_fuzz.py`
  (pre-release-only packages, `!=` against pre-releases, self dependencies,
  duplicate roots, packages without releases, caller-disabled constraints);
  the core never contains a disabled constraint.
- **Enumeration**: on tiny instances (universe of at most 10), every subset is
  checked with the oracle and the minimal UNSAT subsets listed; the extracted
  core must be one of them.
- **Monotonicity** against the oracle on random pairs S of S'.
- **Cost**: deletion makes exactly one call per starting constraint;
  refinement never more; QuickXplain needs 16 calls for a core of 4 among 44
  (deletion 44).
- **Timeouts and budget**: all calls timing out, a zero budget, a budget that
  runs out mid-way, partial timeouts by three different rules.
- **Unsound resolver conflicts**: `deletion+refine` fed conflicts that are
  wrong in three different ways still returns verified minimal cores.
- **Verifier**: redundant members are named, SAT sets rejected, the fallback
  (no oracle) agrees with the oracle on 200 instances, a solver plan is
  checked rather than trusted, timeouts give "unknown".

**Mutation check.** The tests were run against 16 deliberately faulty versions
of `core.py` and `verify.py`: 13 are detected, and the other 3 are equivalent
(still correct) because each of them removes one of two redundant safeguards:

| Fault | Result |
|---|---|
| deletion keeps a removable constraint | detected |
| deletion drops a needed constraint | detected |
| QuickXplain forgets D2 in the background | detected |
| QuickXplain forgets C1 in the background | detected |
| deletion calls a timed-out run minimal | detected |
| refinement keeps the removed constraint | detected |
| the oracle ignores the caller's disabled set | detected |
| the oracle recomputes the universe per call | detected |
| the verifier never removes the member | detected |
| the verifier ignores redundant members | detected |
| the verifier trusts the solver's plan unchecked | detected |
| refinement without the final re-check | detected |
| QuickXplain's two timeout safeguards both removed | detected |
| QuickXplain treats a timeout as UNSAT (re-check still present) | equivalent |
| QuickXplain without the final re-check (timeout handling still present) | equivalent |
| refinement without the "keep every kept constraint" rule (re-check still present) | equivalent |

---

## 7. Results: how much smaller the core is

From the synthetic benchmark (section 8.1), median constraints in the
conflict each configuration returns, and in the minimal core:

| group | cases | universe | C0 | C4 preset | default start | minimal core |
|---|---|---|---|---|---|---|
| random | 215 | 27 | 3 | 10 | 1 | 1 |
| thrash | 4 | 18.5 | 10 | 4 | 4 | 4 |
| thrash_wide | 2 | 14 | 14 | 8 | 8 | 8 |
| wide_pair | 3 | 42 | 42 | 42 | 42 | 42 |
| random_csp | 4 | 126 | 108.5 | 53 | 53 | 21.5 |
| toy | 1 | 11 | 11 | 11 | 11 | 11 |

Median C0 conflict / minimal core: 2.25x (max 19x). 47 of 229 cases have
minimal cores of different sizes depending on method or start: "minimal" does
not mean "smallest".

---

## 8. Results: correctness and cost of the methods

Measured on one Windows laptop, Python 3.13.5, packaging 26.0. **Solver calls
are the cost measure**; wall times are secondary. Medians, not sums.

### 8.1 Synthetic families (`python -m crux.explain.corebench --out results/core --seeds 300 --csp-seeds 10`)

229 UNSAT cases: the 215 UNSAT instances among `random_problem` seeds 0 to 299,
`thrash` n = 3, 9, 20, 40, `thrash_wide` n = 3, 9, `wide_pair` k = 5, 20, 50
(`thrash_wide(0, k)`: a core of 2 + 2k that is almost the whole universe),
4 UNSAT `random_csp` instances (sizes 12 and 16, seeds 0 to 9), `toy_abc`.
Each case from up to four starting sets x three methods = **2,742 extractions**.

- **Correctness:** 2,742 of 2,742 proven minimal; **2,742 of 2,742 pass the
  independent verifier** (2,714 UNSAT verdicts by the oracle, 28 by two
  solvers). **Known cores: exact match 114 of 114.**

Cost, solver calls (median):

| group | start | start size | core | deletion | deletion+refine | quickxplain |
|---|---|---|---|---|---|---|
| random | default | 1 | 1 | 1 | 1 | 0 |
| random | C4 preset | 10 | 1 | 10 | 3 | 4 |
| random | universe | 27 | 1 | 27 | 3 | 5 |
| thrash | universe | 18.5 | 4 | 18.5 | 6 | 12 |
| thrash_wide | universe | 14 | 8 | 14 | 10 | 18 |
| wide_pair | default | 42 | 42 | 42 | 42 | 82 |
| random_csp | default | 53 | 21.5 | 53 | 34.5 | 74 |
| random_csp | universe | 126 | 26 | 126 | 39 | 134 |
| toy | default | 11 | 11 | 11 | 11 | 20 |

QuickXplain against deletion, per case (fewer calls): `random` from the
universe 207 QuickXplain / 5 deletion / 3 ties; from the default start
142 / 46 / 27 (many of QuickXplain's wins there are a 1-constraint start, which
it returns with 0 calls and deletion checks with 1); on `thrash_wide`,
`wide_pair`, `toy`, and `random_csp` from the default start, deletion wins
every case. (`thrash` from C0 has 2 cases, not 4: C0 itself times out on
n = 20 and 40.)

**Reading.**
1. From the default start the starting set is already close to minimal, so
   QuickXplain's divide and conquer has little to discard and pays about 2
   calls per core member.
2. QuickXplain wins clearly only when the core is a small fraction of a large
   start (random from the universe: 5 calls vs 27).
3. deletion+refine is the cheapest or tied in every row, because each UNSAT
   trial's learned conflict removes many constraints at once; but its oracle is
   learning without AC-3, which is slow on boto3-scale data (5.5).

### 8.2 Real conflicts derived from the pilot database

Command: `python -m crux.explain.corebench --out results/core-real --no-synthetic --db data/crux.db --budget 120 --skip conflict:boto3`

19 UNSAT cases built from real metadata by `corebench.real_cases`: the newest
release of a package plus a direct or transitive dependency pinned to its
oldest release, and "ranged" variants with the package at `>=` its 6th newest
release. These are constructed test cases, not user-reported failures (those
are in section 8.4). 177 extractions.

- **Correctness:** 175 of 177 proven minimal; **175 of 175 pass the verifier**
  (141 oracle verdicts, 36 by two solvers). The 2 unproven are the C4-preset
  start with deletion on the two `boto3 ... + urllib3==1.11` transitive cases,
  each with one 10 s per-call timeout; they are labelled `minimal=False`, and
  the verifier confirms they are not minimal. **Known cores (direct pins):
  exact match 84 of 84.**
- Core sizes: 3 for a direct pin (two roots and the edge), 4 for a transitive
  pin (the chain), 8 or 14 for ranged variants (one edge per release in the
  range).
- Conflict sizes (medians): universe 458, C0 8, C4 preset 8, default start 4,
  minimal core 4.
- Calls (median): from the default start deletion 4, deletion+refine 4,
  QuickXplain 6; from C0 8 / 6 / 11; from the universe (2 cases with at most
  300 constraints) 275 / 7.5 / 26.5.

### 8.3 The real boto3/botocore conflict

`boto3>=1.36.6` + `botocore<=1.12.7` (the real UNSAT pair from
`crux/resolver/realbench.py`). Command in section 2.

| | start | core | calls | wall | minimal | verifier |
|---|---|---|---|---|---|---|
| deletion | C4 preset, 480 | 432 | 480 (432 SAT, 48 UNSAT) | 52 s | yes | pass (UNSAT by two solvers; each member necessary by oracle or checked plan) |
| quickxplain | C4 preset, 480 | 432 | 866 (862 SAT, 4 UNSAT) | 94 s | yes | pass |

- The core is **2 roots and one edge for each of the 430 boto3 releases
  1.36.6 to 1.43.123**, each requiring a botocore range whose lower bound is
  above 1.12.7. Every release is a separate way out, so every edge is
  necessary. This is QuickXplain's worst case (k close to n): 1.8x deletion's
  calls.
- Conflict sizes: universe 14,264; C0 1,292; C4 preset 480. The default start
  (learning without AC-3) needs about 24 s here, more than the benchmark's 10 s
  per-call limit, so this run starts from the C4 preset. `explain()`, with its
  60 s first solve, gets there more slowly.
- Each of the 430 edges has a *different* specifier
  (`botocore<1.44.0,>=1.43.123`, `...>=1.43.122`, ...), so grouping by identical
  specifier compresses nothing; group by (package, dependency) and summarise
  the bounds.
- Verifying this core takes about 2 minutes (432 checks).

### 8.4 On the team's benchmark corpora

Scored by `crux/benchmark/score_cores.py` and `real_cores.py`; full tables in
`benchmarks/results/REPORT.md`, method in `docs/BENCHMARK.md`.

- **Planted conflicts** (198, each with a proven unique minimal core): every
  method returns exactly the planted core on **198 of 198**, from the solver's
  conflict and from every constraint of the instance; all 198 are proven
  minimal, with no solver timeouts. From every constraint (median start 100.5),
  median calls are deletion 102, deletion+refine 11, QuickXplain 35.
- **Real reproduced failures** (61): all three methods are proven minimal and
  return the same core on 61 of 61; the verifier confirms 61 of 61 (60 by the
  oracle, 1 by two solvers). Median core size 3 (max 85).

---

## 9. Against the proposal's targets

| Measure (proposal 3.6) | Target | Result |
|---|---|---|
| Core minimality | 100% of cores minimal on oracle re-check | **Met**: every core reported minimal passes the independent verifier (sections 8.1 to 8.4). Cores that could not be proven are labelled `minimal=False`. |
| Core accuracy, planted | exact match on at least 95% of planted cores | **Met**: 198 of 198 (100%) for every method and both starts (8.4). |
| Core accuracy, real | mean Jaccard at least 0.8 against hand-annotated cores | Not measured as stated: there are no hand-annotated cores. Instead (8.4) every real core is verified minimal, and the benchmark records whether it is the only minimal core (36 of 61) or one of several (25). |
| Deletion vs QuickXplain cost | compare | **Done** (8.1 to 8.3): QuickXplain wins only when the core is a small part of the start; from the solver's tight conflict deletion is cheaper, and deletion+refine is cheapest or tied. |
| Time limit on large inputs (risk table) | per-case time limit, report cases that hit it | **Done**: `budget_s` and per-call timeouts; unproven cores are reported (8.2). |

---

## 10. Limitations and possible extensions

- **Cost on large cores.** Every method needs at least one solver call per
  core member (432 on the boto3 conflict). **Group-level deletion** (try
  removing all edges of one package at once before single constraints) would
  cut this; not implemented.
- **Cores from learned nogoods.** Extracting a core directly from the
  resolver's learned nogoods instead of re-solving (proposal 3.4, "extension
  if time allows") is not implemented.
- **The default start on boto3-sized problems** costs about 24 s (5.5).
- **The C4 preset's conflict** includes all of AC-3's justifications (5.4). A
  tighter one would need the AC-3 branch of `learning.py` `_explain` to add
  only the edges behind the versions the refutation relied on.
- **Minimal, not minimum.** The core is minimal (no member can be dropped),
  not necessarily the smallest possible; finding the smallest needs all
  minimal cores.
- The verifier's `solvers` verdict for UNSAT on large problems is agreement
  between two algorithms, not a certificate.

---

## 11. Frequently asked questions

- *What is a minimal unsatisfiable core, and how is it different from a
  minimum one?* Minimal: no single member can be dropped. Minimum: the
  smallest possible size, which is much harder to find (it needs all minimal
  cores). The component promises minimal, which is what the proposal states.
- *Why is deletion correct?* Monotonicity (5.2) and the argument in 5.3.
- *Why does QuickXplain use fewer calls, and when not?* Divide and conquer
  discards whole halves at once when they are irrelevant; when the core is
  most of the set, every half is needed, so it pays about 2 calls per member.
- *Why is the universe computed once?* 5.1.
- *What happens on a timeout?* 5.6: the core stays UNSAT and is never reported
  as minimal.
- *How is minimality confirmed?* By an independent verifier that shares no
  code with the extractor, using the brute-force oracle or checked plans (5.7),
  plus enumeration of all minimal cores on tiny instances and a mutation check
  of the tests (6).
- *Why not start from the C4 preset's conflict?* 5.4.
- *Why is the toy core 11 constraints when the message is two lines?* Each
  version of `a` and `b` is a separate edge, and each must be in the core, or
  that version would be a way out. The renderer groups them.
