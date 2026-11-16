"""Analysis of the user study, exactly as ``study/PROTOCOL.md`` section 7 fixes it.

Input: one JSON file per participant, as ``study/form.html`` saves it
(``study/responses/crux-study-pNN.json``), scored against ``study/items.json``
and checked against ``study/assignments.csv``.

Per participant and style: Q1 accuracy (share correct; "I can't tell" is
wrong) and Q1 time (median ``q1_ms`` over *correct* answers; missing if
there are none).  Four paired Wilcoxon signed-rank tests, exact, two-sided:
Crux vs pip and Crux vs uv, on time and on accuracy, Holm-corrected at
alpha = 0.05.  H1 (Crux at least 30% faster than pip) and H2 (at least 15
points more accurate) are supported only when the test is significant after
correction and the effect reaches the target.  A Q1 time above 5 minutes is
an interruption and counts as missing.

    python -m crux.benchmark.study_analysis --responses study/responses --out study/RESULTS.md

Before there is data, ``--simulate N`` writes N made-up participants to a
separate folder to exercise the pipeline.  Every simulated file is marked
``"simulated": true``, and a report on them says SIMULATED at the top;
simulated and real files are never analysed together.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from crux.benchmark.study import STYLES

ALPHA = 0.05
TARGET_FASTER = 0.30        # H1: at least 30% faster than pip
TARGET_POINTS = 15.0        # H2: at least 15 percentage points more accurate
MAX_MS = 5 * 60 * 1000      # longer is an interruption
NONE = "I can't tell"
ROOT = Path(__file__).resolve().parent.parent.parent
STUDY = ROOT / "study"
NAMES = {"pip": "pip 26.2.1", "uv": "uv 0.11.32", "crux": "Crux"}


# ── statistics ──────────────────────────────────────────────────────────

def _ranks(values: list[float]) -> list[float]:
    """Ranks 1..n, ties given their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


@dataclass
class Wilcoxon:
    n: int              # pairs with a non-zero difference
    w_plus: float       # sum of ranks of positive differences
    p: float            # exact two-sided p-value (1.0 when n == 0)


def wilcoxon(diffs) -> Wilcoxon:
    """Exact two-sided Wilcoxon signed-rank test on paired differences.

    Zero differences are dropped (Wilcoxon's original rule); ties among the
    absolute differences get average ranks, and the null distribution is
    computed exactly for those ranks (every sign pattern equally likely), so
    the p-value stays exact with ties.  Ranks are doubled to make them
    integers for the count."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return Wilcoxon(0, 0.0, 1.0)
    ranks = _ranks([abs(x) for x in d])
    w = sum(r for r, x in zip(ranks, d) if x > 0)
    twice = [int(round(2 * r)) for r in ranks]
    total = sum(twice)
    counts = [0] * (total + 1)            # counts[s]: sign patterns with 2*W+ = s
    counts[0] = 1
    for r in twice:
        for s in range(total, r - 1, -1):
            counts[s] += counts[s - r]
    obs = int(round(2 * w))
    all_ = 2 ** n
    lower = sum(counts[: obs + 1]) / all_
    upper = sum(counts[obs:]) / all_
    return Wilcoxon(n, w, min(1.0, 2 * min(lower, upper)))


def holm(pvalues: dict) -> dict:
    """Holm-adjusted p-values for {name: p}."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m, out, running = len(items), {}, 0.0
    for i, (name, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[name] = running
    return out


# ── loading and scoring ─────────────────────────────────────────────────

def load_items(path=STUDY / "items.json") -> dict:
    return {it["id"]: it for it in json.loads(Path(path).read_text())["items"]}


def load_assignments(path=STUDY / "assignments.csv") -> dict:
    with open(path) as f:
        return {(int(r["participant"]), r["item"]): r["style"] for r in csv.DictReader(f)}


def load_responses(folder) -> list[dict]:
    files = sorted(Path(folder).glob("*.json"))
    return [json.loads(f.read_text()) | {"_file": f.name} for f in files]


def score(sessions: list[dict], items: dict, assignments: dict) -> tuple[list[dict], list[str]]:
    """One row per answered item, scored; plus a list of problems (sessions
    or answers that were left out, and why)."""
    rows, problems, seen = [], [], set()
    for s in sessions:
        p = s.get("participant")
        if p in seen:
            problems.append(f"{s['_file']}: participant {p} appears twice; second file ignored")
            continue
        seen.add(p)
        for r in s.get("responses", []):
            it = items.get(r["item"])
            want = assignments.get((p, r["item"]))
            if it is None or want is None:
                problems.append(f"{s['_file']}: unknown item {r['item']} for participant {p}")
                continue
            if r["style"] != want:
                problems.append(f"{s['_file']}: {r['item']} shown as {r['style']}, "
                                f"assignment says {want}; answer ignored")
                continue
            timed = r["q1_ms"] is not None and r["q1_ms"] <= MAX_MS
            if not timed:
                problems.append(f"participant {p} {r['item']}: Q1 time "
                                f"{r['q1_ms']} ms over 5 minutes, treated as missing")
            asked2 = bool(it["q2_options"])
            rows.append({
                "participant": p, "item": r["item"], "style": r["style"],
                "position": r["position"], "source": it["source"],
                "q1": r["q1"], "q1_correct": r["q1"] in it["q1_correct"],
                "q1_ms": r["q1_ms"] if timed else None,
                "q2_asked": asked2,
                "q2_correct": (r["q2"] in it["q2_correct"]) if asked2 else None,
                "q2_ms": r["q2_ms"] if asked2 else None,
                "pip_use": s.get("background", {}).get("pip_use"),
                "seen_conflict": s.get("background", {}).get("seen_conflict"),
            })
        n = len(s.get("responses", []))
        if n < len(items):
            problems.append(f"participant {p}: {n} of {len(items)} items answered (unfinished)")
    return rows, problems


# ── analysis ────────────────────────────────────────────────────────────

def per_participant(rows) -> dict:
    """{participant: {style: {"acc": float | None, "time": float | None}}}."""
    out: dict = {}
    for p in sorted({r["participant"] for r in rows}):
        out[p] = {}
        for st in STYLES:
            rs = [r for r in rows if r["participant"] == p and r["style"] == st
                  and r["q1_ms"] is not None]
            ok = [r["q1_ms"] for r in rs if r["q1_correct"]]
            out[p][st] = {"acc": (sum(r["q1_correct"] for r in rs) / len(rs)) if rs else None,
                          "time": statistics.median(ok) if ok else None}
    return out


def compare(pp: dict, other: str, measure: str) -> dict:
    """Crux against *other* on one measure, over participants who have both."""
    pairs = [(v["crux"][measure], v[other][measure]) for v in pp.values()
             if v["crux"][measure] is not None and v[other][measure] is not None]
    test = wilcoxon([c - o for c, o in pairs])
    crux = [c for c, _ in pairs]
    base = [o for _, o in pairs]
    out = {"pairs": len(pairs), "n": test.n, "w_plus": test.w_plus, "p": test.p}
    if not pairs:
        return out | {"effect": None, "crux": None, "other": None}
    if measure == "time":
        mc, mo = statistics.median(crux), statistics.median(base)
        out |= {"crux": mc, "other": mo, "effect": 1 - mc / mo if mo else None}
    else:
        mc, mo = statistics.mean(crux), statistics.mean(base)
        out |= {"crux": mc, "other": mo, "effect": 100 * (mc - mo)}
    return out


def analyse(rows) -> dict:
    pp = per_participant(rows)
    tests = {(o, m): compare(pp, o, m) for o in ("pip", "uv") for m in ("time", "acc")}
    adj = holm({k: v["p"] for k, v in tests.items()})
    for k, v in tests.items():
        v["p_holm"] = adj[k]
        v["significant"] = adj[k] < ALPHA
    t, a = tests[("pip", "time")], tests[("pip", "acc")]
    h1 = bool(t["significant"] and t["effect"] is not None and t["effect"] >= TARGET_FASTER)
    h2 = bool(a["significant"] and a["effect"] is not None and a["effect"] >= TARGET_POINTS)
    return {"participants": pp, "tests": tests, "H1": h1, "H2": h2}


# ── report ──────────────────────────────────────────────────────────────

def _pct(x):
    return "–" if x is None else f"{100 * x:.0f}%"


def _p(p):
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def _s(ms):
    return "–" if ms is None else f"{ms / 1000:.1f} s"


def _table(head, body):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return "\n".join(out + ["| " + " | ".join(map(str, r)) + " |" for r in body])


def _by(rows, key):
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return groups


def _cell(rs):
    timed = [r for r in rs if r["q1_ms"] is not None]
    ok = [r["q1_ms"] for r in timed if r["q1_correct"]]
    acc = sum(r["q1_correct"] for r in timed) / len(timed) if timed else None
    return f"{_pct(acc)}, {_s(statistics.median(ok)) if ok else '–'} (n={len(timed)})"


def report(rows, problems, result, *, simulated: bool, sessions: int) -> str:
    T = result["tests"]
    parts = []
    if simulated:
        parts.append("# SIMULATED DATA: NOT STUDY RESULTS\n\nEvery number below comes from "
                     "made-up participants generated by `--simulate`, to test the analysis. "
                     "Do not report or cite them.")
    parts.append("# User study results" if not simulated else "## User study results (simulated)")
    parts.append(f"{sessions} participants, {len(rows)} scored answers. Analysis as fixed in "
                 "`study/PROTOCOL.md` section 7 (`python -m crux.benchmark.study_analysis`).")

    def verdict(name, ok, test, target):
        eff = test["effect"]
        what = ("faster" if name == "H1" else "points more accurate")
        e = ("–" if eff is None else (f"{100 * eff:.0f}% {what}" if name == "H1"
                                      else f"{eff:+.1f} {what}"))
        return (f"- **{name}: {'supported' if ok else 'not supported'}.** Crux vs pip: {e} "
                f"(target {target}); Holm-adjusted p {'' if test['p_holm'] < 0.001 else '= '}{_p(test['p_holm'])} "
                f"({test['pairs']} participants, {test['n']} non-zero differences).")
    parts.append("## Hypotheses\n\n" + "\n".join([
        verdict("H1", result["H1"], T[("pip", "time")], "30% faster"),
        verdict("H2", result["H2"], T[("pip", "acc")], "+15 points")]))

    body = []
    for (o, m), v in T.items():
        if m == "time":
            vals = f"{_s(v['crux'])} vs {_s(v['other'])}"
            eff = "–" if v["effect"] is None else f"{100 * v['effect']:.0f}% faster"
        else:
            vals = f"{_pct(v['crux'])} vs {_pct(v['other'])}"
            eff = "–" if v["effect"] is None else f"{v['effect']:+.1f} points"
        body.append([f"Crux vs {NAMES[o]}", "time to a correct answer (median)" if m == "time"
                     else "accuracy (mean)", vals, eff, v["pairs"], v["n"],
                     _p(v["p"]), _p(v["p_holm"]), "yes" if v["significant"] else "no"])
    parts.append("## The four tests\n\nExact two-sided Wilcoxon signed-rank tests on "
                 "per-participant values; Holm correction over the four.\n\n" + _table(
        ["comparison", "measure", "Crux vs other", "effect", "participants", "non-zero diffs",
         "p", "p (Holm)", "significant"], body))

    by_style = _by(rows, lambda r: r["style"])
    parts.append("## Descriptive\n\nEach cell: accuracy, median time to a correct answer "
                 "(answers).\n\n" + _table(
        ["", *[NAMES[s] for s in STYLES]],
        [["all items", *[_cell(by_style.get(s, [])) for s in STYLES]]]
        + [[f"{src} items", *[_cell([r for r in by_style.get(s, []) if r["source"] == src])
                               for s in STYLES]] for src in ("real", "planted")]))
    items = sorted({r["item"] for r in rows}, key=lambda x: int(x[1:]))
    parts.append("### By item\n\n" + _table(
        ["item", *[NAMES[s] for s in STYLES]],
        [[i, *[_cell([r for r in rows if r["item"] == i and r["style"] == s]) for s in STYLES]]
         for i in items]))
    q2 = [r for r in rows if r["q2_asked"]]
    parts.append("### Q2: which package is the conflict about\n\n" + _table(
        ["", *[NAMES[s] for s in STYLES]],
        [["accuracy", *[_pct((lambda rs: sum(r["q2_correct"] for r in rs) / len(rs) if rs else None)(
            [r for r in q2 if r["style"] == s])) for s in STYLES]]]))
    exp = sorted({r["pip_use"] for r in rows if r["pip_use"]})
    if exp:
        parts.append("### By pip experience\n\n" + _table(
            ["uses pip", *[NAMES[s] for s in STYLES]],
            [[e, *[_cell([r for r in rows if r["pip_use"] == e and r["style"] == s])
                   for s in STYLES]] for e in exp]))
    parts.append("## Exclusions and problems\n\n" + ("\n".join(f"- {x}" for x in problems)
                                                     if problems else "None."))
    parts.append("## Limits\n\nWith this many participants only large effects can be "
                 "detected; read the effect sizes with the p-values. The planted items are "
                 "deep conflicts chosen by rule, where pip is known to be weak, and two real "
                 "items cannot separate the styles on accuracy (`study/PROTOCOL.md` sections "
                 "3 and 8).")
    return "\n\n".join(parts) + "\n"


def write_long_csv(rows, path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ── simulation (pipeline testing only) ──────────────────────────────────

# assumptions for simulated participants (pipeline tests and power estimates
# only; nothing here is a finding)
TIME_FACTOR = {"pip": 1.0, "uv": 0.85, "crux": 0.6}
ACCURACY = {("pip", "real"): 0.9, ("pip", "planted"): 0.55, ("uv", "real"): 0.9,
            ("uv", "planted"): 0.8, ("crux", "real"): 0.95, ("crux", "planted"): 0.9}


def simulate_sessions(n: int, *, seed: int = 1, time_factor=None, acc=None,
                      items=None) -> list[dict]:
    """*n* made-up sessions in the form's format.  ``time_factor[style]``
    scales reading time; ``acc[(style, source)]`` is the chance of a correct
    Q1 answer."""
    from crux.benchmark.study import assign
    items = items or load_items()
    time_factor = time_factor or TIME_FACTOR
    acc = acc or ACCURACY
    rng = random.Random(seed)
    rows = assign(list(items.values()), n)
    sessions = []
    for p in range(1, n + 1):
        speed = rng.lognormvariate(0, 0.3)
        responses = []
        for a in [r for r in rows if r["participant"] == p]:
            it = items[a["item"]]
            ok = rng.random() < acc[(a["style"], it["source"])]
            wrong = [o for o in it["q1_options"] if o not in it["q1_correct"]] or [NONE]
            ms = 25000 * speed * time_factor[a["style"]] * rng.lognormvariate(0, 0.25)
            q2 = None
            if it["q2_options"]:
                q2 = it["q2_correct"][0] if ok else rng.choice(
                    [o for o in it["q2_options"] if o not in it["q2_correct"]] or [NONE])
            responses.append({**a, "q1": rng.choice(it["q1_correct"]) if ok else rng.choice(wrong),
                              "q1_ms": round(ms), "q2": q2,
                              "q2_ms": round(ms / 3) if q2 is not None else None})
        sessions.append({"participant": p, "simulated": True,
                         "background": {"pip_use": rng.choice(["A few times a year", "Monthly"]),
                                        "seen_conflict": rng.choice(["Yes", "No"])},
                         "responses": responses, "version": "v1"})
    return sessions


def simulate(n: int, folder, *, seed: int = 1, **kw) -> None:
    """Write *n* made-up sessions to *folder* (never study/responses)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for s in simulate_sessions(n, seed=seed, **kw):
        (folder / f"crux-study-p{s['participant']:02d}.json").write_text(json.dumps(s, indent=1))


def power(n: int, reps: int = 200, *, seed: int = 1, **kw) -> dict:
    """Share of simulated studies of *n* participants in which H1 and H2
    come out supported, under the given (assumed) effects."""
    from crux.benchmark.study import assign
    items = load_items()
    assignments = {(int(r["participant"]), r["item"]): r["style"]
                   for r in assign(list(items.values()), n)}
    h1 = h2 = 0
    for k in range(reps):
        sessions = [s | {"_file": "sim"} for s in
                    simulate_sessions(n, seed=seed * 100_000 + k, items=items, **kw)]
        rows, _ = score(sessions, items, assignments)
        res = analyse(rows)
        h1 += res["H1"]
        h2 += res["H2"]
    return {"participants": n, "reps": reps, "H1": h1 / reps, "H2": h2 / reps}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--responses", default=str(STUDY / "responses"))
    ap.add_argument("--out", help="report path (default: RESULTS.md, or "
                    "RESULTS_SIMULATED.md for simulated data, next to the responses)")
    ap.add_argument("--simulate", type=int, metavar="N",
                    help="first write N simulated participants into --responses")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--power", metavar="N,N,...",
                    help="estimate power by simulation for these participant counts and exit")
    ap.add_argument("--reps", type=int, default=200)
    a = ap.parse_args(argv)
    if a.power:
        print("Power by simulation (assumed effects: TIME_FACTOR and ACCURACY in this file)")
        for n in (int(x) for x in a.power.split(",")):
            r = power(n, a.reps, seed=a.seed)
            print(f"  {n:3d} participants: H1 supported in {r['H1']:.0%}, "
                  f"H2 in {r['H2']:.0%} of {r['reps']} simulated studies")
        return 0
    folder = Path(a.responses)
    if a.simulate:
        if folder.resolve() == (STUDY / "responses").resolve():
            print("refusing to write simulated data into study/responses; "
                  "pass --responses study/simulated", file=sys.stderr)
            return 2
        simulate(a.simulate, folder, seed=a.seed)
    sessions = load_responses(folder)
    if not sessions:
        print(f"no response files in {folder}", file=sys.stderr)
        return 1
    flags = {bool(s.get("simulated")) for s in sessions}
    if len(flags) > 1:
        print("simulated and real responses in one folder; refusing to analyse them together",
              file=sys.stderr)
        return 2
    simulated = flags == {True}
    rows, problems = score(sessions, load_items(), load_assignments())
    result = analyse(rows)
    out = Path(a.out) if a.out else folder.parent / (
        "RESULTS_SIMULATED.md" if simulated else "RESULTS.md")
    out.write_text(report(rows, problems, result, simulated=simulated,
                          sessions=len({s['participant'] for s in sessions})))
    write_long_csv(rows, out.with_suffix(".csv"))
    print(f"H1 {'supported' if result['H1'] else 'not supported'}, "
          f"H2 {'supported' if result['H2'] else 'not supported'}"
          f"{' (SIMULATED)' if simulated else ''}; wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
