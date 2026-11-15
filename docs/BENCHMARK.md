# Benchmarking: planted conflicts, real failures, pip and uv

Owner: Arrya Sridhar. Code in `crux/benchmark/`, tests in `tests/benchmark/`,
data and results in `benchmarks/`. Results with every number are in
`benchmarks/results/REPORT.md`, generated from the CSV files by
`python -m crux.benchmark.report`; this document explains the method and
quotes the headline numbers.

## What this measures, and what it does not

It measures, on identical metadata for every tool:

- whether pip, uv and Crux reach the right answer within 60 s (solve rate),
- how long they take end to end,
- what their error messages contain: length, whether they name the packages
  in the true conflict, whether they name what the user asked for, and
  whether they state anything false,
- how close each Crux configuration's reported conflict is to the true
  minimal core.

It does not measure Crux's *message*. The English renderer and core
extraction (Parth Gupta, Yash) are not built yet. Crux is compared on
answers, time and the size of the conflict it reports. `benchmarks/planted/`
holds the ground truth that core extraction will be scored against
(exact match on planted cores) once it exists.

## Quick start

```bash
pip install -e ".[dev,plots]"
python -m pytest -q tests/benchmark                     # offline

# planted instances: regenerate, or re-prove the committed file
python -m crux.benchmark.planted --out benchmarks/planted/instances.jsonl
python -m crux.benchmark.planted --verify benchmarks/planted/instances.jsonl

# the comparison (needs a CPython 3.12 with pip==26.2.1, a pip==25.0.1, uv==0.11.32)
python -m crux.benchmark.harness --instances benchmarks/planted/instances.jsonl \
    --out benchmarks/results/planted --pip-python PY312_PIP26 \
    --pip-baseline-python PY312_PIP25 --uv UV
python -m crux.benchmark.report --planted benchmarks/results/planted \
    --real benchmarks/results/real --corpus benchmarks/corpus --out benchmarks/results
```

Real-failure corpus (network): `corpus_collect` (GitHub search),
`crux.benchmark.corpus` (screening, offline), `corpus_ingest` (PyPI, via the
data layer), `replay`. Commands in section 3.

## 1. Instance format (`crux/benchmark/instance.py`)

One JSON object per line. `graph` is exactly what `Graph.from_dict` reads,
`roots` the requested packages, `expected` SAT/UNSAT/null, `core` the planted
minimal core as a list of the resolver contract's `Constraint` fields, `meta`
provenance. A core is a set of `Constraint` values, so comparing a computed
core with the truth is set arithmetic. Cores list one constraint **per
version**, as the CSP in the proposal does: the proposal's A/B/C example has
a core of 11 constraints (2 requests, 5 edges from `a`, 4 from `b`), and
grouping them into "every version of a" is the renderer's job.

## 2. Planted conflicts (`crux/benchmark/planted.py`)

198 unsatisfiable instances. Each hides one conflict ("gadget") inside
optional harmless background packages.

| kind | the conflict |
|---|---|
| `direct` | two requirements need disjoint ranges of one package |
| `deep` | the same, reached through chains of packages (depth 2 to 6) |
| `impossible` | a chain ends in a requirement nothing meets: no releases at all, or none new enough (depth 0 to 3) |
| `partial` | older releases of A clash with B; newer releases of A need a release of D that does not exist |
| `threeway` | three requirements on one package, every pair compatible, all three not |

Background: none, small (6 to 10 packages) or large (25 to 40). Background
packages form a dependency DAG with a known working version each, and
gadget releases get extra dependencies on them, so the conflict is buried
among constraints that do not matter. Background release edges average
about 2 with a quarter of releases having none, roughly the pilot's 2.2 and
25% (`docs/PILOT.md`); this is a rough shape match, not a fitted model.

### Why the planted core is the right answer, and the only one

A minimal unsatisfiable core is not unique in general, so "the core" needs a
guarantee. For an instance I and a constraint set K:

> K is the only minimal unsatisfiable subset of I **iff** K alone is
> unsatisfiable **and**, for every k in K, I without k is satisfiable.

(If a minimal core M missed some k in K, M would sit inside I − {k}, which is
satisfiable: impossible. So every minimal core contains K, and K is itself
unsatisfiable, so the minimal core is K.)

`prove()` checks both halves **without trusting any solver under test**:
"K alone is UNSAT" by the brute-force oracle (exhaustive, shares no code
with the solvers); "I − {k} is SAT" by an explicit plan that the independent
checker accepts. A valid plan is a certificate whoever found it. Mutation
tests show the proof rejects a core with one constraint too many or too
few.

Consequence: every *sound* conflict a solver reports must contain K, since
every unsatisfiable subset contains a minimal core. The harness checks this
for C0 to C4 on all 198 instances.

## 3. Real failures (`corpus_collect.py`, `corpus.py`, `corpus_ingest.py`, `replay.py`)

**Collection.** GitHub issue search, saved verbatim with the fetch time and a
hash of each post:

- pip's tracker, `ResolutionImpossible`, any date;
- uv's tracker, `"No solution found when resolving"`, 2024-01 to 2026-10;
- issues in any GitHub project quoting pip's `ResolutionImpossible`,
  2024-01 to 2026-10, month by month (search returns at most 1,000 per query).

**Deviation from the proposal, to report.** The proposal names "the pip issue
tracker and public Q&A threads". pip's own tracker has only 23 matching
reports since 2024, too few for 50 cases, and the snapshot models CPython
3.12 (released October 2023), so most older reports cannot be replayed.
The search was widened to uv's tracker and other projects' issues. Stack
Overflow was not reachable from the environment this was built in. Only each
issue's opening post is used (comments were not accessible).

**Screening** (offline, `crux.benchmark.corpus`). The request is the last
install command before the error in the same code block when every argument
is a plain requirement (method B, the user's whole request); otherwise pip's
own "The user requested ..." lines from the first conflict report in the
post (method A; pip lists only the requirements directly involved, so this
may be part of the request). Every dropped issue gets exactly one reason:
`no_request`, `constraint`, `local_or_url`, `requirements_file`,
`non_pypi_index`, `unsupported_flag`, `unparseable`, `extras`, `marker`,
`local_version`, `duplicate`, `after_cutoff`.

**Replay.** The data layer's own `run_ingest` and `clean`, unchanged, fetch
every requested package's closure into a separate database (same filters,
same cutoff). Each case is replayed on the snapshot **as of the report
date** (`Graph.load`'s `as_of` rule) and solved by Crux. Outcomes:

| outcome | meaning |
|---|---|
| `not_on_pypi` / `ingest_error` | a requested package could not be fetched |
| `root_unavailable` | a requested package has no release meeting the request at that date, most often no CPython 3.12 wheel: a different failure from the user's, so not counted |
| `not_reproduced` | a plan exists on this data |
| `timeout` | Crux did not finish |
| `missing_dependency` | unsatisfiable only because some dependency has no usable release (switching off edges into release-less packages gives a plan): a data-layer artefact, not counted |
| `reproduced` | still unsatisfiable with those edges off: a genuine conflict among the requested packages on real metadata |

`same_packages` records whether a reproduced conflict involves a package pip
named in the reporter's own conflict lines: a rough check that the replay is
the user's failure and not merely *a* failure.

## 4. The harness (`harness.py`, `wheels.py`, `messages.py`, `run_crux.py`)

**Identical metadata.** Each instance's graph is written as metadata-only
wheels (METADATA, WHEEL, RECORD; tag `py3-none-any`; no Requires-Python,
because the data layer already applied the CPython 3.12 / Linux x86-64
filter). pip and uv read the folder with `--no-index --find-links`: no index
server, no network, the same releases and edges Crux sees. This is the
per-instance form of the proposal's local index mirror.

**Environment.** pip runs under CPython 3.12 on Linux x86-64, the snapshot's
target (pip resolves for the machine it runs on). uv is told the target
(`--python-version 3.12 --python-platform x86_64-unknown-linux-gnu`). Every
subprocess gets a clean environment: no user configuration, no colour.
Versions: pip 26.2.1 (current; the main baseline), pip 25.0.1 (a version
check, see below), uv 0.11.32.

**Passes.** (1) Outcome: every tool once per instance, two at a time; full
output kept; a timeout is re-run alone before it counts. (2) Timing: every
run that finished, 5 more times, one at a time; median reported. Crux's
timing runs in a fresh process (`run_crux`), like pip and uv, so all three
include interpreter start-up. (3) Crux configurations C0 to C4 and
C4-noLCV in-process, each on a fresh `Graph` (shared caches confound
timings, `docs/context.md` 6.3), 10 s limit as in the team's ablation.

**Message definitions** (`messages.py`, fixed before measuring):
pip's message is everything from its first `ERROR:` line to the end of its
output, with stdout and stderr merged in printed order (pip writes `ERROR:`
to stderr but "The conflict is caused by:" to stdout); uv's is its stderr.
`msg_lines` counts non-empty message lines, `out_lines` all output lines,
`statements` the requirement statements listed, `core_recall` /
`core_precision` the planted-core packages the message names (package level),
`names_root` whether it names a requested package, and `false_unavailable`
pip 26's "no matching distributions available" line naming a package that has
releases.

**Why that pip line counts as false.** pip prints a package there when
`Factory._has_any_candidates()` is False, whose docstring is "Check if there
are any candidates available for the project name": any release at all
(pip 26.2.1, `pip/_internal/resolution/resolvelib/factory.py`). The same line
appears against **real PyPI**: `pip install --dry-run boto3==1.34.0
botocore==1.29.0` with pip 26.2.1 lists `botocore`, which has thousands of
releases; pip 25.0.1 does not print the section. **This is a known, open pip
bug**, [pypa/pip#14193](https://github.com/pypa/pip/issues/14193) ("The 'no
matching distributions' hint is shown for packages that are installable"),
with a fix proposed in [pypa/pip#14194](https://github.com/pypa/pip/pull/14194);
both were open when checked on 2026-11-14. We measure how often it appears in
our benchmark; we did not discover it, and the report must say so.

**Why two pip versions.** On identical wheels in an identical environment,
pip 25.0.1 answered one planted instance in 0.4 s and pip 26.2.1 did not
finish in 45 s: it backtracked through background packages unrelated to the
conflict, printing "This is taking longer than usual". pip's behaviour on
hard cases depends on its version, so both are reported.

## Results

All numbers below are copied from `benchmarks/results/REPORT.md`, which the
report script computes from the result CSVs; check them there. pip 26.2.1
and 25.0.1 on CPython 3.12.13, uv 0.11.32, 2-CPU Linux x86-64 VM, 60 s
limit, timings the median of 5 sequential runs (Crux end to end, in a fresh
process, like pip and uv).

### Planted conflicts (198, each proven unsatisfiable with a unique core)

**No tool and no Crux configuration ever reported an install plan** for these
unsatisfiable instances.

| | answered "no solution" within 60 s | median time |
|---|---|---|
| Crux C4-noLCV | 198/198 | 0.08 s |
| uv 0.11.32 | 198/198 | 0.08 s |
| pip 26.2.1 | **169/198** (29 timeouts) | 0.34 s (p95 1.78 s) |
| pip 25.0.1 | 198/198 | not timed |

pip 26.2.1's timeouts are all on instances with background packages: 57/66
answered with a small background, **46/66 with a large one**. It
backtracks through background packages unrelated to the conflict.

**Messages.** uv's messages name every package of the true conflict (mean
recall 1.00, precision 1.00) and always name what the user asked for. pip's
name less of the conflict (mean recall 0.82 for pip 26.2.1, 0.78 for
25.0.1) and do not always name a requested package (140/169 and 147/198).
**121 of pip 26.2.1's 169 messages (72%) contain the false "no matching
distributions" line** (pypa/pip#14193); pip 25.0.1's never do.

**Depth is where the tools differ** (`deep` instances, depth 2 to 6): uv's
message grows from 9 to 17 lines and names the whole conflict at every
depth; pip 26.2.1's stays at 10 or 11 lines but names 0.87 of the conflict's
packages at depth 2 and **0.41 at depth 6** (pip 25.0.1: 0.96 to 0.46). pip
is not shorter because it is clearer: it leaves the conflict out. This is
the gap a minimal-core explanation is meant to close (figure
`message_lines_by_depth.png`).

**Crux configurations** (in-process, 10 s, fresh graph each):

| config | answered | timeouts | median nodes | conflict contains the core | conflict IS the core | conflict / core size |
|---|---|---|---|---|---|---|
| C0 | 161/198 | 37 | 38 | 100% | 66/161 | 1.80 |
| C1 | 166/198 | 32 | 20 | 100% | 66/166 | 1.52 |
| C2 | 197/198 | 1 | 10 | 100% | 66/197 | 1.50 |
| C4 | 198/198 | **0** | 8 | 100% | **168/198 (85%)** | **1.00** |

Every conflict from every configuration contains the planted core, as
soundness requires (198 instances x 6 configurations). C0's timeouts
concentrate in large noise (31/66). **C4's conflict is exactly the true
minimal core on 85% of instances**, before any core extraction: a strong
starting point for the explanation module.

### Real failures

5,275 issues collected; 472 requests kept and verified against their posts;
**61 conflicts reproduced** on the snapshot as of each report date (the
proposal's target was 50). Of the 33 reproduced cases whose post includes
pip's conflict lines, pip on our replayed data blames a package pip blamed
for the user in **32**. The other 28 are genuine conflicts on real metadata
but not shown to be the reporter's own.

| | answered "no solution" within 60 s | median time | p95 time |
|---|---|---|---|
| Crux C4-noLCV | 61/61 | 0.20 s | **11.91 s** |
| uv 0.11.32 | 61/61 | 0.10 s | 0.27 s |
| pip 26.2.1 | 61/61 | 0.41 s | 1.15 s |
| pip 25.0.1 | 61/61 | not timed | not timed |

All four tools agree on all 61. Messages: uv median 6 lines; pip 26.2.1
10 (16 lines of output in all); pip 25.0.1 8 (14). **pip 26.2.1's false
"no matching distributions" line appears in 50 of 61 (82%).**

**Crux on real hard conflicts** (open item 4 of `docs/context.md`):

- **AC-3 matters on real data.** On 5 real conflicts plain backtracking (C0)
  timed out after 125,864 to 971,446 nodes; C1 answered with 0 search nodes.
  C2 to C4 add nothing measurable on top of AC-3 here.
- **Crux's slow tail is setup, not search.** All 25 timeouts of C1 to C4 at
  10 s happened at 0 search nodes, on the 5 largest graphs (27,757 to 65,127
  versions; 93,773 to 280,204 edges). End to end these take 10.8 to 19.2 s
  (median of 5 runs). Profiling one of them (`real/rajpurkarlab/CRIMSON#3`,
  C4-noLCV, cProfile): 99.8% of the time is AC-3's setup and **92% is inside
  `Graph.allowed()`** (98,053 calls), almost all of it in `packaging`'s
  specifier filtering, about 30% in `==` comparisons. This is the cold
  `Graph.allowed()` cost `docs/context.md` reports on the pilot, at a larger
  scale; the `==X` fast path proposed there targets part of it.

### Against the proposal's targets

| target | status |
|---|---|
| Solve rate at least pip's | **Met**: planted 198/198 vs pip 26.2.1's 169/198 (pip 25.0.1: 198/198); real 61/61 each |
| Time within 3x of pip's median *on satisfiable cases* | **Not measured here**: every benchmark instance is unsatisfiable. On these, Crux's median is below pip's (0.08 vs 0.34 s planted; 0.20 vs 0.41 s real), but its 95th percentile on real conflicts is about 10x pip's |
| Core minimality 100%, exact match on 95% of planted | **Waiting for core extraction** (not built). Ground truth is ready; C4's raw conflict already equals it on 85% |
| Jaccard >= 0.8 on real cores | **Not possible yet**: needs core extraction and hand-annotated cores |
| Message size at most half of pip's | **Crux's message is not built.** Baselines measured: uv's median is 6 lines and pip 26.2.1's 10 on real conflicts |
| User study | **Not run.** `messages.jsonl` holds every pip and uv message for it |



## Limitations

- **The data is easier than real resolution** (from the data layer): extras
  are dropped (over half of all Requires-Dist lines), sdist-only releases are
  dropped, `requires_dist: null` is read as no dependencies, one wheel per
  version. pip and uv are run on the same reduced data, so the comparison is
  fair, but no claim here covers extras or source builds.
- **Planted background is synthetic.** Its shape roughly matches the pilot's
  averages; it is not sampled from real graphs.
- **Real cases come mostly from opening posts in other projects' issues**, not
  from pip's tracker as the proposal planned (section 3). Method A requests
  can be only part of what the user asked for.
- **Timing** is on a shared 2-CPU cloud VM; a niced, network-bound data
  download ran during part of the planted timing pass. Medians of 5
  sequential runs; solve rate is the primary speed measure, as the proposal
  says.
- **Crux's message is not measured**: the renderer does not exist yet.
