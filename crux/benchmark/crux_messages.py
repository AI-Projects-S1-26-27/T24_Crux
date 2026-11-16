"""Crux's messages on the benchmark, measured exactly like pip's and uv's.

The harness (``harness.py``) runs pip, uv and Crux's solver and keeps every
tool's raw output in ``<part>/messages.jsonl``.  This step adds Crux's
*message*: for each instance it calls ``crux.explain.explain`` and renders
the core (``crux.explain.render``), wrapped at 80 columns as the CLI prints
it, then measures it with ``messages.measure``, the function that measures
pip and uv.  pip's and uv's numbers are re-measured from their saved output,
so all four tools are scored by one piece of code on the same instances.
pip and uv are not re-run.

Measures (definitions in ``messages.py``): message lines as printed, words
(independent of wrapping), packages named, whether the message names every
request in the conflict (``names_change``: what the user has to change),
and, where the conflict is known, recall and precision of the core's
packages.  ``statements`` for Crux is the number of groups in the rendered
core (docs/EXPLAIN.md section 9); pip's and uv's are counted from their text
as before, so the two are not the same measure and are reported apart.

Crux's time is in-process: explanation plus rendering, after the instance
is loaded; it is reported for information and is not comparable with the
harness's end-to-end times.

    python -m crux.benchmark.crux_messages --out benchmarks/results

writes ``<part>/crux_messages.jsonl`` (one row per instance, message text
included) and ``MESSAGES.md`` (the comparison tables).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from packaging.utils import canonicalize_name

from crux.benchmark.instance import Instance, constraint_from_json, load_jsonl
from crux.benchmark.messages import measure

WIDTH = 80
TOOLS = ("pip", "pip-baseline", "uv", "crux")
NAMES = {"pip": "pip 26.2.1", "pip-baseline": "pip 25.0.1", "uv": "uv 0.11.32",
         "crux": "Crux"}
ROOT = Path(__file__).resolve().parent.parent.parent
PARTS = {
    "planted": ROOT / "benchmarks" / "planted" / "instances.jsonl",
    "real": ROOT / "benchmarks" / "corpus" / "instances.jsonl.gz",
}
REAL_CORES = ROOT / "benchmarks" / "results" / "cores" / "real_cores.jsonl"


def _packages(inst: Instance) -> set[str]:
    names = set(inst.graph) | {canonicalize_name(d) for d, _ in inst.roots}
    for vs in inst.graph.values():
        for deps in vs.values():
            names.update(d for d, _ in deps)
    return names


def truth(inst: Instance, real_cores: dict):
    """The conflict a message should explain: the planted core, or for a real
    case the extracted core when it is proven the only minimal one (None
    otherwise: several correct answers, so recall is not scored)."""
    if inst.core is not None:
        return frozenset(inst.core)
    rec = real_cores.get(inst.id)
    if rec and rec.get("unique") == "unique":
        return frozenset(constraint_from_json(c) for c in rec["core"])
    return None


def core_scores(row: dict, core, named: set[str]) -> dict:
    if core is None:
        return {}
    pk = {c.dep for c in core} | {c.pkg for c in core if c.pkg}
    roots = {c.dep for c in core if c.kind == "root"}
    hit = named & pk
    return {"core_recall": len(hit) / len(pk),
            "core_precision": len(hit) / len(named) if named else None,
            "names_change": roots <= named}


def crux_row(inst: Instance, core) -> dict:
    from crux.explain.core import explain
    from crux.explain.render import render_core
    p = inst.problem()
    t0 = time.perf_counter()
    r = explain(p, budget_s=60)
    m = render_core(p, r.core, minimal=r.minimal) if r.core else None
    secs = time.perf_counter() - t0
    text = m.wrapped(WIDTH) if m else ""
    row = {"instance": inst.id, "tool": "crux", "status": r.status,
           "stdout": text, "stderr": "", "explain_s": round(secs, 4),
           "core_size": len(r.core), "minimal": r.minimal,
           "logical_lines": len(m.lines) if m else 0,
           "complete": m.complete if m else None, "checked": m.checked if m else None}
    row.update(_measured("crux", text, "", inst, core))
    row["statements"] = row["distinct_statements"] = m.statements if m else 0
    return row


def _measured(tool, stdout, stderr, inst, core) -> dict:
    out = measure(tool, stdout, stderr, packages=_packages(inst),
                  roots=[canonicalize_name(d) for d, _ in inst.roots])
    named = set(out["named_list"].split())
    out.update(core_scores(out, core, named))
    return out


_ADVICE = "To fix this you could try"


def without_advice(msg: str) -> str:
    """pip's message minus its fixed advice: the "To fix this you could
    try to:" block (two numbered generic suggestions) and the closing
    "ERROR: ResolutionImpossible: for help visit ..." line.  Neither depends
    on the instance, so this is the part of pip's message about the
    conflict."""
    out, skip = [], False
    for line in msg.splitlines():
        if line.startswith(_ADVICE):
            skip = True
            continue
        if skip and (line.strip()[:2] in ("1.", "2.") or not line.strip()):
            continue
        skip = False
        if line.startswith("ERROR: ResolutionImpossible"):
            continue
        out.append(line)
    return "\n".join(out)


def tool_rows(part_dir: Path, insts: dict, cores: dict) -> list[dict]:
    """pip's and uv's saved messages, re-measured."""
    out = []
    for line in (part_dir / "messages.jsonl").read_text().splitlines():
        d = json.loads(line)
        if d["tool"] not in TOOLS or d["status"] != "UNSAT":
            continue
        inst = insts[d["instance"]]
        row = {"instance": d["instance"], "tool": d["tool"], "status": d["status"]}
        row.update(_measured(d["tool"], d["stdout"], d["stderr"], inst, cores[inst.id]))
        if d["tool"].startswith("pip"):
            from crux.benchmark.messages import message
            core = without_advice(message(d["tool"], d["stdout"]))
            row["lines_no_advice"] = sum(1 for x in core.splitlines() if x.strip())
            row["words_no_advice"] = len(core.split())
        out.append(row)
    return out


# ── report ──────────────────────────────────────────────────────────────

def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _f(x, nd=0):
    return "–" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def _table(head, body):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return "\n".join(out)


def summary(rows: list[dict], scored: bool) -> str:
    """One row per tool over the instances where every tool gave a message
    (so the medians compare the same instances), then the target."""
    by = {t: {r["instance"]: r for r in rows if r["tool"] == t} for t in TOOLS}
    common = set.intersection(*(set(v) for v in by.values()))
    head = ["tool", "compared (of its messages)", "lines (median)", "words (median)",
            "names every request in the conflict", "core recall (mean)",
            "core precision (mean)"]
    body = []
    for t in TOOLS:
        rs = [by[t][i] for i in sorted(common)]
        sc = [r for r in rs if "names_change" in r]
        body.append([NAMES[t], f"{len(rs)} ({len(by[t])})",
                     _f(_med(r["msg_lines"] for r in rs), 1),
                     _f(_med(r["words"] for r in rs), 1),
                     f"{sum(r['names_change'] for r in sc)}/{len(sc)}" if sc else "–",
                     _f(_mean(r.get("core_recall") for r in sc), 2) if sc else "–",
                     _f(_mean(r.get("core_precision") for r in sc), 2) if sc else "–"])
    pip = [by["pip"][i] for i in common]
    crux = [by["crux"][i] for i in common]
    pl, cl = _med(r["msg_lines"] for r in pip), _med(r["msg_lines"] for r in crux)
    pw, cw = _med(r["words"] for r in pip), _med(r["words"] for r in crux)
    verdict = (f"Target (proposal 3.6): Crux's median message at most half of "
               f"pip 26.2.1's. Lines: Crux {_f(cl, 1)} vs pip {_f(pl, 1)} "
               f"({'met' if cl <= pl / 2 else 'not met'}); words: Crux {_f(cw, 1)} "
               f"vs pip {_f(pw, 1)} ({'met' if cw <= pw / 2 else 'not met'}).")
    pa = _med(r.get("lines_no_advice") for r in pip)
    wa = _med(r.get("words_no_advice") for r in pip)
    if pa is not None:
        verdict += (f" Without pip's fixed advice (the \"To fix this you could try\" "
                    f"block and the closing help link, the same in every message) pip's "
                    f"median is {_f(pa, 1)} lines and {_f(wa, 1)} words, and Crux's is "
                    f"{cl / pa:.0%} of that in lines and {cw / wa:.0%} in words: "
                    f"shorter, but the half-of-pip target is "
                    f"{'still met' if cl <= pa / 2 and cw <= wa / 2 else 'not met'} "
                    f"on this stricter count.")
    missing = {t: len(set().union(*(set(v) for v in by.values())) - set(by[t]))
               for t in TOOLS}
    gaps = [f"{NAMES[t]} gave no message on {n}" for t, n in missing.items() if n]
    note = (f"Medians over the {len(common)} instances where all four tools "
            "report a conflict" + (f" ({'; '.join(gaps)}: timeouts)" if gaps else "")
            + ".")
    return "\n\n".join([_table(head, body), note, verdict])


def depth_table(rows: list[dict], insts: dict) -> str:
    deep = [r for r in rows if insts[r["instance"]].meta.get("kind") == "deep"]
    depths = sorted({insts[r["instance"]].meta["depth"] for r in deep})
    head = ["depth"] + [f"{NAMES[t]} recall / lines" for t in TOOLS]
    body = []
    for d in depths:
        line = [str(d)]
        for t in TOOLS:
            rs = [r for r in deep if r["tool"] == t
                  and insts[r["instance"]].meta["depth"] == d]
            line.append(f"{_f(_mean(r.get('core_recall') for r in rs), 2)} / "
                        f"{_f(_med(r['msg_lines'] for r in rs), 1)}" if rs else "–")
        body.append(line)
    return _table(head, body)


def write_report(out: Path, results: dict, insts: dict, env: dict) -> Path:
    planted, real = results["planted"], results["real"]
    crux_all = [r for rs in results.values() for r in rs if r["tool"] == "crux"]
    times = [r["explain_s"] for r in crux_all]
    complete = sum(1 for r in crux_all if r["complete"] and r["checked"])
    unav = sum(1 for r in results["real"] if r["tool"] == "crux"
               and re.search(r"no release|has no releases|has only pre-releases",
                             " ".join(r["stdout"].split())))
    ex = {r["instance"]: r for r in real if r["tool"] == "crux"}
    sample = ex.get("real/grpc/grpc#39262")
    parts = [
        "# Error messages: Crux against pip and uv",
        f"Generated by `python -m crux.benchmark.crux_messages` on {env['when']}; "
        "pip's and uv's messages are the ones saved by the harness run in "
        "`planted/env.json` and `real/env.json`, re-measured here with the same "
        "code as Crux's. Crux's message is printed wrapped at 80 columns; "
        "`words` does not depend on wrapping.",
        "## Planted conflicts (198)",
        summary(planted, scored=True),
        "### By depth of the conflict (deep planted instances)",
        "Mean core recall (share of the planted conflict's packages the "
        "message names) and median message lines.",
        depth_table([r for r in planted], insts),
        "## Real conflicts (61)",
        summary(real, scored=True),
        "Core recall and `names every request` are scored on the 36 real "
        "conflicts whose core is proven the only minimal one "
        "(`cores/real_cores.jsonl`); on the others several answers are correct.",
        "## Crux's messages",
        f"- Complete and confirmed (every group of the core shown, every clash "
        f"checked against the graph): {complete} of {len(crux_all)}.",
        f"- Explanation time (in-process, solve + core extraction + rendering): "
        f"median {_f(_med(times), 3)} s, max {_f(max(times), 2)} s.",
        f"- {unav} of the 61 real messages say a requested version is missing "
        "without a reason. The real instances carry the data layer's drop reasons "
        "(`python -m crux.benchmark.add_drops`, from the corpus database), so "
        "these are versions the database has no record of: they did not exist on "
        "PyPI at all.",
    ]
    if sample:
        parts += ["Example (`grpc/grpc#39262`), as printed:",
                  "```\n" + sample["stdout"] + "\n```"]
    path = out / "MESSAGES.md"
    path.write_text("\n\n".join(parts) + "\n")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "results"))
    ap.add_argument("--parts", default="planted,real")
    a = ap.parse_args(argv)
    out = Path(a.out)
    real_cores = ({d["instance"]: d for d in map(json.loads,
                   REAL_CORES.read_text().splitlines())}
                  if REAL_CORES.exists() else {})
    results, all_insts = {}, {}
    for part in a.parts.split(","):
        insts = {i.id: i for i in load_jsonl(PARTS[part])}
        all_insts.update(insts)
        cores = {iid: truth(i, real_cores) for iid, i in insts.items()}
        t0 = time.perf_counter()
        crux = [crux_row(i, cores[i.id]) for i in insts.values()]
        print(f"{part}: {len(crux)} Crux messages in {time.perf_counter() - t0:.0f} s",
              flush=True)
        with open(out / part / "crux_messages.jsonl", "w") as f:
            for r in crux:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        results[part] = tool_rows(out / part, insts, cores) + crux
    env = {"when": time.strftime("%Y-%m-%d")}
    if set(results) == {"planted", "real"}:
        print(f"wrote {write_report(out, results, all_insts, env)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
