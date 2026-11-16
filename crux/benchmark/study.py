"""User study materials: which message lets a reader find the cause fastest.

Design (full protocol: ``study/PROTOCOL.md``).  Within-subject: every
participant sees 8 conflicts, each with one message style (pip 26.2.1, uv
0.11.32 or Crux), styles assigned by a Latin square over three groups and
the order shuffled per participant.  Per conflict:

Q1 (primary, proposal 3.6)  "Which of these requirements would you change?"
    Options: the requirements file.  Correct: any request in the minimal
    core.  Timed from the moment the message is shown.
Q2 (secondary)  "Which package is the conflict about?"  Options: every
    package any of the three messages names, plus the requests.  Correct:
    the package(s) where the requirements finally collide.

Items are chosen by rules fixed before looking at any participant data
(``select_items``):

* real (4): the robust conflicts with a unique minimal core whose
  requirements file has requests outside the core (the only two such), and
  the two robust unique conflicts with the most requirement statements;
* planted (4): for each depth 3 to 6, the first deep instance (by id) that
  has at least one request outside the core and a pip 26.2.1 message.

Messages are the ones the benchmark saved (pip and uv: ``messages.jsonl``,
Crux: ``crux_messages.jsonl``), cut as ``messages.message`` defines a
message, so the study shows exactly what the benchmark measured.

    python -m crux.benchmark.study --participants 18 --out study

writes ``items.json`` (with answers, for scoring), ``assignments.csv`` and
``form.html`` (self-contained; no answers in it).
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import random
import sys
from pathlib import Path

from crux.benchmark.crux_messages import PARTS, REAL_CORES, ROOT, _packages
from crux.benchmark.instance import constraint_from_json, load_jsonl
from crux.benchmark.messages import message, named_packages

STYLES = ("pip", "uv", "crux")
RESULTS = ROOT / "benchmarks" / "results"
PRACTICE = "planted/direct/d1/none/s0"
SEED = 2026
MIN_Q2 = 3          # Q2 needs at least this many packages to choose from


def _req(c) -> str:
    return f"{c.dep}{c.spec}"


def load_messages() -> dict:
    out = {}
    for part in ("planted", "real"):
        for line in (RESULTS / part / "messages.jsonl").read_text().splitlines():
            d = json.loads(line)
            if d["tool"] in ("pip", "uv") and d["status"] == "UNSAT":
                text = d["stdout"] if d["tool"] == "pip" else d["stderr"]
                out[(d["instance"], d["tool"])] = message(d["tool"], text)
        for line in (RESULTS / part / "crux_messages.jsonl").read_text().splitlines():
            d = json.loads(line)
            out[(d["instance"], "crux")] = d["stdout"]
    return out


def targets(problem, core) -> list[str]:
    """Packages where the requirements finally collide: what the core's
    requirements point at but that has no requirement of its own in it."""
    from crux.explain.group import group_core
    groups = group_core(problem, core)
    has_edges = {g.pkg for g in groups if g.kind == "edge"}
    return sorted({g.dep for g in groups if g.dep not in has_edges})


def _item(inst, core, msgs: dict, source: str) -> dict:
    reqs = [_req(c) for c in inst.root_constraints()]
    in_core = {_req(c) for c in core if c.kind == "root"}
    texts = {s: msgs[(inst.id, s)] for s in STYLES}
    pk = _packages(inst)
    shown = set().union(*(named_packages(t, pk) for t in texts.values()))
    shown |= {c.dep for c in inst.root_constraints()}
    tgt = targets(inst.problem(), core)
    opts = sorted(shown | set(tgt))
    return {
        "instance": inst.id, "source": source,
        "requirements": reqs,
        "messages": texts,
        "q1_options": reqs, "q1_correct": [r for r in reqs if r in in_core],
        # Q2 is asked only when it has a real choice: a conflict inside one
        # package (two pins on pillow) has a single possible answer
        "q2_options": opts if len(opts) >= MIN_Q2 else [],
        "q2_correct": tgt if len(opts) >= MIN_Q2 else [],
    }


def select_items(msgs: dict | None = None) -> list[dict]:
    msgs = msgs if msgs is not None else load_messages()
    real = {i.id: i for i in load_jsonl(PARTS["real"])}
    planted = {i.id: i for i in load_jsonl(PARTS["planted"])}
    rc = {d["instance"]: d for d in map(json.loads, REAL_CORES.read_text().splitlines())}

    clean = []
    for iid in sorted(rc):
        d = rc[iid]
        if not (d["robust"] and d["unique"] == "unique"):
            continue
        core = frozenset(constraint_from_json(c) for c in d["core"])
        decoys = len({_req(c) for c in real[iid].root_constraints()}
                     - {_req(c) for c in core if c.kind == "root"})
        clean.append((iid, core, decoys, d["core_groups"]))
    with_decoys = [x for x in clean if x[2] > 0]
    rest = sorted((x for x in clean if x[2] == 0), key=lambda x: (-x[3], x[0]))
    chosen_real = with_decoys + rest[:4 - len(with_decoys)]

    chosen_planted = []
    for depth in (3, 4, 5, 6):
        for iid in sorted(planted):
            inst = planted[iid]
            if inst.meta.get("kind") != "deep" or inst.meta.get("depth") != depth:
                continue
            roots = {c.dep for c in inst.root_constraints()}
            core_roots = {c.dep for c in inst.core if c.kind == "root"}
            if roots - core_roots and (iid, "pip") in msgs:
                chosen_planted.append((iid, inst.core))
                break

    items = [_item(real[iid], core, msgs, "real") for iid, core, *_ in chosen_real]
    items += [_item(planted[iid], core, msgs, "planted") for iid, core in chosen_planted]
    for n, it in enumerate(items, 1):
        it["id"] = f"S{n}"
    return items


def practice_item(msgs: dict | None = None) -> dict:
    msgs = msgs if msgs is not None else load_messages()
    inst = {i.id: i for i in load_jsonl(PARTS["planted"])}[PRACTICE]
    it = _item(inst, inst.core, msgs, "planted")
    it["id"] = "P"
    return it


def assign(items: list[dict], participants: int, seed: int = SEED) -> list[dict]:
    """Latin square over three groups (participant p is in group p mod 3;
    item k gets style (k + group) mod 3), order shuffled per participant."""
    rows = []
    for p in range(1, participants + 1):
        group = p % 3
        order = list(range(len(items)))
        random.Random(seed * 1000 + p).shuffle(order)
        for pos, k in enumerate(order, 1):
            rows.append({"participant": p, "group": group, "position": pos,
                         "item": items[k]["id"],
                         "style": STYLES[(k + group) % len(STYLES)]})
    return rows


def _public(it: dict) -> dict:
    """What the form needs: no correct answers."""
    return {k: it[k] for k in ("id", "requirements", "messages", "q1_options",
                               "q2_options")}


def form_html(items, practice, assignments) -> str:
    data = {"items": [_public(i) for i in items],
            "practice": practice,           # answers shown as feedback
            "assignments": assignments}
    template = (Path(__file__).parent / "study_form.html").read_text()
    return template.replace("/*DATA*/null", json.dumps(data))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--participants", type=int, default=18)
    ap.add_argument("--out", default=str(ROOT / "study"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    msgs = load_messages()
    items, practice = select_items(msgs), practice_item(msgs)
    rows = assign(items, a.participants)
    (out / "items.json").write_text(json.dumps(
        {"practice": practice, "items": items}, indent=1) + "\n")
    with open(out / "assignments.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "form.html").write_text(form_html(items, practice, rows))
    for it in items:
        print(f"{it['id']} {it['source']:8} {it['instance']}")
    print(f"wrote items.json, assignments.csv ({a.participants} participants), "
          f"form.html to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
