# Crux resolver: working context

The handover document for anyone continuing the resolver work (Aahana, Jas Simar,
Parth Sureka, and the teams that build on them). It records what exists, why it is
the way it is, what was measured, and what is still open. **When you finish
something, update section 7 (state) and section 10 (change log).**

Last updated: 2026-11-14. Test suite: **1,102 passing** (`python -m pytest -q`).

---

## 0. At a glance

**What exists.** A from-scratch dependency resolver over the Crux data layer, in
`crux/resolver/`: the contract, an independent plan checker, a brute-force oracle
and generator, five search configurations (C0 to C4), scale families, an
experiment harness, an ablation, and a real-data benchmark. All of it is tested
and nothing is committed yet (see section 3).

| Config | Adds | State |
|---|---|---|
| C0 | plain backtracking | done |
| C1 | + AC-3 preprocessing | done |
| C2 | + MRV variable ordering | done |
| C3 | + LCV value ordering | done |
| C4 | + conflict-directed backjumping and nogoods | done |

**Headline results (all measured; caveats in sections 6.2 and 6.3).**
- Correctness: every configuration agrees with the brute-force oracle on 1,000
  random instances; every SAT plan passes the independent checker; every UNSAT
  conflict is sound. 275 ablation runs and 168 real-data runs had no wrong
  answer, disagreement, or invalid plan. An independent review found one bug in the
  checker and oracle (the `!=` pre-release rule) and one timeout overrun in AC-3; both
  are fixed with regression tests (sections 4 and 6.3).
- Median node reduction C0 to C4: 34,444.5x (`thrash`, n=9), 16,607x
  (`thrash_wide`, n=9), 500x (`pinned_pair`, m=1000), and on `random_csp` 7.4x,
  19x, 306x (at least 454x), 66x (at least 828x) at sizes 12, 16, 20, 24. These
  credit the whole stack: the `thrash` gain is all MRV, the `pinned_pair` gain is
  all AC-3, the `thrash_wide` gain is all C4. The proposal's 10x target is met at
  the largest size of every family except `random_csp` (7.4x at size 12);
  `pinned_pair` at m=10 is only 5.5x.
- On the **real** pilot graph, almost every requirement set is trivial for plain
  C0; only the `boto3`/`botocore` pins make it scan (1,217, 2,123 and 430 nodes),
  and AC-3 fixes those. **Real data does not show C2 to C4 helping**, and **LCV (in
  C3 and C4) picks very old releases** there (`requests` 2.15.1 alone instead of
  2.34.2 with four dependencies), so C3/C4 are not the configurations to use for
  real resolution. `C4-noLCV` (AC-3 + MRV + learning) keeps newest-first plans.

**Core extraction** (Yash, 2026-11-15) is in `crux/explain/` and documented in
`docs/EXPLAIN.md`.

**Open (details in section 8).**
1. Nothing from this work is committed or pushed.
2. The final report is not written yet (section 3).
3. Each member must be able to explain their component in the viva.
4. No hard real conflict has ever been run through C4 (needs Arrya's corpus).
5. `Graph.allowed()` costs about 4.6 s cold on `boto3` (data team's module).
6. Explanation module, pip/uv comparison, user study, CLI belong to other members.
7. LCV picks very old, dependency-free releases on real data (section 6.3): decide
   whether the recommended configuration should drop LCV.

**Run it.**
```bash
pip install -e ".[dev]"
python -m pytest -q                                               # 1,102 pass, about 3 min
python -m crux.resolver.ablation --out results/ablation --seeds 10 --timeout 10 --repeats 3
python -m crux.data.run_ingest --limit 30 && python -m crux.data.clean && python -m crux.data.qa
python -m crux.resolver.realbench --db data/crux.db --timeout 10 --out results/real
```

---

## 1. What Crux is

University project on **Search & Problem Solving** (declared track: **Application**).
A Python dependency resolver written from scratch as a **constraint satisfaction
problem** over a frozen snapshot of real PyPI metadata. It returns either an
install plan or, on failure, a conflicting set of requirements that the
explanation team reduces to a minimal core and states in plain English. It is to
be measured against pip and uv. No resolvelib, PubGrub or SAT library.

Target: CPython 3.12, Linux x86-64. Snapshot cutoff 2026-11-01. Repo:
`https://github.com/Kurokokun21/crux`. Working branch: `parth`.

**CSP model.** Packages are variables, versions are their domains, each
dependency is a binary constraint "if p = v then q is in S". It is a *dynamic*
CSP: a package is **active** only when a root requirement or a chosen version
needs it. The plan lists exactly the active packages.

Techniques (the proposal's course links): backtracking, AC-3, MRV, LCV,
conflict-driven learning (backjumping and nogoods), and, for the explanation
team, minimal unsatisfiable cores.

---

## 2. Team and ownership

From the project proposal: eight members, four components.

| Component | Member | Owns |
|---|---|---|
| Data and ingestion | Ritika Mirium Thomas | PyPI ingestion, cache, snapshot, local index mirror |
| Data and ingestion | Sumehra Afsheen | PEP 440/508 parsing, graph, scope doc, later the CLI |
| **Resolver engine** | **Aahana Patil** | Backtracking (C0), conflict-driven learning and backjumping (C4) |
| **Resolver engine** | **Jas Simar Kaur** | AC-3 (C1), brute-force oracle, plan checker, correctness tests |
| **Resolver engine** | **Parth Sureka** | MRV (C2), LCV (C3), ablation experiment |
| Explanation | Yash Kejriwal | Core extraction (deletion, QuickXplain), minimality verifier |
| Explanation | Parth Gupta | English renderer, ideal explanations, user study |
| Benchmarking | Arrya Sridhar | Hard-case corpus, planted-conflict generator, pip/uv harness, plots |

All three resolver-engine roles were worked together in one session. "Owns" says who
answers for a component.

---

## 3. Course requirements, and where we stand

Source: `AI_Project_Guidelines` (PDF, 6 pages). Final report, code and AI-usage
disclosure are due **18 Nov 2026**; live demo and individual viva on **21 Nov 2026**.

**What the guidelines require of the final submission**
- Written report: Introduction, Background/Related Work, Method, Results/Evaluation,
  Discussion and Limitations, Individual Contributions, and **AI Usage details**.
- Code repository **with a commit history that reflects work across the semester**,
  "not a single last-minute upload"; a project assembled in the last 48 hours with no
  earlier history "will be flagged for review".
- Presentation of 6 slides (title with team name, team number, project name, repo
  link, type and area; problem; related work; methods; results; project management
  and work division), live demo, and an **individual viva**: every member is asked
  about the project and "must be able to explain any part ... they are associated
  with". A section you cannot explain "will be treated as if you did not do that
  part"; substantiated free-riding lowers the individual grade.
- AI tools are allowed, but the report must state **which tools, for what, and how
  the output was verified**; the team owns correctness, and a fabricated result or
  citation is graded as the team's own error.
- Rubric (40 marks): report technical depth and correctness (5), report quality (5),
  track-specific criterion (5), live demo including Q&A (15), GitHub code
  (reproducibility, organisation, consistency) (5), process and accountability
  (work distribution over time, honest reflection) (5). The **Application** track
  is judged on solving "a real, well-scoped problem with realistic data/users and a
  clear account of impact and limitations".

**Where we stand (facts as of 2026-11-14)**

| Requirement | Status |
|---|---|
| Team size 7 to 8 | 8, ok |
| One declared track, unchanged | Application, unchanged |
| Commit history across the semester | **Off track.** 18 commits, all between 2026-11-08 and 2026-11-12, all on the data layer. No commits from Aahana, Jas Simar, Parth Sureka, Yash, Parth Gupta or Arrya. The resolver is uncommitted. The proposal promised weekly commits from week 1 |
| Viva readiness | **Not prepared.** Each owner must study their component and be able to explain it |
| Realistic data (Application track) | Partly: the real pilot graph is benchmarked (6.3). No hard real conflicts, no user study, no pip/uv comparison yet |
| Code reproducibility and consistency | README is stale (says 111 tests, does not describe the resolver); `snapshots/` is not committed; no LICENSE or CI |
| Citations | The proposal's references were not verified by us; each author should check theirs |

**Do not backdate commits or rewrite history to simulate a longer timeline.** That
would be a worse integrity problem than a thin history. Commit honestly, say in the
report's Individual Contributions and reflection sections that the resolver was built
in a concentrated sprint, and let each member add real work (reviews,
tests, docs, their report sections) under their own name from now on.

---

## 4. The data layer (done; do not change without its owners)

Lives in `crux/data/`. Pipeline: `run_ingest` -> `clean` -> `qa` -> `Graph`. Full
detail in `README.md`, `docs/HANDOVER.md`, `docs/SCOPE.md`, `docs/PROGRESS.md`,
`docs/PILOT.md`.

What the resolver uses (`from crux.data.graph import Graph`):

| Call | Returns |
|---|---|
| `Graph.from_dict({pkg: {ver: [[dep, spec], ...]}})` | a graph for tests |
| `Graph.load("data/crux.db")` | the real graph (the DB is gitignored) |
| `versions(pkg)` | `list[Version]` newest first, `[]` if unknown. **No pre-release rule** |
| `deps(pkg, version)` | `list[(dep_name, SpecifierSet)]` |
| `candidates(dep, spec)` | `tuple[Version]` newest first, **applies the pre-release rule** |
| `allowed(dep, spec)` | the same versions as a `frozenset` |
| `dependents(dep)` | reverse edges `(pkg, Version, SpecifierSet)` |
| `dangling()` | edges to packages with zero versions |

Facts to rely on: names are canonical (PEP 503); one shared `Version` object per
release; `str(SpecifierSet)` is sorted, so specifier strings are stable keys; edges to
packages with no versions are kept (an empty `candidates()` is a dead end, not a bug).

**Caveat for any claim in the report.** The graph is *easier* than real resolution:
extras are dropped (over half of all `Requires-Dist` lines), sdist-only releases are
dropped (15% of versions), `requires_dist: null` is stored as zero dependencies, and
one wheel is kept per version. Say so whenever comparing with pip or uv.

**The real pilot database.** `data/crux.db` exists locally (gitignored). We built it
ourselves with `run_ingest --limit 30`, `clean`, `qa` (160 s, placeholder
`USER_AGENT`). It **reproduced the original pilot**: 7,830 releases, 17,300 edges,
identical edge-drop counts, QA 0 errors and 0 warnings. The only difference is 25
`after_cutoff` drops instead of 13, the releases uploaded since the original run,
which the cutoff excludes, so the kept data is unchanged. Graph: 76 packages, 27
dangling edges, 0 collisions, loads in 0.47 s with a 14.3 MiB peak; `botocore` has
2,420 releases and `boto3` 2,146.

The pilot run also wrote `snapshots/top5000-2026-11-01.json` (5,000 names, untracked).
Its first names match `docs/SPIKE.md` (`boto3`, `packaging`, `typing-extensions`,
`certifi`, `idna`) and the pilot it produced is identical to the original, so it is very
likely the same list the data team used, but **it has not been confirmed byte-identical
to Ritika's file**. Ask her before committing it: that file is what makes a fresh clone
reproduce the 2026-11-01 dataset.

### The pre-release rule (verified)

A pre-release is admissible for a specifier only if the specifier itself names a
pre-release **in an inclusive clause (any operator except `!=`)**, **or no final
release of that package satisfies the specifier**. The rule
is per package, not per version, so `SpecifierSet.contains()` alone is wrong. A bare
requirement on a package with `1.0` and `2.0b1` excludes `2.0b1`; on a package with
only `1.0b1` and `1.0b2` it admits both. Dev releases count as pre-releases.
`checker.admissible()` re-derives this independently and
`test_admissible_matches_graph_candidates` checks it against `Graph.candidates` on 400
random cases. (`docs/HANDOVER.md` states the rule slightly wrong: it omits "or no
final release satisfies".)

`!=1.0b1` does **not** count as naming a pre-release: excluding one pre-release is not
an invitation to pick others, and packaging 26.0 (pinned) agrees. The first checker
counted it, so the checker and the oracle (which borrows the checker's rule) disagreed
with every solver on specs like `a!=1.0b1, a!=1.0`. An independent review found it with
a fuzzer (40 wrong answers and 18 unsound conflicts in 2,000 edge-case instances); the
differential test's spec pool now includes `!=`-against-pre-release specs and
`tests/resolver/test_edge_fuzz.py` covers it permanently.

---

## 5. Resolver design

### 5.1 The contract (`crux/resolver/problem.py`)

**Extend by adding fields, never by changing existing ones**; other components are
written against it.

- `Constraint(kind, dep, spec, pkg, version)`: a root requirement (`kind="root"`) or one
  dependency edge (`kind="edge"`). Frozen, hashable, **its own identifier**. Build with
  `Constraint.root(dep, spec)` / `Constraint.edge(pkg, version, dep, spec)`, which
  canonicalise names and normalise the specifier string.
- `Problem(graph, roots, disabled=frozenset())`. `Problem.from_requirements(graph,
  ["flask>=3", ...])` builds the roots; markers, extras and URLs raise `ValueError`.
  `disabled` constraints are **ignored totally**: they need not hold and they do not
  activate their dependency. Core extraction calls `solve` repeatedly with different
  `disabled` sets.
- `Problem.constraints()`: every enabled constraint that could matter (active roots plus
  every release's edges in the static closure). Core extraction picks its subsets here.
- `Config(ac3, mrv, lcv, learning, timeout_s=60, nogoods=True)`. `Config.named("C0".."C4")`
  gives the cumulative presets; the flags are independent, so any combination runs.
  `nogoods` only matters with `learning`; `False` gives backjumping alone.
- `Stats(nodes, backtracks, ac3_pruned, wall_s, ac3_versions, nogoods, backjumps)`.
  `ac3_pruned / ac3_versions` is the share of versions AC-3 removed.
- `Result(status, plan, conflict, stats)`: `SAT` has a `plan` mapping each **active**
  package to one `Version`; `UNSAT` has a `conflict`, a list of `Constraint` that cannot
  all hold (**sound, not necessarily minimal**); `TIMEOUT` has neither and never raises.
  Invariants are enforced in `__post_init__`.
- `solve(problem, config=Config()) -> Result` in `crux/resolver/solver.py`. All
  configurations must agree on SAT/UNSAT; they differ only in effort and in how tight
  the conflict is.

Decisions made at the start (change only by agreement): constraints are first-class and
disableable; UNSAT returns constraints, not a derivation; "not installed" is **not** a
domain value (it is the absence of a key in the plan); presets are cumulative with
independent flags underneath; out-of-scope requirements raise instead of being ignored;
integer version ids are deferred.

### 5.2 Plan checker (`checker.py`, Jas Simar)

`check_plan(problem, plan) -> list[Violation]` (empty means valid) and
`is_valid(problem, plan)`. Independent of any solver: it reads only `Graph.versions()` and
`Graph.deps()`, and every other component is tested against it. A plan is valid when:

1. keys are canonical names mapping to `Version` objects, and every version is a real
   release;
2. every active root requirement is met;
3. for every chosen release, every non-disabled dependency is met;
4. the plan contains **exactly** the active packages: those reachable from an active
   root through non-disabled dependencies of the chosen releases.

"Met" means the dependency is in the plan and `admissible()` under the pre-release rule.
Violation kinds: `bad_plan`, `non_canonical_name`, `unknown_package`, `unknown_version`,
`missing_dependency`, `unsatisfied`, `unneeded_package` (reported only when reachability
is fully known, to avoid cascade noise). `tests/resolver/test_checker.py` includes an
exhaustive proof that `toy_abc.json` with roots `a`, `b` has no valid plan among all 160
version combinations.

### 5.3 Oracle and generator (`oracle.py`, `gen.py`, Jas Simar)

**Oracle.** Exhaustive generate-and-test that shares no code with any solver and never
prunes. It gives every package in the static closure a version (`None` for a package
with no releases), walks from the active roots, and the packages reached are the plan.
`brute_force(problem, max_states=2_000_000) -> OracleResult(status, plan, states)`,
`all_plans`, `count_plans`, `search_space`; `OracleLimitExceeded` over the cap. UNSAT
carries no conflict. It is cross-checked against a second enumeration that asks
`check_plan` (480 random instances, with and without disabled constraints).

**Generator.** `random_problem(seed, max_packages=8, max_versions=6, max_states=50_000,
max_roots=3, max_deps=3)`: deterministic per seed, versions `1.0` to `6.0` plus occasional
`b1`/`rc1` pre-releases, ranges, `==`, `!=`, pre-release bounds, cycles and edges to a
missing package. Instances over the state cap are **redrawn smaller**, so the largest
cases (8 packages, 6 versions) are rarer than the proposal's wording suggests; say so
when quoting the "1,000 random small instances" target. On seeds 0 to 999: 29.5% SAT,
median search space 216, maximum 48,000. It never produces `!=` against a pre-release,
wildcards, `~=`, self-dependencies, duplicate roots or roots on packages without
releases; `tests/resolver/test_edge_fuzz.py` has a second generator that does, and
checks all 20 flag combinations against the oracle on 600 such instances.

**How solvers are tested.** For each seed, `solve(problem, cfg)` must give the same
SAT/UNSAT as `brute_force`, every SAT plan must pass `check_plan`, and the UNSAT
conflict must stay UNSAT when everything outside it is disabled. `test_backtrack.py`
runs this through a `cfg` fixture over `_CONFIGS` (C0 to C4, `mrv-only`, `lcv-only`,
`learning-only`, `backjump-only`, `learning+ac3`).

### 5.4 C0: plain backtracking (`backtrack.py`, Aahana)

Iterative DFS (a 3,000-deep chain works). State: partial assignment, the requirements
placed on each active package, activation order, and an undo trail. It picks the first
unassigned active package, tries each version admissible under every requirement on it
(via `Graph.candidates` / `allowed`), recurses, and undoes on a dead end.

`class Search` exposes the hooks the later configurations replace:

| Hook | C0 behaviour | Replaced by |
|---|---|---|
| `select_variable()` | first-activated unassigned package | C2 MRV |
| `order_values()` | newest first | C3 LCV |
| `domain()` | versions admissible under every requirement | C1 AC-3 prunes it |
| `_conflict()` | roots + edges of every tried version | C4 learned conflict |

`_setup()` runs AC-3 and activates the roots; `req_info` and `last_fail` record the
constraint behind each requirement and the one that failed an assignment (C4 needs both).
`Stats.nodes` counts assignments tried, `backtracks` assignments undone. The timeout is
checked before each assignment and returns `TIMEOUT`.

**C0's UNSAT conflict is sound but coarse**: the active roots plus every edge of every
version tried. Verified by `test_conflict_alone_is_still_unsatisfiable` (disable
everything outside the conflict; the oracle still says UNSAT).

### 5.5 C1: AC-3 (`ac3.py`, Jas Simar)

`ac3(problem, deadline=None) -> ACResult(domains, pruned, versions, wiped, used)`. Runs
**once before search** over the static closure; not maintained during search.

- Version v of p has *support* for the constraint "p=v requires q in S" while q still has a
  version in S. Unsupported versions are deleted and deletions propagate to a fixed
  point. No solution is lost (tested against an independent naive fixed point).
- Root requirements first act as **unary** constraints; those removals are **not counted**
  as AC-3 work (C0 honours them too), so `ac3_pruned` is only what arc propagation added.
- A package inactive in some plan can still have versions pruned.
- `wiped` (a root package has no version left) means UNSAT with zero search nodes.
- **Soundness bookkeeping:** every deletion records the edge that justified it in `used`;
  the solver's conflict includes them, because a version AC-3 removed is never tried.
- Respects the deadline (`ACTimeout`).

Effect: `pinned_pair` 1,001 nodes -> 2 and 1.12 s -> 0.02 s at m=1000; **no effect (0%
pruned) on `thrash`, `chain` or `random_csp`**. AC-3 alone is weak outside directly
unsupportable constraints; that is what makes C2 to C4 necessary.

### 5.6 C2 (MRV) and C3 (LCV) (`ordering.py`, Parth Sureka)

Both only reorder the search, so they cannot change SAT/UNSAT. They work with or without
AC-3.

- **MRV** `mrv_select`: the unassigned active package with the fewest admissible versions;
  ties go to the one activated first; an already-empty domain is picked immediately, which
  turns a hidden dead end into an immediate one (why `thrash` collapses).
- **LCV** `lcv_order`: ascending `lcv_cost`, the number of options a version rules out for
  its dependencies; **ties keep newest-first**. `_cost_cached` computes each neighbour's
  domain and each distinct requirement once per call (the reference loop `lcv_cost` is
  kept, and differential tests check they agree). This took LCV on real `boto3` from
  6.6 s to 0.25 s.

**MRV is not monotone and has a heavy tail.** It wins most `random_csp` instances: in a
40-seed scratch study (not saved under `results/`) 32 of 39 at size 12, 36 of 40 at 16,
15 of 17 at 20, median C1/C2 node ratio 3.5x, 6.7x, 13.5x; in the saved 10-seed ablation
run, wins/losses 8/2, 9/1, 10/0, 8/2 and medians 3.25x, 9.16x, 13.55x, 17.97x at sizes 12
to 24. But one size-16 instance goes from 31 nodes to **87,553**. Summed nodes are dominated by
such outliers (an 8-seed sum even flipped the conclusion), so **report medians, win counts
and timeouts, never totals.** LCV saves almost no nodes and costs 1.5x to 2.5x more time
per run. **On real data LCV picks very old releases**: it prefers the release that rules
out the fewest options for its dependencies, and old releases are recorded with no
dependencies at all (a data-layer caveat), so `requests` resolves to 2.15.1 alone and
`boto3` to 1.2.0. The plans are valid against the graph but not what a user wants, and
node counts between LCV and newest-first configurations compare different answers.
Because an oracle cannot judge a heuristic, `test_ordering.py` tests the ordering
directly and was mutation-checked (largest domain, last-activated tie-break, costliest
first, oldest-first ties: all caught).

`bench.non_latest_pins(problem, plan)` implements the proposal's "report how many pins
differ from the latest release".

### 5.7 C4: conflict-directed backjumping and nogoods (`learning.py`, Aahana)

`LearningSearch(Search)`, chosen when `config.learning`; it reuses the same hooks.

- Each decision is a frame with a level. A **blame** rule: a version of q ruled out by a
  requirement from p's chosen version blames p (the **earliest** excluding source if
  several; a root requirement or an AC-3 deletion is static and blames no one).
- A **conflict** is (D, K): D the decisions to blame, K the constraints used. At a dead
  end D covers every excluded version, the conflicts returned from tried versions (minus
  the variable itself), a neighbour that rejected a version at once, **and the decision
  that activated the package**.
- **Jump:** D empty means UNSAT; otherwise rewind to the decision in D with the highest
  level, add D minus it to that frame's conflict, try its next value.
- **Nogoods:** a recorded conflict `({(pkg, ver)...}, K)`; when all but one member are
  assigned, the last version is ruled out like a requirement. Capped at 20,000,
  deduplicated.

**The bug found while building it.** A dead end at a package also depends on the decision
that *activated* it: change that decision and the package may vanish. The first version
blamed only decisions that excluded versions, so a package with no versions looked like an
unconditional dead end and C4 answered UNSAT where a solution existed (34 of 1,000 random
instances; smallest was seed 811, now a regression test).

**The UNSAT conflict is tight.** K of the final refutation plus the activating chain of
every package involved. On `thrash_wide` it is **exactly 8 constraints at any size**; on
287 random UNSAT instances the mean size is 5.5 (C0) versus 2.0 (C4), smaller in 223, equal
in 64, never larger. If AC-3 deleted a version the refutation relied on, all of AC-3's
`used` constraints are added (coarse but sound). This is the input for core extraction.

*Clarification (Yash, 2026-11-15, `docs/EXPLAIN.md` 4.4):* the "2.0 (C4)" figure is for
**learning alone** (`Config(learning=True)`, as in `test_learning.py`), not the C4 preset.
With AC-3 on, the AC-3 rule above applies often: on the 215 UNSAT instances among
`random_problem` seeds 0 to 299 the C4 preset's conflict has mean 10.3 constraints (C0 5.6,
learning + MRV without AC-3 1.86, true minimal core 1.79). Core extraction therefore starts
from learning without AC-3.

**Mutation check.** Jumping to the earliest decision (20 wrong), forgetting the activating
decision (12 wrong, 12 unsound), not passing the conflict to the target (2 wrong, 6
unsound), an over-general nogood (1 wrong, 3 unsound) and dropping the activation chain
from K (53 unsound) are all caught. **One mutant survived the oracle tests**: blaming the
*latest* excluding requirement. It is sound but does 23% to 62% more work; a direct unit
test now pins "earliest". An oracle cannot protect an efficiency decision.

---

## 6. Evaluation tooling and results

### 6.1 Scale families and the harness (`families.py`, `bench.py`)

`build(size, seed) -> Case(family, size, seed, problem, expected)`; `expected` is known by
construction, so correctness is checked at sizes the oracle cannot reach.

| Family | Shape | Expected | C0 (measured) | What fixes it |
|---|---|---|---|---|
| `thrash(n)` | n free packages, then a late two-package conflict | UNSAT | 93, 849, 7,653, 68,889 nodes for n=3,5,7,9 (x3 per package) | MRV (C2), C4 |
| `thrash_wide(n)` | as `thrash`, but the culprits have as many versions as the free packages, so MRV has no signal | UNSAT | C0 to C3 all exponential (363 ... 265,719; MRV slightly worse) | **only C4** (n+7 nodes) |
| `pinned_pair(m)` | boto3 version v pins botocore==v; root wants botocore<=1 | SAT | m+1 nodes, scans every release | AC-3 |
| `chain(n)` | n packages in a line | SAT | n nodes, 0 backtracks (control) | nothing |
| `random_csp(n, seed)` | random interval constraints, k=4, tightness 0.4, density 3.6/n | unknown | median 386 (n=12), 1,048 (16), 38,254 with 3 of 10 timed out (20), 135,840 with 5 of 10 timed out (24; medians with timeouts are lower bounds) | everything |

`random_csp` is tuned: with k=5 it is trivially satisfiable with zero backtracking, so keep
k=4 unless you re-measure (a test guards it). These are synthetic; Arrya's planted-conflict
generator and real-failure corpus are the benchmark proper.

`python -m crux.resolver.bench` runs configurations over families (one row per case and
config: status, nodes, backtracks, AC-3 share, wall time, `correct`, `consistent`,
`valid_plan`, `non_latest`). Node counts are deterministic for runs that finish; a
timed-out run's count depends on machine speed. Wall-clock is the median of `--repeats`. It exits non-zero on any wrong, inconsistent or invalid row. A configuration
that cannot run is skipped and listed, never run with a flag ignored.

### 6.2 The ablation (`ablation.py`; run of 2026-11-14)

```bash
python -m crux.resolver.ablation --out results/ablation --seeds 10 --timeout 10 --repeats 3
python -m crux.resolver.ablation --from-rows results/ablation/rows.csv --out results/ablation
pip install -e ".[plots]"      # optional, for the two figures
```

Writes `rows.csv`, `summary.csv`, `reduction.csv`, `report.md`, `nodes_by_size.png`,
`reduction.png` to `--out` (`results/` is gitignored; commit the numbers you quote).

**How the numbers are read (decided here, keep them).** Medians, win counts and timeout
counts, never summed nodes. **Timeouts are censored**: a timed-out run reports the nodes
reached at the deadline, a lower bound, so the reduction is given twice: `median` (cases
both finished) and `median >=`, a valid lower bound on the median over **all** cases, in
which a case where the base timed out counts at its computed ratio and a case where the
*other* configuration timed out (or both did) counts as 0. The first version dropped those
cases instead, which is not a lower bound; the independent review caught it (a test now
checks the bound against randomly drawn true values). Timeouts are also counted in both
directions. Because timed-out counts depend on machine speed, medians that include
timeouts are not exactly reproducible. The report states a
correctness check line and says "PROBLEM ROWS ... do not quote" if any row fails. Run it on
an idle machine for wall times.

**Result: 275 runs, no wrong answers, no disagreements, no invalid plans.**

| Family | size | median C0/C4 | median >= | wins/losses | C0 timeouts | C4 timeouts |
|---|---|---|---|---|---|---|
| `thrash` | 9 | 34,444.5x | same | 1/0 | 0 | 0 |
| `thrash_wide` | 9 | 16,607x | same | 1/0 | 0 | 0 |
| `pinned_pair` | 1000 | 500x | same | 1/0 | 0 | 0 |
| `chain` | 1000 | 1.0x | same | ties | 0 | 0 |
| `random_csp` | 12 | 7.4x | 7.4x | 9/1 (worst 0.46x) | 0 | 0 |
| `random_csp` | 16 | 19.3x | 19.3x | 9/1 (worst 0.67x) | 0 | 0 |
| `random_csp` | 20 | 306x | **>= 454x** | 7/0 (+3 where only C0 timed out) | 3 of 10 | 0 |
| `random_csp` | 24 | 66x | **>= 828x** | 5/0 (+5 where only C0 timed out) | 5 of 10 | 0 |

C4 never timed out, so these lower bounds are unaffected by the review's correction.

What each technique adds: **C1** 500x on `pinned_pair`, 1.00x everywhere else. **C2** 34,444x
on `thrash`, 3.3x to 18x on `random_csp`, but 0.82x on `thrash_wide`. **C3** about 1.0x in
nodes and about 2x the time; it timed out twice: on `thrash_wide` n=9 (where C2 took
6.2 s and C0 5.0 s; the per-step cell reads `0.00 (C3 t/o on 1)`) and on one `random_csp`
24 seed. The C0-to-C1 cell at `random_csp` 24 reads `0.50 (C1 t/o on 5)` because C0 and C1
both timed out on the same 5 seeds, and a both-timed-out case tells us nothing.
**C4** 16,607x on `thrash_wide`, 1.8x / 2.0x / 20x / 92x on `random_csp` 12 / 16 / 20 / 24, and
it never timed out.

**Against the proposal's target** (median node reduction >= 10x from C0 to C4 on hard
instances): met on `random_csp` at sizes 16, 20, 24 and at the largest size of every
thrash/pinned family; **not met at `random_csp` 12 (7.4x)** nor at `pinned_pair` m=10
(5.5x); not applicable to `chain`. Say exactly that.

State alongside any number: synthetic families only; only `random_csp` has many seeds;
medians over 10 seeds are modest samples; at sizes 12 and 16 one instance has C4 using
*more* nodes than C0, so C4 is not uniformly better than the baseline; wall time is not
nodes. Two earlier versions of the figures hid coinciding lines and drew "no change" as a
bar; both were fixed after looking at them. **Look at generated figures before trusting
them.**

### 6.3 The resolver on real PyPI data (`realbench.py`; run of 2026-11-14)

```bash
python -m crux.resolver.realbench --db data/crux.db --timeout 10 --out results/real
```

28 distinct real requirement sets (single packages, pairs, old pins, the boto3/botocore
pins, one real backtracking case, one real UNSAT case; the first runs counted
`boto3+botocore` twice, as 29) x 6 configurations (C0 to C4 plus `C4-noLCV`) = 168 runs
in the latest run. **Every SAT plan passed the checker and every configuration that
finished agreed on every case.** The oracle is far too big here, so this cannot prove an
UNSAT answer correct; it catches wrong plans and disagreement.

**The plans differ by configuration** (the report's "Plan chosen" table, packages / pins
below the newest release). C0, C1, C2 and `C4-noLCV` return the **same newest-first plan**
on every case they finish (judged by plan size and old pins). C3 and C4 (LCV) return a
different, usually much older and smaller plan on 20 of the 26 satisfiable cases they
finished: `requests` 1 package / 1 old pin instead of
5 / 0, `pydantic+pytest` 3 / 2 instead of 10 / 0, `boto3` 2 / 2 instead of 7 / 1. Their lower
node counts on real data are therefore not a fair win.

- **Most real instances are trivial**: C0 needs at most 10 nodes on 26 of 29, and its total
  warm time for all 29 is 1.2 s.
- **The synthetic `pinned_pair` family models something real.** `boto3` pins one `botocore`
  release per version, so with an old `botocore` pin C0 scans the releases: 1,217 nodes
  (`botocore<=1.22.7`), 2,123 (`==1.0.0rc1`), 430 for the real UNSAT pair. AC-3 needs 7, 6
  and 0.
- **AC-3 wins on nodes but not always on time.** Warm, the real UNSAT pair costs C0 0.14 s
  (430 nodes) and C1 0.17 s (0 nodes); it wins clearly only where C0 really scans
  (`<=1.22.7`: 0.89 s vs 0.15 s).
- **C2, C3 and C4 show no benefit on real data**: no real case needed them. The synthetic
  ablation is the only evidence for them. Warm totals: C0 1.15 s, C1 1.66, C2 1.63, C3 2.70,
  C4 2.40.
  (Those warm totals are from the 21:24 run, before any timeout; later runs include
  timeouts in the totals and are not comparable.)
- **Cold vs warm.** Any configuration with AC-3 pays a large fixed cost on every `boto3`
  case when the Graph's caches are cold: 4.6 to 6.8 s in the 21:24 run and 7.3 to 7.6 s in
  the 21:58 re-run (the machine was slower), against 0.02 s for C0 on `single:boto3`; warm it
  is about 0.15 to 0.4 s. On the cases where C0 itself scans, C0 is slow too (1.0 to 7.2 s
  cold). All of the fixed cost is `Graph.allowed()` filling its cache: 2,195 distinct
  requirement sets, each a `packaging` filter over about 2,400 versions. **That module is
  the data team's; a fast path for a single `==X` specifier would remove nearly all of it.**
- **Timeouts in the 21:58 re-run (10 s limit).** Every one happened at **0 nodes**, during
  AC-3's setup, never in search: `old:boto3<=1.24.4` under C2, C3, C4 and `C4-noLCV`, and the
  real UNSAT pair under C1 to C4. They are the cold `Graph.allowed()` cost on a slower
  machine, not a search problem; C0, which does no AC-3, solved all 28. Some of those runs
  also **overran the deadline** (C3 took 18.5 s), because AC-3 only checked the deadline in
  its propagation loop, not while building the closure. That is fixed: a 0.2 s timeout on
  an expensive closure now returns in 0.20 s, where the unfixed code took 9.0 s
  (`test_the_deadline_is_honoured_promptly_while_building_the_closure`). The re-run's
  timings predate the fix.

A flaw in our own first benchmark: configurations shared the Graph's caches, so whichever ran
first paid for filling them and AC-3 looked 25x slower than MRV (both run the same AC-3).
Every run now starts with empty caches and is repeated warm; the report shows both. **A
shared cache silently confounds timing comparisons.**

Caveats: a 30-seed pilot (76 packages), not the 5,000-seed dataset; easier than real
resolution; one machine, one run per case; **C4's learning has never been exercised on a hard
real conflict**.

---

## 7. State

| Stage | What | State |
|---|---|---|
| 0 | Contract (`problem.py`, `solver.py`, tests) | done |
| 1 | Plan checker, brute-force oracle, generator (Jas Simar) | done |
| 1 | C0 plain backtracking (Aahana) | done |
| 1 | Experiment harness and scale families (Parth) | done |
| 2 | C1 AC-3 (Jas Simar) | done |
| 2 | C2 MRV, C3 LCV (Parth) | done |
| 3 | C4 backjumping and nogoods; tight UNSAT conflict (Aahana) | done |
| 4 | Ablation with tables and figures (Parth) | done; 10x target met except `random_csp` size 12 |
| 4 | Real-data benchmark on the pilot | done; fixed the LCV cost; re-run with per-configuration plans and `C4-noLCV`; LCV picks old releases |
| 4 | Independent review | done; 2 code bugs and 3 tooling flaws fixed, documentation corrected |
| 4 | Hand the solver to Yash and Arrya | done for Yash: core extraction uses `solve` as its oracle |
| 4 | Core extraction (deletion, deletion+refine, QuickXplain) and minimality verifier (Yash) | done; `crux/explain/`, `docs/EXPLAIN.md` |
| 4 | Commit and push; final report; viva preparation | **not done** (section 3) |

Targets from the proposal (they can fail; report if missed): 100% SAT/UNSAT agreement with
the oracle on 1,000 random small instances and 0 invalid plans (met, with the generator
caveat in 5.3); median node reduction >= 10x C0 to C4 on hard instances (met except size 12);
solve rate >= pip's and median time <= 3x pip's (cannot be measured yet; needs the pip/uv
harness and the real corpus).

---

## 8. Open items and owners

**Ours (resolver engine)**
- Commit the work honestly in logical pieces; each member commits their own further work.
- Write the report's Individual Contributions and the honest reflection truthfully.
- Prepare for the viva: each of Aahana, Jas Simar and Parth Sureka studies their component
  (sections 5.2 to 5.7) and can explain its tests and its failure modes.
- Update `README.md`: it still says 111 tests and does not describe the resolver.
- Run C4 on a hard real conflict once Arrya's corpus exists.
- Decide whether the recommended configuration should drop LCV (it picks very old,
  dependency-free releases on real data); `C4-noLCV` is the obvious candidate.
- The deadline is checked before each assignment and inside AC-3 only. MRV's scan, LCV's
  scoring and C4's `_explain` do not check it, so a single step on a very large domain
  could overrun the timeout. Not triggered at pilot scale.
- `timeout_s=0` is not uniform across configurations (a root on a package without
  releases gives UNSAT without AC-3 and TIMEOUT with it). The contract allows this.

**Other teams**
- Yash: core extraction and the verifier are **done** (`crux/explain/`, `docs/EXPLAIN.md`).
  Two findings for the resolver team in `docs/EXPLAIN.md` section 9: the C4 preset's conflict
  is inflated by AC-3's justifications, and `learning.py` `_explain` costs about 8 s per
  solve on the real boto3/botocore conflict when AC-3 is off (98% of the time in a profile).
- Parth Gupta: the English renderer over `crux.explain.explain(problem).core`, the user study.
- Arrya: the hard-case corpus (at least 50 real pip-issue failures), the planted-conflict
  generator, the pip/uv harness.
- Ritika and Sumehra: a `==X` fast path in `Graph.allowed()` (about 4.6 s cold on `boto3`);
  the full 5,000-seed run (about 6.5 h) after deciding the JSON-fallback question (old wheels
  stored as zero dependencies; it changes the dataset); confirm and commit `snapshots/`
  (a candidate now exists locally, see section 4); a real
  `USER_AGENT` contact in `crux/data/config.py`; correct the pre-release sentence in
  `docs/HANDOVER.md`; LICENSE and CI.

---

## 9. How to work here

Conventions:
- Tests stay **offline**; use `Graph.from_dict` and the fixtures in `tests/fixtures/`.
- New packages go in `[tool.setuptools] packages` in `pyproject.toml` (flat-layout
  autodiscovery is disabled on purpose).
- Resolver code in `crux/resolver/`, tests in `tests/resolver/`.
- Go through `Graph.candidates()`, never `versions()`, when choosing versions.
- **A new configuration** must pass the same agreement tests as the others (oracle on 1,000
  seeds, plans through `check_plan`, conflicts sound): add it to `_CONFIGS` in
  `tests/resolver/test_backtrack.py`. The oracle cannot catch a bad *ordering or learning
  heuristic*, so also add direct behavioural tests like `test_ordering.py` and mutation-check
  them.
- No wall-clock assertions in correctness tests (they flaked on a busy machine).
- The checker and the oracle share one rule (`checker.admissible`), so a mistake there
  hides from both. Keep `test_admissible_matches_graph_candidates`' spec pool covering
  every operator, including `!=` against a pre-release.
- When comparing configurations, check they returned the **same plan** before comparing
  node counts (LCV can return a different, older one).
- Look at every generated figure before trusting it.

Layout (`crux/resolver/`): `problem.py`, `checker.py`, `oracle.py`, `gen.py`, `backtrack.py`,
`ac3.py`, `ordering.py`, `learning.py`, `solver.py`, `families.py`, `bench.py`, `ablation.py`,
`realbench.py`. Tests mirror them in `tests/resolver/`.

Environment notes (this machine, Windows):
- `git` is not on the PowerShell PATH; it is at `C:\Program Files\Git\cmd\git.exe`.
  Python is 3.12.4.

---

## 10. Change log

All entries are 2026-11-14 unless stated.

- **Stage 0.** `crux/resolver/` (`problem.py`, `solver.py`), `test_contract.py`, package
  registered in `pyproject.toml`.
- **Plan checker.** `checker.py`, `test_checker.py`; found the per-package pre-release rule.
- **Oracle.** `oracle.py`, `gen.py`, `test_oracle.py`; exposed `max_deps` after finding only 27%
  of first instances had any plan.
- **C0.** `backtrack.py`, `Problem.constraints()`, `test_backtrack.py`; removed the strict xfail.
- **Families and harness.** `families.py`, `bench.py`; re-tuned `random_csp` (it was trivially
  easy); `results/` gitignored.
- **C1.** `ac3.py`, `Stats.ac3_versions`, "ac3 pruned" column, `test_ac3.py`. AC-3 prunes
  nothing on `random_csp` and `thrash`.
- **C2 and C3.** `ordering.py`, `non_latest` metric, `test_ordering.py`. Studied MRV over 40
  seeds per size: medians, not sums.
- **C4.** `learning.py`, `thrash_wide`, `Config.nogoods`, `Stats.nogoods` and `backjumps`,
  `test_learning.py`; factored `_setup`, `req_info`, `last_fail` out of `backtrack.py`. Fixed
  the activating-decision bug; killed the surviving mutant with a unit test; removed wall-clock
  assertions from correctness tests.
- **Ablation.** `ablation.py` (`--from-rows`), `test_ablation.py`, the `plots` extra; ran the
  275-run ablation; fixed both figures and the "timed out" table cell.
- **Real data.** Built the pilot (reproduced the original), `realbench.py`,
  `test_realbench.py`, 145 real runs; fixed LCV (`_cost_cached`, 6.6 s to 0.25 s); fixed the
  shared-cache timing flaw; reported the `Graph.allowed()` cold cost.
- **This document** moved from the repo root to `docs/context.md` and rewritten: reorganised,
  stale items corrected, added the course-requirements status (section 3), the open-items list
  with owners (section 8).
- **Independent review.** Fixed the `!=` pre-release rule in `checker.py` (with an
  extended differential test and a regression test); added `tests/resolver/test_edge_fuzz.py`;
  made `reduction_stats`' lower bound valid (later-config timeouts count as 0) and the
  per-step table and figure show such losses; `bench.node_reduction` now counts
  other-config timeouts; `realbench` records plan size and non-latest pins per
  configuration, adds `C4-noLCV`, de-duplicates order-insensitively; `--from-rows` reports
  say they were regenerated; corrected docstrings (`backtrack.py`, `families.py`,
  `realbench.py`) and the overreaching claims in this document. AC-3 now checks the
  deadline while building its closure (it overran 10 s to 18.5 s on real data), with a
  regression test. Re-ran the real-data benchmark (168 runs) and regenerated the ablation
  report from its saved rows.
- **2026-11-15 (Yash).** Core extraction and the minimality verifier in `crux/explain/`, with
  `tests/explain/` and `docs/EXPLAIN.md`; `crux.explain` added to `pyproject.toml`. Clarified
  the conflict-size figure in 5.7 and updated sections 0, 7 and 8. No resolver code changed.
