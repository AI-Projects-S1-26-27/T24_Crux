# Crux explanation: minimal unsatisfiable cores

Working context for core extraction and the minimality verifier: what exists,
why it is built the way it is, what was measured, and what the next people
(Parth Gupta's renderer, Arrya's benchmark, Sumehra's CLI, the report) need from
it. **When you change something here, update section 9 (open items) and section
12 (change log).**

Owner: **Yash Kejriwal** (proposal 4.2: "Core extraction (deletion,
QuickXplain), minimality verifier"). Built 2026-11-15 on branch
`yash/core-extraction`, starting from `main` at `70d730b`.

---

## 0. At a glance

| What | Where | State |
|---|---|---|
| Deletion-based core extraction | `crux/explain/core.py` | done, tested |
| Deletion with clause-set refinement | `crux/explain/core.py` | done, tested |
| QuickXplain | `crux/explain/core.py` | done, tested |
| `explain(problem)` entry point for renderer / CLI | `crux/explain/core.py` | done |
| Independent minimality verifier | `crux/explain/verify.py` | done, tested |
| Benchmark: methods, starting sets, conflict sizes, real data | `crux/explain/corebench.py` | done; results in section 7 (`--starts`, `--methods`, `--only`, `--skip` select a subset) |
| Tests | `tests/explain/` (4 files) | 98 tests, mutation-checked |

**Headline facts (details and caveats below).**
- Every core the code calls minimal was verified minimal independently: on
  every UNSAT instance among `random_problem` seeds 0 to 999 (705), on the 442
  UNSAT instances among 600 edge-case seeds, and in all 2,919 benchmark extractions that claimed minimality
  (2,742 synthetic, 175 real derived, 2 on the real boto3 conflict; section 7).
  The 2 that could not be proven (timeouts) were labelled so, and the verifier
  confirmed they were not minimal.
- Known cores are found exactly: `toy_abc.json` (11 constraints), `thrash`
  (4 at any size), `thrash_wide` (8 at any size), derived real direct
  conflicts (3).
- **The C4 preset's conflict is not a good starting point.** With AC-3 on, the
  learning search adds every AC-3 justification to its conflict: mean 10.3
  constraints against 5.6 for plain C0 and a true minimal core of 1.79. Learning
  *without* AC-3 gives 1.86. `docs/context.md` 5.7's "C4 2.0 vs C0 5.5" was
  measured with learning alone (`Config(learning=True)`), not the preset.
- **QuickXplain is not always cheaper than deletion.** It wins when the core is
  a small part of the starting set and loses (about 2x the calls) when nearly
  every constraint is needed, which is the common case once you start from a
  tight solver conflict.
- On real PyPI conflicts the core can be **large**: the realbench
  `boto3>=1.36.6 + botocore<=1.12.7` conflict has a 432-constraint core (2 roots
  and one edge for each of 430 boto3 releases, all with different botocore
  ranges); deletion needs 480 calls (52 s), QuickXplain 866 (94 s). The renderer
  has to summarise it by package (section 7.3).
- Full test suite after this work: **1,200 passing** (1,102 before + 98 new).

---

## 1. What this component is

The resolver (`crux/resolver/`) returns, for an unsatisfiable problem, a list
of constraints that cannot all hold (`Result.conflict`). It is **sound but not
minimal**. This component cuts it down to a **minimal unsatisfiable core**
(proposal 3.4):

> A subset of constraints that is unsatisfiable on its own and becomes
> satisfiable if any one member is removed.

The core is what Parth Gupta's renderer turns into two lines of English: every
member is needed for the conflict and nothing else is.

Course link (proposal section 2): resolution-based refutation. "No solution"
means the constraint clauses are unsatisfiable; the core is a set of premises
that is still unsatisfiable and from which no premise can be dropped.

Constraints are the resolver's own `Constraint` objects (`problem.py`): a root
requirement (`root requires flask>=3`) or one dependency edge
(`flask 3.0.0 requires werkzeug>=3`). They are hashable and are their own
identifiers.

---

## 2. Running it

```bash
pip install -e ".[dev]"
python -m pytest tests/explain -q                     # 98 tests, about 2 min

# the benchmark (synthetic only; several minutes, mostly the random_csp cases)
python -m crux.explain.corebench --out results/core
# with real data (build the pilot first: see README / docs/context.md)
python -m crux.explain.corebench --out results/core-real --no-synthetic --db data/crux.db --budget 120 --skip conflict:boto3
# the large real boto3 conflict on its own (about 4 min; section 7.3)
python -m crux.explain.corebench --out results/core-boto3 --no-synthetic --db data/crux.db --budget 600 --only conflict:boto3 --starts C4 --methods deletion,quickxplain
```

`results/` is gitignored. Copy the numbers you quote into the report.

Quick use:

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

---

## 3. The API (what other components call)

`from crux.explain import explain, extract_core, verify_core, is_unsat, METHODS`

| Call | Returns | Use |
|---|---|---|
| `explain(problem, method="quickxplain", budget_s=None)` | `CoreResult` | **Renderer and CLI entry point.** Solves; if UNSAT, extracts a core from the solver's conflict. For SAT/TIMEOUT: that status and an empty core. |
| `extract_core(problem, start=None, method=..., config=None, budget_s=None, check_start=True, universe=None)` | `CoreResult` | Core from a given UNSAT starting set (default: the learning conflict). |
| `verify_core(problem, core, max_states=200_000, timeout_s=30)` | `CoreCheck` | Independent check: UNSAT on its own and every member necessary. |
| `is_unsat(problem, constraints)` | `"SAT"` / `"UNSAT"` / `"TIMEOUT"` | Status of the problem restricted to *constraints*. |
| `METHODS` | `("deletion", "deletion+refine", "quickxplain")` | |

`CoreResult` fields: `status` (the problem's status), `method`, `core` (tuple
of `Constraint`, roots first, then by text; deterministic), `minimal` (True
**only** when proven: no timeout, budget not exhausted), `start_size`, `calls`,
`sat_calls`, `unsat_calls`, `timeouts`, `budget_exhausted`, `wall_s`,
`solver_s`, and `size`.

`CoreCheck` fields: `unsat` (True/False/None), `unsat_by` (`"oracle"`,
`"solvers"`, or `""`), `minimal` (True/False/None), `necessary_by`
(`{Constraint: "oracle" | "plan"}`), `redundant` (members that can be
dropped), `unknown` (undecided, timeouts), `errors`, and `ok`.

**For the renderer (Parth Gupta).** Group the core's edges by `(c.pkg, c.dep)`:
all releases of one package that constrain the same dependency become "every
allowed version of A needs C<2.0". Do not group by identical specifier only: on
the real boto3 conflict each of 430 releases has a different botocore range, so
summarise the bounds instead (7.3). Roots name what
the user can change. A core always contains at least one root (with no roots
there is nothing to install, which is trivially satisfiable). Show
`minimal=False` honestly ("could not prove this is the smallest set"). The core
does **not** say why a plausible version is missing from the graph (`no_wheel`,
`yanked`, `py_version`); that is in the data layer's `drops` table and is not
reachable through `Graph` (`docs/HANDOVER.md`).

**For the benchmark (Arrya).** Core accuracy against planted cores: compare
`set(r.core)` with the planted constraint set (`exact`), or use
`corebench.jaccard(a, b)` for the hand-annotated real cases. Planted cores
should be specified as `Constraint` objects built with `Constraint.root(...)` and
`Constraint.edge(...)`, which normalise names and specifiers. Note that a
"planted core" must be *minimal* for exact match to be the right test: if the
planted conflict has several minimal cores, any of them is a correct answer.

---

## 4. Design

### 4.1 The oracle: `solve` with disabled constraints

"Subset S is UNSAT" is decided by `solve(problem.with_disabled(U - S))`, where
**U = `problem.constraints()` computed once** at the start. A disabled
constraint is ignored totally by every solver and the checker: it need not
hold and it does not activate its dependency (`problem.py`). The caller's own
`problem.disabled` is always kept disabled.

Why U is computed once: disabling edges shrinks the static closure, so
`problem.with_disabled(...).constraints()` is a *smaller* set. Recomputing U per
call would leave constraints outside the new closure enabled. (A mutant that
did this was caught, section 6.)

### 4.2 Why the algorithms are correct: monotonicity

If S is a subset of S' and S' has a plan P, then P cut down to the packages
reachable from S's roots through S's edges is a plan for S: every
S-constraint it walks is an S'-constraint that P satisfies, and the cut-down
plan has exactly the S-active packages. So **adding constraints never turns
UNSAT into SAT**. Every step below depends on this, and
`test_unsatisfiability_is_monotone` checks it against the brute-force oracle.

### 4.3 Methods

- **deletion.** Walk the starting set; drop constraint c; if the rest is still
  UNSAT, c goes, otherwise it stays. One solver call per starting constraint.
  Minimal by construction: c was kept because `core_then - {c}` is SAT; the
  final core is a subset of `core_then`, so `final - {c}` is SAT by monotonicity.
- **deletion+refine** (clause-set refinement, Liffiton & Sakallah 2008). As
  deletion, but when a trial is UNSAT the solver's own conflict for that trial
  (sound, usually smaller) replaces the working set, so several constraints go
  per call. A refinement takes the resolver's word that its conflict is UNSAT
  on its own, so two safeguards make the result independent of that word:
  (a) a refinement is accepted only if it keeps every constraint kept so far
  (a sound conflict always does: a smaller UNSAT set without a kept c would
  contradict the SAT call that kept c), so no constraint is left untested;
  (b) when any refinement happened, the final core is checked once more; if it
  is SAT, plain deletion is rerun from the start, and if the check times out
  the start is returned, unproven. Each kept constraint was shown necessary
  against a superset of the final core, so with (b) the proof holds whatever
  the resolver returns. The extra call is made only when refinement has
  already saved at least one, so refine never costs more calls than plain
  deletion (tested). Added after a review on 2026-11-15; before it, an unsound
  resolver conflict could have produced a satisfiable "core".
- **quickxplain** (Junker 2004). `qx(B, D, C)`: if D is non-empty and B is
  already UNSAT, return nothing; if C has one element, return it; otherwise
  split C into halves C1, C2, compute `D2 = qx(B + C1, C1, C2)` and
  `D1 = qx(B + D2, D2, C1)`, return `D1 + D2`. For a core of k out of n it
  needs about 2k log2(n/k) + 2k calls; with k close to n it needs about 2n.

Different methods, and different orders of the same starting set, can return
different minimal cores when the problem has several. Each is minimal. Output is
deterministic for a given input (tested).

### 4.4 Which conflict to start from

Any UNSAT starting set works; minimality is relative to the problem, not the
start. The start only changes the cost. The default (`DEFAULT_SOLVE`) is the
conflict of **learning + MRV without AC-3**, because measured on the 215 UNSAT
instances among `random_problem` seeds 0 to 299:

| Starting conflict | mean size | median |
|---|---|---|
| C0 (plain backtracking: roots + every edge tried) | 5.6 | 3 |
| C1 (AC-3) | 10.3 | 10 |
| C4 preset (AC-3 + MRV + LCV + learning) | 10.28 | 10 |
| **learning + MRV, no AC-3 (the default start; identical with LCV)** | 1.86 | 1 |
| learning only | 2.04 | 1 |
| **true minimal core** | **1.79** | **1** |

The cause is in `learning.py` (`_explain`): when AC-3 deleted a version that
the refutation relied on, it adds **all** of AC-3's `used` edges (documented
there as "coarse but sound"). This is not a bug, and I did not change the
resolver; `docs/context.md` 5.7 was clarified to say which configuration its
"2.0 vs 5.5" figure refers to.

### 4.5 Which configuration answers the oracle calls

Every configuration gives the same SAT/UNSAT, so this is about speed, and
about conflict tightness where the conflict is used.

- **deletion, quickxplain** use only the status: `DEFAULT_ORACLE` = **AC-3 +
  MRV + learning**, no LCV.
- **deletion+refine** uses the conflict: `REFINE_ORACLE` = learning + MRV
  **without** AC-3 (tight conflicts, section 4.4).

How the default was chosen (it changed twice during the work; all measured on
this machine, warm Graph caches):

| Oracle configuration | real boto3/botocore, s per call | `thrash_wide(9)` universe start, s per call |
|---|---|---|
| C0 | 0.053 | - |
| AC-3 (C1) | 0.085 | - |
| AC-3 + MRV | 0.084 to 0.098 | 0.63 |
| MRV only | 0.099 | - |
| learning + MRV (no AC-3) | 8.3 | - |
| **AC-3 + MRV + learning** | **0.101** | **0.0007** |

On `random_csp(16, seed 9)` from the universe start, QuickXplain with AC-3 + MRV
hit three per-call timeouts and its budget; with AC-3 + MRV + learning it
finished in 1.6 s. Each part of the default covers a case where the others are
slow: learning without AC-3 is about 100x slower on boto3; AC-3 + MRV without
learning is exponential on `thrash_wide` trials that still contain the free
packages (only C4's backjumping fixes that family, `docs/context.md` 6.2), and
QuickXplain's intermediate subsets (about half the constraints of a near-hard
random CSP) can be hard instances in their own right.

Why learning without AC-3 is slow on boto3: a profile of 22 calls shows 98% of
the time in `learning.py` `_explain`, which loops over every version of the
dead-end package (about 2,400 `botocore` releases) and, per excluded version,
builds `Constraint.specifier` (a property that constructs a new `SpecifierSet`
each time, `problem.py`) before calling `Graph.allowed()`: 4.5 million
`SpecifierSet` constructions. An in-memory experiment passing the specifier
string instead gave no speed-up, but I did not confirm that patch actually took
effect, so the cause within `_explain` is **not settled**. With AC-3 on, AC-3
removes the impossible boto3 releases before search, so `_explain` rarely runs.
I did not change the resolver (section 9).

**Practical consequences.** On boto3-sized conflicts, `deletion+refine` is
slow (it needs learning without AC-3); use `deletion` or `quickxplain`. The
first solve in `explain()` (learning without AC-3, for the tight starting
conflict) costs about 24 s cold on that case.

### 4.6 Timeouts and the budget

- A solver call that times out is **unknown**. Deletion keeps the constraint
  under test (keeping is always sound for unsatisfiability). QuickXplain
  treats unknown as "not UNSAT", so it only ever drops constraints after a
  proven UNSAT; by induction every call returns a set that is UNSAT with its
  background. As defence in depth it re-checks its final answer when any call
  timed out and falls back to the start if that is not shown UNSAT.
- `budget_s` limits the whole extraction. When it runs out, deletion returns
  its current working set (UNSAT, because every set it moved to was shown
  UNSAT), except after an unchecked refinement, when it returns the start;
  QuickXplain returns the start.
- In both cases `minimal=False`. **An unproven core is never reported as
  minimal.** `calls == sat_calls + unsat_calls + timeouts`.

### 4.7 The verifier

`verify.py` shares no code with `core.py`. For a candidate core K it decides
(1) K is UNSAT and (2) for every c, K - {c} is SAT, using, strongest first:

| Verdict | How |
|---|---|
| `oracle` | the brute-force oracle (`resolver/oracle.py`, shares no code with any solver) when the restricted problem's search space is at most `max_states` (200,000). Restricting to K usually makes the closure tiny. |
| `plan` (SAT only) | plain backtracking (C0) returns a plan **and the independent checker `check_plan` accepts it**. A checked plan is a certificate; no trust in the solver. |
| `solvers` (UNSAT only) | C0 (plain backtracking) and C4 without LCV (AC-3 + MRV + learning), two different search algorithms, both say UNSAT. The second is also the extractor's default oracle, so the verdict's independence rests on C0. Weakest verdict, labelled as such: UNSAT has no certificate short of exhaustive search. |

A verdict that cannot be reached (timeout) is `None`, never a guess. A solver
plan the checker rejects is recorded in `errors` and gives no verdict.

Whether the oracle is usable is decided by `_small_enough`, which computes the
same number as `oracle.search_space` (product of release counts over the static
closure) but stops at the bound and skips the oracle's admissibility tables.
`search_space` took 2.8 s per call on the real boto3/botocore core, which made
verifying its hundreds of members take hours; a test checks the two agree on
300 random and edge-case instances.

---

## 5. Files

```
crux/explain/__init__.py   public API
crux/explain/core.py       extraction: deletion, deletion+refine, quickxplain, explain()
crux/explain/verify.py     independent verifier
crux/explain/corebench.py  benchmark (CLI)
tests/explain/test_core.py        extraction correctness, cost, timeouts, bad input
tests/explain/test_verify.py      the verifier rejects bad cores, fallback agrees with oracle
tests/explain/test_timeouts.py    soundness under partial timeouts
tests/explain/test_corebench.py   the harness runs and scores known cores
docs/EXPLAIN.md            this document
```

Changes outside these files: `pyproject.toml` (added `crux.explain` to the
explicit package list; required by the repo's convention, `docs/context.md`
section 9) and `docs/context.md` (state, open items and change log entries,
and a clarification of the conflict-size figure in 5.7). No resolver or data
code was changed.

---

## 6. Testing

All offline, using `Graph.from_dict`, the resolver's generators and
`tests/fixtures/`. Highlights:

- **Known cores**: `toy_abc` = both roots + all 9 edges to `c` (11; every
  version of `a` must be pinned, otherwise that version is a way out); `thrash`
  = 2 roots + 2 edges at n = 1, 3, 6, from the learning conflict and from the
  whole universe (the free roots must go); `thrash_wide` = 8 at n = 1, 4, 8;
  a root on a missing package or with an empty range = a core of one.
- **Random**: every UNSAT case of `random_problem` seeds 0 to 999, every method,
  verified by the oracle (about 700 per method). From C0, C4 and universe starts
  on seeds 0 to 199; shuffled starts.
- **Edge cases**: `tests/resolver/test_edge_fuzz.py`'s generator (pre-release
  only packages, `!=` against pre-releases, self dependencies, duplicate roots,
  ghosts, caller-disabled constraints); the core never contains a disabled
  constraint.
- **Enumeration**: on tiny instances (universe of at most 10), every subset is
  checked with the oracle and the minimal UNSAT subsets listed; the extracted
  core must be one of them.
- **Monotonicity** against the oracle on random pairs S of S'.
- **Cost**: deletion makes exactly one call per starting constraint;
  refinement never more; QuickXplain needs 16 calls for a core of 4 among 44
  (deletion 44).
- **Timeouts and budget**: all calls timing out, a zero budget, a budget that
  runs out mid-way, partial timeouts by three different rules.
- **Unsound resolver conflicts**: `deletion+refine` fed a fake resolver whose
  UNSAT conflicts are wrong in three ways still returns verified minimal cores.
- **Verifier**: redundant members named, SAT sets rejected, the fallback (no
  oracle) agrees with the oracle on 200 instances, a solver plan is checked not
  trusted, timeouts give unknown.

**Mutation check** (each mutant applied alone, `tests/explain` run):

| Mutant | Result |
|---|---|
| deletion keeps a removable constraint | caught |
| deletion drops a needed constraint | caught |
| QuickXplain forgets D2 in the background | caught |
| QuickXplain forgets C1 in the background | caught |
| deletion still calls a timed-out run minimal | caught |
| refinement keeps the removed constraint | caught (hang) |
| oracle ignores the caller's disabled set | caught |
| oracle recomputes the universe per call | caught |
| verifier never removes the member | caught |
| verifier ignores redundant members | caught |
| verifier trusts the solver's plan unchecked | survived at first; caught after adding `test_a_solver_plan_is_checked_not_trusted` |
| QuickXplain treats a timeout as UNSAT | survives: equivalent, the final re-check covers it |
| QuickXplain without the final re-check | survives: equivalent, correct timeout handling makes it sound |
| both of the last two together | caught by `test_timeouts.py` |
| refine without the final re-check | caught by `test_refinement_survives_an_unsound_conflict` |
| refine without the "keep every kept constraint" rule | survives: equivalent, the final re-check covers it (dropping a necessary kept constraint makes the final set SAT, which triggers the rerun) |

16 mutants: 13 caught, 3 equivalent. An oracle-based test cannot see a mutant
that is still correct, so equivalent mutants are expected; in each pair of
safeguards, removing both is caught.

---

## 7. Results

All numbers from runs on 2026-11-15, one Windows laptop, Python 3.13.5,
packaging 26.0. Full reports are written to `results/` (gitignored); the
commands are given with each table. Wall times are secondary (the synthetic and
real runs partly ran at the same time); **solver calls are the cost measure**.
Medians, not sums (the team's convention).

### 7.1 Synthetic (`python -m crux.explain.corebench --out results/core --seeds 300 --csp-seeds 10`)

229 UNSAT cases: the 215 UNSAT instances among `random_problem` seeds 0 to 299,
`thrash` n = 3, 9, 20, 40, `thrash_wide` n = 3, 9, `wide_pair` k = 5, 20, 50,
4 UNSAT `random_csp` instances (sizes 12 and 16, seeds 0 to 9), `toy_abc`.
Each case from up to four starting sets x three methods = **2,742 extractions**.

- **Correctness:** 2,742 of 2,742 proven minimal by the method; **2,742 of
  2,742 pass the independent verifier** (2,714 UNSAT verdicts by the oracle,
  28 by two solvers agreeing). **Known cores: exact match 114 of 114**, mean
  Jaccard 1.0.
- 47 of 229 cases have minimal cores of different sizes depending on method
  or start: a problem can have several minimal cores, and "minimal" does not
  mean "smallest".

Conflict size before and after extraction (medians):

| group | cases | universe | C0 | C4 preset | learn (default start) | minimal core |
|---|---|---|---|---|---|---|
| random | 215 | 27 | 3 | 10 | 1 | 1 |
| thrash | 4 | 18.5 | 10 | 4 | 4 | 4 |
| thrash_wide | 2 | 14 | 14 | 8 | 8 | 8 |
| wide_pair | 3 | 42 | 42 | 42 | 42 | 42 |
| random_csp | 4 | 126 | 108.5 | 53 | 53 | 21.5 |
| toy | 1 | 11 | 11 | 11 | 11 | 11 |

Median C0 conflict / minimal core: 2.25x (max 19x).

Cost, solver calls (median):

| group | start | start size | core | deletion | deletion+refine | quickxplain |
|---|---|---|---|---|---|---|
| random | learn | 1 | 1 | 1 | 1 | 0 |
| random | C4 | 10 | 1 | 10 | 3 | 4 |
| random | universe | 27 | 1 | 27 | 3 | 5 |
| thrash | universe | 18.5 | 4 | 18.5 | 6 | 12 |
| thrash_wide | universe | 14 | 8 | 14 | 10 | 18 |
| wide_pair | learn | 42 | 42 | 42 | 42 | 82 |
| random_csp | learn | 53 | 21.5 | 53 | 34.5 | 74 |
| random_csp | universe | 126 | 26 | 126 | 39 | 134 |
| toy | learn | 11 | 11 | 11 | 11 | 20 |

QuickXplain against deletion, per case (fewer calls): `random` from the
universe 207 QX / 5 deletion / 3 ties; from the default start 142 / 46 / 27
(many of QX's wins there are a 1-constraint start, which QX returns with 0 calls
and deletion checks with 1); on `thrash_wide`, `wide_pair`, `toy`, and
`random_csp` from the default start, deletion wins every case.
(`thrash` from C0 has 2 cases, not 4: C0 itself timed out on n = 20 and 40.)

**Reading.** (1) Starting from the default (learning without AC-3) conflict,
the start is already close to minimal (median 1 vs 1 on random, 53 vs 21.5 on
random_csp), and QuickXplain's divide and conquer has little to discard: it
pays about 2 calls per core member. (2) QuickXplain wins clearly only when the
core is a small fraction of a large start (random from the universe: 5 calls vs
27). (3) **deletion+refine is the cheapest or tied in every row** (tied on
random from the C0 start: 3, 3 and 3 calls), because each UNSAT trial's learned
conflict removes many constraints at once, at the price of one confirming call
at the end (4.3); but its oracle is learning without AC-3, which is slow on
boto3-scale data (4.5).

### 7.2 Real PyPI data, pilot DB (`python -m crux.explain.corebench --out results/core-real --no-synthetic --db data/crux.db --budget 120 --skip conflict:boto3`)

19 UNSAT cases derived from real metadata (`corebench.real_cases`: the newest
release of a package plus a direct or transitive dependency pinned to its
oldest release, and "ranged" variants with the package at `>=` its 6th newest
release). Constructed by us, **not user-reported failures**; real failures are
Arrya's corpus. 177 extractions.

- **Correctness:** 175 of 177 proven minimal; **175 of 175 pass the verifier**
  (141 oracle verdicts, 36 by two solvers). The 2 unproven: the C4-preset start
  with deletion on the two `boto3 ... + urllib3==1.11` transitive cases, each
  with one 10 s per-call timeout; the verifier confirms those 2 cores are not
  minimal, as the method had warned. **Known cores (direct pins): exact match 84
  of 84.**
- Core sizes: 3 for a direct pin (two roots + the edge), 4 for a transitive pin
  (the chain), 8 or 14 for ranged variants (one edge per release in the range).
- Conflict sizes (medians): universe 458, C0 8, C4 preset 8, learn 4, minimal
  core 4. Median C0 / core 2x (max 3.2x).
- Calls (median): from the default start deletion 4, deletion+refine 4,
  QuickXplain 6; from C0 8 / 6 / 11; from the universe (2 cases with at most
  300 constraints) 275 / 7.5 / 26.5.

### 7.3 The real boto3/botocore conflict

`boto3>=1.36.6` + `botocore<=1.12.7` (realbench's real UNSAT pair).
Command: `python -m crux.explain.corebench --out results/core-boto3 --no-synthetic --db data/crux.db --budget 600 --only conflict:boto3 --starts C4 --methods deletion,quickxplain`

| | start | core | calls | wall | minimal | verifier |
|---|---|---|---|---|---|---|
| deletion | C4 preset, 480 | 432 | 480 (432 SAT, 48 UNSAT) | 52 s | yes | pass (UNSAT by two solvers; each member necessary by oracle or checked plan) |
| quickxplain | C4 preset, 480 | 432 | 866 (862 SAT, 4 UNSAT) | 94 s | yes | pass |

- The core is **2 roots + one edge for each of the 430 boto3 releases
  1.36.6 to 1.43.123**, each requiring a botocore range whose lower bound is
  above 1.12.7. Every release is a separate way out, so every edge is necessary.
  This is QuickXplain's worst case (k close to n): 1.8x deletion's calls.
- Conflict sizes for comparison: universe 14,264; C0 1,292; C4 preset 480;
  the default start (learning without AC-3) **timed out** at the 10 s per-call
  limit (about 24 s, section 4.5), which is why this run starts from the C4
  preset. `explain()` with its 60 s default would get there, slowly.
- **For the renderer:** each of the 430 edges has a *different* specifier
  (`botocore<1.44.0,>=1.43.123`, `...>=1.43.122`, ...), so grouping by identical
  specifier does not compress anything. Group by (package, dependency) and
  summarise the bounds: "every boto3 release from 1.36.6 to 1.43.123 needs
  botocore 1.36.x or newer, but you asked for botocore<=1.12.7".
- The verifier took about 2 minutes for this core (432 checks), after the
  `_small_enough` fix (4.7); before it, hours.


---

## 8. Against the proposal's targets

| Measure (proposal 3.6) | Target | Result |
|---|---|---|
| Core minimality | 100% of cores minimal on oracle re-check | **Met**: every core called minimal passed the independent verifier (2,742 synthetic, 175 real derived, plus every test). Cores that could not be proven (timeouts, budget) are labelled `minimal=False`, never passed off as minimal. |
| Core accuracy, planted | exact match on at least 95% of planted cores | **Not measurable yet**: Arrya's planted-conflict generator does not exist. On cores known by construction (toy, `thrash`, `thrash_wide`, `wide_pair`, derived direct pins): 198 of 198 exact (100%). These are not the planted corpus. |
| Core accuracy, real | mean Jaccard at least 0.8 against hand-annotated cores | **Not measurable yet**: needs the real-failure corpus and annotations. |
| Deletion vs QuickXplain cost | compare | **Done** (7.1): QuickXplain wins only when the core is a small part of the start; from the solver's tight conflict deletion is cheaper, and deletion+refine is cheapest or tied. |
| Time limit on large inputs (risk table) | per-case time limit, report cases that hit it | **Done**: `budget_s` and per-call timeouts; unproven cores reported (7.2, 7.3). |


---

## 9. Open items and handover

**For Parth Gupta (renderer, user study).** Call `explain(problem)`; group
the core as in section 3. Large real cores (hundreds of boto3 edges) must be
summarised by package and dependency, with the version bounds, not listed. The 30 hand-written ideal
explanations can use `toy_abc` and the derived real cases in
`corebench.real_cases` as material.

**For Arrya (benchmark).** Use `extract_core` / `explain` on the planted and
real corpora; score with exact match and `corebench.jaccard`. Make sure a
planted core is minimal (check it with `verify_core`) before using exact match
as the metric. For the pip/uv comparison, "distinct requirement statements
named in the message" for Crux is the number of groups in the rendered core,
not `len(core)`.

**For the resolver team (Aahana, Jas Simar, Parth Sureka).**
- The C4 preset's UNSAT conflict includes all of AC-3's justifications, which
  makes it larger than C0's on random instances (section 4.4). If a tighter C4
  conflict is wanted, the AC-3 branch of `_explain` could add only the edges
  that justified the deleted versions it relied on.
- With AC-3 off, `learning.py` `_explain` costs about 8 s per solve on the
  real boto3 conflict; it loops over every version of the variable and
  rebuilds `SpecifierSet`s through `Constraint.specifier` (section 4.5; the
  exact cause is not settled).

**For Sumehra (CLI).** `explain(problem, method="deletion")` or `quickxplain`
with a `budget_s` (for example 60) is the safe call; print the problem's status
when it is not UNSAT.

**Mine, still open.**
- Extracting a core from learned nogoods instead of re-solving (proposal 3.4,
  "extension if time allows"): not done.
- Hand-annotated cores for real user-reported failures (the Jaccard target)
  need Arrya's corpus; there is none yet.
- Group-level deletion (try removing all edges of one package at once before
  single constraints) would cut the calls on boto3-sized cores; not done.

---

## 10. Viva notes (questions I should be able to answer)

- *What is a minimal unsatisfiable core, and how is it different from a
  minimum one?* Minimal: no single member can be dropped. Minimum: smallest
  possible size; finding that is much harder (it needs all MUSes). We
  promise minimal, which is what the proposal says.
- *Why is deletion correct?* Monotonicity (4.2) and the argument in 4.3.
- *Why does QuickXplain use fewer calls, and when not?* Divide and conquer
  discards whole halves at once when they are irrelevant; with a core that is
  most of the set, every half is needed, so it pays about 2 calls per member.
- *What is the oracle and why compute the universe once?* 4.1.
- *What happens on a timeout?* 4.6; never reported as minimal.
- *How do you know your cores are minimal?* An independent verifier that
  shares no code with the extractor, using the brute-force oracle or checked
  plans (4.7), plus enumeration of all MUSes on tiny instances, plus a mutation
  check (6).
- *Why not start from the C4 preset's conflict?* 4.4.
- *Why is the toy core 11 constraints when the message is two lines?* Each
  version of a and b is a separate edge, and each must be in the core, or that
  version would be a way out. The renderer groups them.

---

## 11. AI usage (for the report's disclosure)

Tool: Claude Code (Anthropic, Claude Opus 5.5 model), 2026-11-15, used as a
pair programmer on this component.
- **What for:** reading the existing docs and resolver code; drafting
  `core.py`, `verify.py`, `corebench.py`, the tests and this document; running
  the tests, the mutation check, the profiler and the benchmark.
- **How it was checked:** every core is checked by a verifier that shares no
  code with the extractor; the brute-force oracle and an exhaustive enumeration
  of minimal cores on tiny instances; known cores by construction; a mutation
  check of the tests (section 6); every number in sections 4 and 7 comes from a
  command run in this session, and the commands are listed with them.
- **Corrections made during the work:** a miscounted test expectation (44 not
  46 constraints in `thrash(40)`); an infinite loop in a mutation script; the
  oracle configuration changed after profiling showed learning was 100x
  slower on real data; the verifier's oracle-size check replaced after it took
  hours on a real core (and its first version mishandled a bound of 0, caught
  by the new agreement test); two tests added after mutants survived; the AI
  committed the work without being asked and the commits were undone at the
  owner's request (the owner commits his own work).
- **Final review (2026-11-15)**, a line-by-line re-read of every file against
  the proposal and the docs, found and fixed: a soundness gap in
  `deletion+refine` (it trusted the resolver's conflict; section 4.3), a wrong
  docstring default, unused names in `verify.py`, a known-core helper that
  could pick the wrong edge, a crash on an empty benchmark result, and several
  wrong statements in this document (test counts, the edge-case count, the
  start configuration's label, the change-log section number). The benchmarks
  were rerun after the fix.

---

## 12. Change log

- **2026-11-15.** `crux/explain/` (core, verify, corebench), `tests/explain/`,
  this document; `crux.explain` added to `pyproject.toml`; entries in
  `docs/context.md`. Pilot database rebuilt locally (`run_ingest --limit 30`,
  `clean`, `qa`): 7,830 releases, 17,300 edges, QA 0 errors and 0 warnings,
  identical to the documented pilot.
- **2026-11-15, review.** `deletion+refine` safeguards (4.3) and their tests;
  `corebench` fixes; verifier docstring; this document corrected; synthetic
  and real benchmarks rerun (section 7).
