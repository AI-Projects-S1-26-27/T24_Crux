"""Tables and figures from harness results (no numbers typed by hand).

    python -m crux.benchmark.report --planted benchmarks/results/planted \\
        [--real benchmarks/results/real --corpus benchmarks/corpus] \\
        --out benchmarks/results

Writes REPORT.md and figures/*.png.  Reading rules: medians, never sums;
timeouts counted, never averaged in; a tool's message is measured only where
the tool itself said "no solution".  matplotlib is optional (figures are
skipped without it).
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import statistics
import sys
from pathlib import Path

TOOLS = ("crux-e2e", "uv", "pip", "pip-baseline")
CONFIGS = ("C0", "C1", "C2", "C3", "C4", "C4-noLCV")


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _f(x):
    return float(x) if x not in ("", None) else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _fmt(x, nd=2):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "–"


def _table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def tool_names(env):
    t = env.get("tools", {})
    pick = lambda s: " ".join(s.split()[:2]) if s else "?"
    return {"pip": pick(t.get("pip")), "pip-baseline": pick(t.get("pip_baseline")),
            "uv": pick(t.get("uv")), "crux-e2e": "Crux C4-noLCV"}


def outcome_table(outcomes, names, expected="UNSAT", groups=None, gkey="noise",
                  timeout=60):
    head = ["tool", "no solution (correct)" if expected == "UNSAT" else "no solution",
            f"timeout ({timeout:.0f} s)", "error", "found a plan"]
    rows = []
    for tool in TOOLS:
        rs = [r for r in outcomes if r["tool"] == tool]
        if not rs:
            continue
        c = collections.Counter(r["status"] for r in rs)
        rows.append([names[tool], _pct(c["UNSAT"], len(rs)), c["TIMEOUT"],
                     c["ERROR"], c["SAT"]])
    out = _table(head, rows)
    if groups:
        sub = []
        for g in groups:
            row = [g]
            for tool in TOOLS:
                rs = [r for r in outcomes if r["tool"] == tool and r.get(gkey) == g]
                if rs:
                    row.append(_pct(sum(r["status"] == "UNSAT" for r in rs), len(rs)))
            sub.append(row)
        out += (f"\n\nAnswered \"no solution\" within {timeout:.0f} s, by " + gkey
                + ":\n\n") + _table(
            [gkey] + [names[t] for t in TOOLS if any(r["tool"] == t for r in outcomes)], sub)
    return out


def timing_table(timing, names):
    per = collections.defaultdict(list)
    for r in timing:
        if r["wall_s"] not in ("", None):
            per[(r["instance"], r["tool"])].append(float(r["wall_s"]))
    med = collections.defaultdict(list)
    for (inst, tool), xs in per.items():
        med[tool].append(statistics.median(xs))
    rows = []
    for tool in ("crux-e2e", "uv", "pip"):
        xs = sorted(med.get(tool, []))
        if not xs:
            continue
        p95 = xs[min(len(xs) - 1, int(0.95 * len(xs)))]
        rows.append([names[tool], len(xs), statistics.median(xs), p95, max(xs)])
    return _table(["tool", "instances finished", "median s", "p95 s", "max s"], rows)


MSG_COLS = ("msg_lines", "out_lines", "statements", "distinct_statements", "named")


def message_table(outcomes, names, with_core=True):
    head = ["tool", "n", "message lines (median)", "all output lines (median)",
            "statements (median)", "names a requested package"]
    if with_core:
        head += ["core packages named (mean recall)", "named that are in the core (mean precision)"]
    head += ["false 'no matching distributions'"]
    rows = []
    for tool in ("uv", "pip", "pip-baseline"):
        rs = [r for r in outcomes if r["tool"] == tool and r["status"] == "UNSAT"]
        if not rs:
            continue
        row = [names[tool], len(rs), _med(_f(r["msg_lines"]) for r in rs),
               _med(_f(r["out_lines"]) for r in rs), _med(_f(r["statements"]) for r in rs),
               _pct(sum(r["names_root"] == "True" for r in rs), len(rs))]
        if with_core:
            rec = [_f(r.get("core_recall")) for r in rs if r.get("core_recall") not in ("", None)]
            pre = [_f(r.get("core_precision")) for r in rs if r.get("core_precision") not in ("", None)]
            row += [statistics.mean(rec) if rec else None, statistics.mean(pre) if pre else None]
        if tool.startswith("pip"):
            claims = sum(int(r["unavailable_claimed"] or 0) > 0 for r in rs)
            false = sum(int(r["false_unavailable"] or 0) > 0 for r in rs)
            row.append(f"{_pct(false, len(rs))} of messages ({claims} print the section)")
        else:
            row.append("–")
        rows.append(row)
    return _table(head, rows)


def depth_table(outcomes, names):
    deep = [r for r in outcomes if r["kind"] == "deep" and r["status"] == "UNSAT"]
    depths = sorted({int(r["depth"]) for r in deep})
    rows = []
    tools = ("uv", "pip", "pip-baseline")
    for d in depths:
        row = [d]
        for tool in tools:
            rs = [r for r in deep if r["tool"] == tool and int(r["depth"]) == d]
            rec = [_f(r["core_recall"]) for r in rs if r.get("core_recall")]
            row.append(f"{_fmt(_med(_f(r['msg_lines']) for r in rs), 0)} lines, "
                       f"names {_fmt(statistics.mean(rec) if rec else None)} "
                       f"of the core (n={len(rs)})")
        rows.append(row)
    return _table(["depth"] + [names[t] for t in tools], rows), deep


def crux_table(crux):
    rows = []
    for cfg in CONFIGS:
        rs = [r for r in crux if r["config"] == cfg]
        if not rs:
            continue
        c = collections.Counter(r["status"] for r in rs)
        fin = [r for r in rs if r["status"] == "UNSAT"]
        cont = sum(r.get("contains_core") == "True" for r in fin)
        eq = sum(r.get("equals_core") == "True" for r in fin)
        ratio = [float(r["conflict_size"]) / float(r["core_size"]) for r in fin
                 if r.get("conflict_size") and r.get("core_size")]
        rows.append([cfg, _pct(c["UNSAT"], len(rs)), c["TIMEOUT"], c["SAT"],
                     _med(_f(r["nodes"]) for r in fin),
                     _pct(cont, len(fin)) if fin and fin[0].get("contains_core") else "–",
                     _pct(eq, len(fin)) if fin and fin[0].get("equals_core") else "–",
                     _med(ratio)])
    return _table(["config", "no solution", "timeout", "found a plan",
                   "nodes (median, finished)", "conflict contains planted core",
                   "conflict IS the planted core", "conflict size / core size (median)"], rows)


def timeout_notes(crux) -> str:
    """Where the configuration timeouts come from: search, or setup."""
    by = collections.defaultdict(dict)
    for r in crux:
        by[r["instance"]][r["config"]] = r
    rescued = [i for i, c in by.items() if c.get("C0", {}).get("status") == "TIMEOUT"
               and c.get("C1", {}).get("status") == "UNSAT"]
    c0_nodes = [int(by[i]["C0"]["nodes"]) for i in rescued]
    c1_nodes = [int(by[i]["C1"]["nodes"]) for i in rescued]
    later = [r for r in crux if r["config"] != "C0" and r["status"] == "TIMEOUT"]
    at_zero = sum(int(r["nodes"]) == 0 for r in later)
    lines = []
    if rescued:
        span = (f"{min(c1_nodes)}" if min(c1_nodes) == max(c1_nodes)
                else f"{min(c1_nodes)} to {max(c1_nodes)}")
        lines.append(
            f"On **{len(rescued)}** instances C0 timed out after "
            f"{min(c0_nodes):,} to {max(c0_nodes):,} nodes while C1 (AC-3) "
            f"answered with {span} search nodes.")
    if later:
        lines.append(
            f"Timeouts in C1 to C4: {len(later)}, of which **{at_zero} at 0 search "
            "nodes**, i.e. before search began (AC-3 and domain setup on the "
            f"largest graphs), on {len({r['instance'] for r in later})} instances.")
    return " ".join(lines)


def cores_section(cores: Path) -> str:
    env = json.loads((cores / "env_universe.json").read_text())
    out = [f"Core extraction is `crux.explain` (Yash Kejriwal), commit "
           f"`{env['explain_last_commit'][:7]}` (pull request #2, not merged when "
           "this ran). Each planted core is the unique minimal core, so an "
           "extracted core is correct only if it equals the planted one.", ""]
    for start, label in (("solver", "the solver's own conflict (explain's default)"),
                         ("universe", "every constraint of the instance")):
        f = cores / f"cores_{start}.csv"
        if not f.exists():
            continue
        rows = _rows(f)
        table = []
        for m in ("deletion", "deletion+refine", "quickxplain"):
            rs = [r for r in rows if r["method"] == m]
            if not rs:
                continue
            calls = [int(r["calls"]) for r in rs]
            table.append([m, _pct(sum(r["exact"] == "True" for r in rs), len(rs)),
                          sum(r["claimed_minimal"] == "True" for r in rs),
                          _med(float(r["start_size"]) for r in rs),
                          f"{statistics.median(calls):.0f} (max {max(calls)})",
                          _med(_f(r["wall_s"]) for r in rs),
                          sum(int(r["timeouts"]) for r in rs)])
        same = sum(r["start_size"] == r["core_size"] for r in rows if r["method"] == "deletion")
        out += [f"Starting from {label}:", "",
                _table(["method", "core = planted core", "claims minimal",
                        "start size (median)", "solver calls (median)",
                        "seconds (median)", "solver timeouts"], table), ""]
        if start == "solver":
            out += [f"Here the starting conflict already equals the planted core on "
                    f"{same} of {sum(1 for r in rows if r['method'] == 'deletion')} "
                    "instances, so extraction only confirms minimality. Starting from "
                    "every constraint tests the reduction itself:", ""]
    return "\n".join(out).rstrip()


def crux_noise_table(crux):
    rows = []
    for noise in ("none", "small", "large"):
        row = [noise]
        for cfg in CONFIGS:
            rs = [r for r in crux if r["config"] == cfg and r["noise"] == noise]
            row.append(f"{sum(r['status'] == 'TIMEOUT' for r in rs)}/{len(rs)}")
        rows.append(row)
    return _table(["noise"] + list(CONFIGS), rows)


def figures(out: Path, planted_out, deep, crux, names, funnel=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    fig_dir = out / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    made = []
    colors = {"uv": "#2a7ab9", "pip": "#c4452f", "pip-baseline": "#e3a33b",
              "crux-e2e": "#3c8d4f"}
    # 1. message length by conflict depth
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10, 4))
    depths = sorted({int(r["depth"]) for r in deep})
    styles = {"uv": ("o", "-", -0.08), "pip": ("s", "--", 0.0),
              "pip-baseline": ("^", ":", 0.08)}
    for tool in ("uv", "pip", "pip-baseline"):
        mk, ls, dx = styles[tool]
        at = {d: [r for r in deep if r["tool"] == tool and int(r["depth"]) == d]
              for d in depths}
        ys = [_med(_f(r["msg_lines"]) for r in at[d]) for d in depths]
        rec = [statistics.mean(_f(r["core_recall"]) for r in at[d]) if at[d] else None
               for d in depths]
        n = sum(len(v) for v in at.values())
        ax.plot([d + dx for d in depths], ys, marker=mk, ls=ls,
                label=f"{names[tool]} (n={n})", color=colors[tool])
        ax2.plot([d + dx for d in depths], rec, marker=mk, ls=ls, color=colors[tool])
    for a_ in (ax, ax2):
        a_.set_xticks(depths)
        a_.set_xlabel("depth of the planted conflict (longest chain)")
    ax.set_ylabel("lines in the error message (median)")
    ax.set_title("Message length")
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=8)
    ax2.set_ylabel("share of the true conflict's packages named (mean)")
    ax2.set_title("How much of the true conflict it names")
    ax2.set_ylim(0, 1.05)
    fig.suptitle("Error messages as the planted conflict gets deeper ('deep' instances)")
    fig.tight_layout()
    fig.savefig(fig_dir / "message_lines_by_depth.png", dpi=150)
    plt.close(fig)
    made.append("figures/message_lines_by_depth.png")
    # 2. answered within 60 s by noise level
    fig, ax = plt.subplots(figsize=(6.4, 4))
    noises = ["none", "small", "large"]
    width = 0.2
    for k, tool in enumerate(TOOLS):
        ys = []
        for n in noises:
            rs = [r for r in planted_out if r["tool"] == tool and r["noise"] == n]
            ys.append(100 * sum(r["status"] == "UNSAT" for r in rs) / len(rs) if rs else 0)
        ax.bar([i + (k - 1.5) * width for i in range(3)], ys, width,
               label=names[tool], color=colors[tool])
    ax.set_xticks(range(3), ["no background", "small background\n(6-10 packages)",
                             "large background\n(25-40 packages)"])
    ax.set_ylabel("% answered 'no solution' in time")
    ax.set_ylim(0, 100)
    ax.set_title("Planted conflicts: answered correctly within the time limit",
                 pad=28)
    ax.legend(fontsize=8, loc="lower center", bbox_to_anchor=(0.5, 1.0),
              ncol=4, frameon=False)
    fig.tight_layout()
    fig.savefig(fig_dir / "answered_by_noise.png", dpi=150)
    plt.close(fig)
    made.append("figures/answered_by_noise.png")
    # 3. Crux conflict size against the planted core
    fig, ax = plt.subplots(figsize=(6.4, 4))
    for cfg, col, mk in (("C0", "#999999", "x"), ("C4", "#3c8d4f", "o")):
        pts = [(float(r["core_size"]), float(r["conflict_size"])) for r in crux
               if r["config"] == cfg and r["status"] == "UNSAT" and r.get("conflict_size")]
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=14, alpha=0.6,
                   marker=mk, color=col, label=f"{cfg} (n={len(pts)})")
    lim = max([float(r["conflict_size"]) for r in crux if r.get("conflict_size")] + [2])
    ax.plot([1, lim], [1, lim], color="black", lw=0.8, ls="--",
            label="conflict = core (nothing extra)")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("planted core size (constraints, log)")
    ax.set_ylabel("constraints in Crux's reported conflict (log)")
    ax.set_title("How much larger than the true core is Crux's conflict?")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(fig_dir / "conflict_vs_core.png", dpi=150)
    plt.close(fig)
    made.append("figures/conflict_vs_core.png")
    if funnel:
        fig, ax = plt.subplots(figsize=(6.4, 4))
        labels, vals = zip(*funnel)
        ax.barh(range(len(vals))[::-1], vals, color="#2a7ab9")
        ax.set_yticks(range(len(vals))[::-1], labels, fontsize=8)
        for i, v in zip(range(len(vals))[::-1], vals):
            ax.text(v, i, f" {v}", va="center", fontsize=8)
        ax.set_xscale("log")
        ax.set_xlabel("issues (log)")
        ax.set_title("Real failures: from search results to replayed conflicts")
        fig.tight_layout()
        fig.savefig(fig_dir / "corpus_funnel.png", dpi=150)
        plt.close(fig)
        made.append("figures/corpus_funnel.png")
    return made


def _real_id(url: str) -> str:
    repo, num = url.split("github.com/", 1)[1].split("/issues/")
    return f"real/{repo}#{num}"


def corpus_section(corpus: Path, real_outcomes=None):
    issues = [json.loads(l) for l in open(corpus / "issues.jsonl")]
    screened = [json.loads(l) for l in open(corpus / "screened.jsonl")]
    replay = [json.loads(l) for l in open(corpus / "replay.jsonl")]
    src = collections.Counter(r["source"] for r in issues)
    drops = collections.Counter(r["reason"] for r in screened if r["status"] != "kept")
    kept = [r for r in screened if r["status"] == "kept"]
    outs = collections.Counter(r["outcome"] for r in replay)
    rep = [r for r in replay if r["outcome"] == "reproduced"]
    lines = [
        f"Issues collected: **{len(issues)}** (pip's tracker {src['pip']}, uv's "
        f"tracker {src['uv']}, other projects quoting pip's error {src['gh']}, "
        f"other projects quoting uv's error {src['uvgh']}).",
        "",
        "Screening (one reason per dropped issue):",
        "",
        _table(["outcome", "issues"], [["kept: a usable request", len(kept)]]
               + [[f"dropped: {k}", v] for k, v in drops.most_common()]),
        "",
        "Kept requests by source: " + ", ".join(
            f"{k} {v}" for k, v in collections.Counter(r["source"] for r in kept).most_common())
        + "; by extraction method: " + ", ".join(
            f"{k} {v}" for k, v in sorted(collections.Counter(r["method"] for r in kept).items()))
        + " (B = the user's install command, A = pip's 'The user requested' lines).",
        "",
        "Replay of the kept requests on the snapshot as of each report date:",
        "",
        _table(["outcome", "cases"], [[k, v] for k, v in outs.most_common()]),
    ]
    if real_outcomes is not None:
        by_inst = collections.defaultdict(list)
        for r in real_outcomes:
            by_inst[r["instance"]].append(r)
        no_lines = [r for r in rep if r.get("same_packages") is None]
        with_lines = [r for r in rep if r.get("same_packages") is not None]
        faithful = [r for r in with_lines if any(
            o.get("original_overlap") == "True" for o in by_inst[_real_id(r["url"])]
            if o["tool"].startswith("pip"))]
        lines += ["", "**Is the replay the user's failure?** For each reproduced case "
                  "whose post includes pip's own conflict lines, does pip, run on our "
                  "replayed data, name at least one package that pip named for the "
                  f"user? Yes for **{len(faithful)} of {len(with_lines)}**. "
                  f"{len(no_lines)} reproduced cases have no conflict lines to compare "
                  "(their request came from a command, or from uv); they are genuine "
                  "conflicts on real metadata, but not shown to be the reporter's."]
    funnel = [("issues collected", len(issues)), ("kept after screening", len(kept)),
              ("requested packages available", len(kept) - outs["not_on_pypi"]
               - outs["ingest_error"] - outs["root_unavailable"]),
              ("unsatisfiable on the snapshot", outs["reproduced"] + outs["missing_dependency"]),
              ("reproduced (genuine conflict)", outs["reproduced"])]
    return "\n".join(lines), funnel, outs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--planted", required=True)
    ap.add_argument("--real")
    ap.add_argument("--corpus")
    ap.add_argument("--cores", help="core-extraction scores (score_cores output)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    P = Path(a.planted)
    env = json.loads((P / "env.json").read_text())
    names = tool_names(env)
    po = _rows(P / "outcomes.csv")
    pt = _rows(P / "timing.csv")
    pc = _rows(P / "crux_configs.csv")
    n_inst = len({r["instance"] for r in po})
    dtab, deep = depth_table(po, names)
    md = [
        "# Benchmark results",
        "",
        "Generated by `python -m crux.benchmark.report` from the CSV files next to "
        "this report. Do not edit numbers by hand: re-run the script.",
        "",
        f"Tools: `{names['pip']}` (main pip baseline) and `{names['pip-baseline']}` "
        f"(version check), both on {env['tools'].get('pip_python', '?')}; "
        f"`{names['uv']}`. "
        f"Machine: {env['machine']['platform']}, {env['machine']['cpus']} CPUs, "
        f"Python {env['machine']['python']} for Crux. Timeout {env['timeout_s']:.0f} s; "
        f"Crux configurations {env['crux_timeout_s']:.0f} s. Timings: median of "
        f"{env['repeats']} sequential runs per instance.",
        "",
        f"## Planted conflicts ({n_inst} instances, every one proven unsatisfiable "
        "with a unique minimal core)",
        "",
        "### Outcome",
        "",
        outcome_table(po, names, groups=["none", "small", "large"],
                      timeout=env["timeout_s"]),
        "",
        "### Time (end to end, instances the tool finished)",
        "",
        timing_table(pt, names),
        "",
        "### Messages (instances where the tool answered 'no solution')",
        "",
        message_table(po, names),
        "",
        "Message lines by conflict depth ('deep' instances):",
        "",
        dtab,
        "",
        "### Crux configurations",
        "",
        crux_table(pc),
        "",
        "Timeouts by noise level:",
        "",
        crux_noise_table(pc),
    ]
    if a.cores and Path(a.cores, "env_universe.json").exists():
        md += ["", "### Core extraction against the planted cores", "",
               cores_section(Path(a.cores))]
    funnel = None
    ro = None
    if a.real and Path(a.real, "outcomes.csv").exists():
        R = Path(a.real)
        ro, rt, rc = (_rows(R / "outcomes.csv"), _rows(R / "timing.csv"),
                      _rows(R / "crux_configs.csv"))
    if a.corpus:
        sec, funnel, outs = corpus_section(Path(a.corpus), ro)
        md += ["", "## Real failures", "", sec]
    if ro is not None:
        md += ["", "### Tools on the reproduced real conflicts", "",
               outcome_table(ro, names, expected=None, timeout=env["timeout_s"]), "",
               "Time:", "", timing_table(rt, names), "",
               "Messages:", "", message_table(ro, names, with_core=False), "",
               "Crux configurations:", "", crux_table(rc), "", timeout_notes(rc)]
    made = figures(out, po, deep, pc, names, funnel)
    if made:
        md += ["", "## Figures", ""] + [f"![{m}]({m})" for m in made]
    (out / "REPORT.md").write_text("\n".join(md) + "\n")
    print(f"wrote {out / 'REPORT.md'} and {len(made)} figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
