# Ideal explanations

`ideal.jsonl` holds 30 hand-written messages: what a developer should ideally
read for each conflict. They are the target the renderer is compared against
(`python -m crux.explain.ideal`) and the source of ground truth for the user
study.

## Cases

| | count | chosen for |
|---|---|---|
| toy | 1 | the proposal's A/B/C example |
| real | 17 + 3 | 17 of the 31 real conflicts that are robust (no missing version) and have a unique minimal core, covering each shape: two pins on one package, a dependency against a pin, a conflict one package removed, many releases on one or both sides; plus 3 that rest on a missing version (no wheel, yanked, released after the report) |
| planted | 9 | one of each kind (direct, deep at depths 2, 4 and 6, impossible at depths 0 and 2, partial, three-way) and one deep conflict buried in a large background graph |

## Fields

| field | meaning |
|---|---|
| `id` | `ideal-01` .. `ideal-30` |
| `instance` | `toy_abc` (`tests/fixtures/toy_abc.json`) or a benchmark instance id |
| `source` | `toy`, `real` or `planted` |
| `requirements` | the requests, exactly as the instance has them |
| `change` | the requests in the conflict, i.e. the ones a reader could change. Correct answers for the user study's "which requirement would you change?" |
| `ideal` | the message, at most three lines |
| `fixes` | changes that make the requests installable, each `{"replace": {old: new}}` or `{"remove": [req]}`; empty when no fix exists inside the snapshot |
| `notes` | why the case is in the set |
| `provenance` | `hand-written` |
| `reviewed` | set to `true` once a second team member has checked the text |

## How they were written

From each instance's requirements, minimal core and graph, not from the
renderer's output. Rules:

- Lead with the requests that cannot hold together, by the names the user
  wrote; leave out requests and packages that are not part of the conflict.
- Say what collides in versions a developer recognises ("numpy 2", "a 2025
  certifi"), with the exact range once.
- End with a fix that works in the snapshot, or say there is none.
- At most three lines.

## What the tests check (`tests/explain/test_ideal.py`)

Every case loads and is unsatisfiable; `requirements` match the instance;
`change` equals the requests in the minimal core; every fix makes the problem
satisfiable, with the plan checked independently; each ideal is at most three
lines and names every request in `change`. The wording itself is not checked:
that is what the review is for.
