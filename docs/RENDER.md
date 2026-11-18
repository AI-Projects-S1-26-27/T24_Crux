# Crux explanation messages and ideal explanations

Working context for the part of Crux that turns a minimal unsatisfiable core
into a message a developer can act on, the hand-written ideal explanations it
is compared against, and the comparison with pip's and uv's messages. It records what exists, why it is built the way it is, what was measured,
and what is still open. **When you change something here, update section 9
(open items) and section 10 (change log).**

Owner: **Parth Gupta** (English renderer, hand-written ideal explanations). Built 2026-11-16 on branch
`parth-gupta/english-renderer-ideals-user-study`, starting from `main` at
`5eb0d7a`.

---

## 0. At a glance

| What | Where | State |
|---|---|---|
| Core grouped into statements | `crux/explain/group.py` | done, tested |
| English renderer; `message(problem)` entry point | `crux/explain/render.py` | done, tested |
| Why a version is missing (`Graph.why_missing`) | `crux/data/graph.py` | done, tested on the pilot DB |
| Drop reasons copied into the real instances | `crux/benchmark/add_drops.py` | done; 43 of 61 instances |
| 30 hand-written ideal explanations | `benchmarks/ideal/` | written; **review pending** (`"reviewed": false`) |
| Renderer against the ideals | `crux/explain/ideal.py` | done |
| Crux's messages against pip and uv | `crux/benchmark/crux_messages.py` | done; results in `benchmarks/results/MESSAGES.md` |

**Headline facts (details and caveats below).**
- On all 259 benchmark conflicts (198 planted, 61 real), every message is
  *complete* and *checked*:
  - complete: every group of the core appears;
  - checked: every clash it states is confirmed against the graph.

  Every message also names every package of its core.
- On the real conflicts, the median message is **4 lines and 24 words**, against 10 and 79 for
  pip 26.2.1 and 6 and 40 for uv 0.11.32. On the planted conflicts it is 4 and 31, against 11 and
  83 for pip.
- That meets the proposal's target "at most half of pip's" as the benchmark defines a message.
  **It does not meet it** if pip's fixed advice block is left out of pip's count: Crux is then 55
  to 67% of pip's length (section 7.1).
- On deep planted conflicts:
  - Crux names the whole conflict at every depth, in 3 to 4 lines;
  - pip names 87% of it at depth 2 and 41% at depth 6;
  - uv names all of it, in 9 to 17 lines.
- Against the 30 ideal explanations, Crux names the requests to change in 30 of 30, at a similar
  length (median 197 characters against 190). The ideals always end with a fix and Crux never
  suggests one: the main gap (section 9).

---

## 1. What this component is

`crux.explain` (Yash Kejriwal) returns a minimal core: the smallest set of
requirements that cannot hold together. A core lists one constraint per release.
The proposal's A/B/C example is 11 constraints, and the real boto3/botocore conflict
is 432. A reader needs a few statements instead:
- the requests they wrote that cannot hold together;
- where those requests collide;
- why no version gets past the collision.

The pipeline:

```
explain(problem).core ──► group_core ──► render_groups ──► Message ──► text / wrapped(80)
                          (group.py)     (render.py)        .complete, .checked, .lines
```

`message(problem)` runs all of it and is the one call the CLI and the benchmark
make. Everything else in this document measures or tests those messages.

---

## 2. Running it

```bash
pip install -e ".[dev]"
python -m pytest -q tests/explain tests/benchmark          # all offline

# one message
python - <<'PY'
from crux.data.graph import Graph
from crux.resolver import Problem
from crux.explain import message
g = Graph.from_dict({"a": {"1.0": [["c", "<2.0"]]}, "b": {"1.0": [["c", ">=3.1"]]},
                     "c": {"1.0": [], "3.1": []}})
print(message(Problem.from_requirements(g, ["a", "b"]), width=80))
PY

# Crux's messages on the benchmark, measured like pip's and uv's (about 30 s)
python -m crux.benchmark.crux_messages            # -> benchmarks/results/MESSAGES.md

# the renderer against the ideal explanations
python -m crux.explain.ideal

# copy drop reasons into the real instances (needs the corpus database, 1.3 GB)
python -m crux.benchmark.add_drops --db data/corpus.db \
    --instances benchmarks/corpus/instances.jsonl.gz
```

---

## 3. The API (what other components call)

`from crux.explain import message, render, render_core, group_core, Group, Message`

| Call | Returns | Use |
|---|---|---|
| `message(problem, method="quickxplain", budget_s=60, width=None)` | `str` | **CLI and benchmark entry point.** Solves, extracts the core, renders it. With `width`, wrapped to that many columns. For SAT or TIMEOUT, a one-line status. |
| `render(problem, result, width=None)` | `str` | The same from an existing `CoreResult`. |
| `render_core(problem, core, minimal=True)` | `Message` | Group and render a core. |
| `render_groups(graph, groups, minimal=True)` | `Message` | Render already-grouped core groups. |
| `group_core(problem_or_graph, core)` | `tuple[Group, ...]` | The core as statements: roots first, then edges by depth. Order-independent. |
| `rules_out_package(groups, pkg)` | `bool` | Do the groups of *pkg* together constrain every release it may take? |
| `Graph.why_missing(dep, spec)` | `tuple[(version, reason), ...]` | Releases that would match but cannot be used, newest first (data layer; `docs/HANDOVER.md`). |

`Message` fields:
- `lines`: unwrapped, headline first;
- `text`;
- `wrapped(width)`;
- `complete`: every group appears on a line or on the way to one;
- `checked`: every clash was confirmed against the graph;
- `groups`;
- `statements`: the number of groups, the benchmark's "distinct requirement statements";
- `named`.

`Group` fields:

| field | meaning |
|---|---|
| `kind`, `dep`, `pkg`, `members`, `specs` | the requests on one package, or one (package, dependency) pair of edges |
| `range` | for requests, their intersection, or `None` if they contradict each other; for edges, the envelope of the members' ranges |
| `n_matching`, `lowest_match`, `highest_match` | the releases of `dep` that satisfy the group |
| `dep_known` | whether the graph has any release of `dep` |
| `missing`, `missing_reasons` | when nothing matches, why, from `why_missing` |
| `versions`, `n_allowed`, `covers_all`, `uncovered` | which releases of `pkg` the group covers |
| `depth` | 0 for requests; for edges, the distance of `pkg` from a request |

**For Sumehra (CLI).** Call `message(problem, width=80)` and print it. Printing it
wrapped at 80 is how the benchmark measures Crux. When the status is SAT, print the
plan instead. The message already says so on one line, but a CLI wants the plan.

---

## 4. Design

### 4.1 Grouping: one statement per kind of requirement, not per release

A core has one constraint per release, so it is grouped (`group_core`):
- one group per requested package: all the user's requests on it;
- one group per (package, dependency) pair of edges, **whatever their specifiers**.

Grouping by identical specifier would compress nothing on the boto3 conflict, where each of the
430 releases needs a different botocore range (`docs/EXPLAIN.md` 8.3). Instead the ranges are
summarised: `range` is the smallest single range containing them all, written with lower bounds
first (`>=1.0,<2.0`, not packaging's sorted `<2.0,>=1.0`).

The number of groups is exactly the benchmark's statement count (`real_cores.groups`). They
agree on all 61 real and 198 planted cores (`test_counts_and_coverage_match_the_benchmark`).

"Every allowed version of a" must be true before a message says it. `covers_all` compares the
group's releases with the releases the *core's own* constraints let the package take, because
that is what minimality is proven against. When releases of one package fail for different
reasons (bramble 3.3.1 through one dependency, 3.4.0 through another), no single group covers
them all. `rules_out_package` checks that the package's groups do so together. It holds for
every package in all 259 cores.

### 4.2 Targets and clauses

A **target** is a package that core requirements point at but that has no requirement of its own
in the core. It is where the conflict lands: `c` in the toy, `botocore` in the boto3 conflict,
`zinc` six levels down in `planted/deep/d6`. Every requirement on a target becomes one clause:

- **From a requested package:** the clause says which of its versions it covers. It reads "every
  allowed version of a (1.0 to 1.4, 5 releases)", "the only allowed version of glyph (1.1.0)",
  or "bramble 3.3.1" when only some releases are involved. A plural subject takes a plural verb.
- **From deeper in the graph:** the clause is credited to the request it comes from, with the
  packages in between: "violet>=1.1.1 needs zinc<1.4.1 (through helix → kestrel → pumice → pine →
  vapor)". The path is the shortest one through core edges. A step that covers only some releases
  is qualified ("through bramble 3.3.1").
- **The user's own request on a target:** "you asked for protobuf==6.30.2".

Clauses follow the order of the headline, so the line reads in the order the user wrote their
requirements.

### 4.3 What each line says

| shape | line |
|---|---|
| two clauses share no version | "*clause*, but *clause*." |
| three or more | "*a*, *b* and *c*; no version of T satisfies all of them." (the three-way planted conflicts are compatible pairwise and fail only together) |
| a clause no release satisfies | "*clause*, but *why* (section 4.4)." |
| the user's requests contradict each other | "no version of six satisfies both ==1.16.0 and ~=1.15.0." |
| the user asked for something that does not exist | the reason alone: the headline already names the request |

The headline names every request in the core: "No solution. Your requirements X and Y cannot
both hold:". These are what the user can change. A core whose minimality was not proven ends with
"(Crux could not prove that this is the smallest set of conflicting requirements.)".

Examples, as printed:

```
No solution. Your requirements a>=1.0 and b>=1.0 cannot both hold:
  every allowed version of a (1.0 to 1.4, 5 releases) needs c<2.0, but every
    allowed version of b (1.0 to 1.3, 4 releases) needs c>=3.1.
```
```
No solution. Your requirements garnet>=2.0.0,<2.0.1 and violet>=1.1.1 cannot
both hold:
  garnet>=2.0.0,<2.0.1 needs zinc>=2.1.0 (through sorrel → dahlia → heron), but
    violet>=1.1.1 needs zinc<1.4.1 (through helix → kestrel → pumice → pine →
    vapor).
```
```
No solution. Your requirements bramble>=3.3.1 and cinder>=1.0.0 cannot both
hold:
  bramble 3.4.0 needs egret>=1.4.0, but no release of egret matches (the newest
    in the snapshot is 1.3.0).
  bramble 3.3.1 needs ochre<1.2.1, but every allowed version of cinder (1.0.0,
    2.0.0 and 2.0.1) needs ochre>=1.2.3.
```
```
No solution. Your requirement khoj cannot be met:
  every allowed version of khoj (1.17.0 to 1.42.10, 84 releases) needs
    docx2txt==0.8, but docx2txt 0.8 has no wheel for CPython 3.12 on Linux
    x86-64.
```

### 4.4 Why a version is missing

The core says *that* no release matches, not *why*. The reason (no wheel for the target, yanked,
wrong Python, uploaded after the cutoff) is in the data layer's `drops` table, and the graph did
not expose it (`docs/HANDOVER.md`). `Graph.why_missing(dep, spec)` now does:

- **What it reads:** only `ingest` and `clean` drops. `edge` drops are Requires-Dist lines, not
  releases.
- **Other reasons:** `after_as_of` for a release left out by `load(as_of=...)`, and `prerelease`
  for a pre-release the specifier does not opt into.
- **When it reads:** lazily, so `load()` is unchanged.
- **Without a database:** a database without `drops` gives `()`. A dict-built graph knows only
  what is passed as `drops=`.

On the pilot database it reports every one of its 1,522 ingest drops correctly, and never reports
a release that is in the graph.

The benchmark's real instances are small graphs cut out of the corpus database, so their reasons
were left behind. `add_drops` copies back exactly what a message can need into `meta["drops"]`:
for every requirement nothing in the instance meets, the matching versions and their reasons.
Rewriting the file without changes gives identical bytes, so `drops` is the only change.

When one version has two reasons, it is counted once, and the data layer's own reason is given
(`one_reason_per_version`). Counting reasons instead of versions was a bug the corpus database
exposed ("none of the 2 matching releases" for one release).

One real message still has no reason: the version it asks for never existed on PyPI, and the
message says so ("no release of X matches (the newest in the snapshot is ...)").

### 4.5 Safeguards: never hide part of the conflict

- `Message.complete`: every group is on a line or on the path to one. A group that is neither
  gets an "Also involved: ..." line, so a message can be long but never silently incomplete.
- `Message.checked`: every clash line is confirmed, because the clauses' version sets
  really have an empty intersection in the graph.

Both are true on all 259 benchmark cores. The fallbacks exist for cores that are not clean, and
are tested with deliberately redundant or satisfiable "cores".

### 4.6 Measuring fairly

- **Wrapping.** Unwrapped lines run to about 150 characters, while uv wraps at about 80 and pip
  does not wrap. So Crux is measured as printed at 80 columns, and **words** are counted for every
  tool, since words do not depend on wrapping.
- **Same code, same instances.** pip's and uv's numbers are re-measured from the output the
  harness saved, with the same `messages.measure`. The medians are over instances where all four
  tools gave a message.
- **pip's fixed advice.** pip ends every message with the same "To fix this you could try to:"
  block and a help link. The headline comparison counts pip's whole message, as `messages.py`
  defines it. The stricter count without that block is reported next to it (section 7.1).
- **Statements.** Crux's statement count is its number of groups. pip's and uv's are counted from
  their text, as before. These are different measures and are not compared directly.

### 4.7 Ideal explanations

`benchmarks/ideal/ideal.jsonl` holds 30 cases:
- the toy example;
- 17 clean real conflicts, one of each shape;
- 3 real conflicts with a missing version (no wheel, yanked, released after the report);
- 9 planted conflicts, one of each kind.

The writing rules are in `benchmarks/ideal/README.md`. Each was written from the instance's
requirements, core and graph:
- it leads with the requests in the conflict;
- it names the collision in terms a developer recognises ("numpy 2", "a 2025 certifi");
- it ends with a fix that works in the snapshot, or says there is none.

Every fact in them is checked against the data, so wording can be wrong but facts cannot:
- every case is unsatisfiable;
- `change` equals the requests in the minimal core;
- every proposed fix makes the problem installable, with the plan checked independently;
- the three "no fix" cases have no single-request fix.

---

## 5. Files

| File | What |
|---|---|
| `crux/explain/group.py` | `group_core`, `Group`, `rules_out_package`; range helpers (`spec_bounds`, `envelope`, `intersection`) |
| `crux/explain/render.py` | `message`, `render`, `render_core`, `render_groups`, `Message`; reason wording (`REASONS`, `TARGET`) |
| `crux/explain/ideal.py` | loads the ideals, applies fixes, compares the renderer (CLI) |
| `crux/data/graph.py` | `why_missing`, `from_dict(drops=)` (additive; data layer) |
| `crux/benchmark/instance.py` | `Instance.problem` passes `meta["drops"]` to the graph |
| `crux/benchmark/add_drops.py` | copies drop reasons into the real instances (CLI) |
| `crux/benchmark/messages.py` | `words` measure and the message rule for Crux (additive) |
| `crux/benchmark/crux_messages.py` | Crux's messages on the benchmark; writes `MESSAGES.md` (CLI) |
| `benchmarks/ideal/` | `ideal.jsonl`, `README.md` |
| `benchmarks/results/MESSAGES.md`, `*/crux_messages.jsonl` | the message comparison and every Crux message |

---

## 6. Testing

| file | tests | what |
|---|---|---|
| `tests/explain/test_group.py` | 39 | grouping, ranges, covers-all, missing reasons; counts match the benchmark on 259 cores |
| `tests/explain/test_render.py` | 33 | golden messages for every shape; plural verbs; order; wrapping; the fallbacks; every benchmark message complete, checked and naming its core |
| `tests/explain/test_ideal.py` | 123 | every ideal's facts against the data, fixes re-solved and plan-checked |
| `tests/data/test_graph.py` | 22 (8 new) | `why_missing` from dicts and databases, `as_of`, edge drops ignored |
| `tests/benchmark/test_crux_messages.py` | 8 | Crux, pip and uv measured by one function on one instance with real pip and uv output |
| `tests/benchmark/test_add_drops.py` | 5 | only unmet requirements looked up; nothing else changes; idempotent |

**Deliberate breaks.** For each module, the code was broken on purpose and the tests were re-run.
Every break below is caught; each one is listed with the problem it introduced.

- **Grouping (6):**
  - roots grouped by specifier;
  - wrong `~=` bound;
  - `covers_all` always true;
  - a wrong depth;
  - roots matched by union instead of intersection;
  - a wrong envelope tie rule.
- **Rendering (7):**
  - no target detection;
  - no version counts;
  - singular verbs;
  - clauses sorted alphabetically;
  - no "also involved" fallback;
  - clashes never checked;
  - chain qualifiers ignored.
- **`why_missing` (4):**
  - edge drops included;
  - usable pre-releases reported;
  - wrong order;
  - `as_of` ignored.

  A fifth break, dropping `prereleases=True`, makes no difference under the pinned
  `packaging==26.0`, which already matches pre-releases by default. The flag is kept so the
  behaviour stays pinned.
- **Ideal data (4):**
  - a wrong fix;
  - a wrong `change` list;
  - a "fix" for an unfixable case;
  - a changed provenance.

---

## 7. Results

### 7.1 Message size and content (`benchmarks/results/MESSAGES.md`)

Real conflicts, all 61:

| tool | lines (median) | words (median) | names every request in the conflict | core recall | core precision |
|---|---|---|---|---|---|
| pip 26.2.1 | 10 | 79 | 36/36 | 1.00 | 0.95 |
| pip 25.0.1 | 8 | 65 | 36/36 | 1.00 | 0.95 |
| uv 0.11.32 | 6 | 40 | 36/36 | 1.00 | 1.00 |
| **Crux** | **4** | **24** | **36/36** | **1.00** | **0.99** |

Planted conflicts (169 where all four tools gave a message; pip 26.2.1 timed out on the other
29): Crux 4 lines and 31 words, pip 26.2.1 11 and 83, uv 11 and 82. pip 26.2.1 names every
request in the conflict in 107 of 169, uv and Crux in 169 of 169.

- **"Names every request" and recall on real conflicts** are scored on the 36 whose core is proven
  the only minimal one.
- **Crux's 0.99 precision** is one instance with a package literally called `wheel`: the reason
  "has no wheel for CPython 3.12" counts as naming it. The rule is the same for every tool, so it
  is left as measured.
- **Without pip's fixed advice**, pip's median is 6 lines and 44 words on real conflicts (7 and 47
  planted). Crux is 55 to 67% of that: shorter, but not half.

### 7.2 By depth (deep planted conflicts)

Share of the conflict's packages named / median lines:

| depth | pip 26.2.1 | pip 25.0.1 | uv 0.11.32 | Crux |
|---|---|---|---|---|
| 2 | 0.87 / 11 | 0.96 / 12 | 1.00 / 9 | 1.00 / 3 |
| 3 | 0.67 / 11 | 0.73 / 10 | 1.00 / 12 | 1.00 / 3 |
| 4 | 0.53 / 10 | 0.59 / 8 | 1.00 / 14 | 1.00 / 3 |
| 5 | 0.45 / 10.5 | 0.56 / 8 | 1.00 / 14 | 1.00 / 3 |
| 6 | 0.41 / 10 | 0.46 / 8 | 1.00 / 17 | 1.00 / 4 |

uv explains the whole derivation and grows with depth. Crux states only where the conflict lands
and through which packages, so its length stays flat.

### 7.3 Against the ideal explanations (`python -m crux.explain.ideal`)

- Crux names the requests to change in 30 of 30 cases.
- It names every core package the ideal names, in 30 of 30.
- Median length: Crux 197 characters, the ideals 190.

The difference is content. Every ideal with a fix ends with one, such as "pin protobuf below 6:
5.29.4 works", and Crux never suggests one.

---

## 8. Against the proposal's targets

| Measure (proposal 3.6) | Target | Result |
|---|---|---|
| Message size | median at most half of pip's on the hard corpus | **Met** as the benchmark defines a message (real 4 vs 10 lines, 24 vs 79 words; planted 4 vs 11, 31 vs 83). **Not met** without pip's fixed advice block (Crux 55 to 67% of pip). Both are reported |
| Message names what to change | (renderer design) | Every request in the conflict is named in every message (259 of 259; 36 of 36 unique real cores, 169 of 169 planted) |
| 30 hand-written ideal explanations | written | 30 written and checked against the data; review by a second team member pending |

---

## 9. Open items and handover

**Mine, still open.**
- **Review the ideal explanations** and set `"reviewed": true`.
- **Suggest a fix in the message.** The ideals' main advantage, for example "pin protobuf below 6:
  5.29.4 works". A candidate fix can be checked the same way `test_ideal.py` checks the ideals'
  fixes, by solving again with the requirement relaxed. Not done.
- **"(the exact range differs by release)"** is honest but long. A shorter summary, such as the
  lowest bound only, needs care to stay true.

**For Sumehra (CLI).** `message(problem, width=80)` (section 3). The data layer gained
`Graph.why_missing` and `from_dict(drops=)`. Both are additive and documented in
`docs/HANDOVER.md`.

**For Arrya (benchmark).**
- `benchmarks/corpus/instances.jsonl.gz` gained `meta["drops"]` on 43 instances and nothing else.
  Rewriting it without changes is byte-identical.
- `messages.py` gained a `words` measure and a message rule for Crux.
- `docs/BENCHMARK.md` now points to `MESSAGES.md`.

**For the report.** Quote both message-size counts, with and without pip's advice.

---

## 10. Change log

- 2026-11-16: grouping (`df9667e` `why_missing`, `ef9789c` grouping), renderer
  (`db26b15`), ideal explanations (`5ac72e1`), message comparison (`47251f1`),
  drop reasons in the real instances (`4654e6c`).
