"""The C0 to C4 ablation: run, summarise, plot, write a report.

    python -m crux.resolver.ablation --out results/ablation \
        --seeds 10 --timeout 10 --repeats 3

Writes into ``--out``:
    rows.csv     one row per (case, configuration), the raw data
    summary.csv  medians per (family, size, configuration)
    reduction.csv node-reduction statistics per (family, size, base -> other)
    report.md    the tables and the caveats, ready to read
    nodes_by_size.png, reduction.png   (only if matplotlib is installed)

How the numbers are read (these choices are the point of this module):

* Medians, win counts and timeout counts, never summed nodes.  A heavy tail of
  rare disasters dominates a sum (measured: one MRV instance used 87,553 nodes
  where plain search used 31).
* A configuration that times out reports the nodes it reached by the deadline,
  which is only a LOWER BOUND on its true effort.  So the node reduction is
  given twice: over the cases both finished (``median``), and as a valid lower
  bound on the median over ALL cases (``lower_bound_median``).  For that bound
  every case contributes the smallest ratio it could truly have: the computed
  ratio when both finished or only the base timed out (the true ratio is at
  least that), and 0 when the *other* configuration timed out (its true effort
  is unknown and could be arbitrarily large).  The first version dropped those
  cases instead, which is not a lower bound; an independent review caught it.
  Cases where the other configuration timed out are also counted separately.
* Node counts are deterministic for runs that FINISH.  A timed-out run's count
  depends on how fast the machine was, so medians that include timeouts are not
  exactly reproducible.  Wall-clock is the median of ``--repeats`` runs.
"""

from __future__ import annotations

import argparse
import csv
import math
import platform
import statistics
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from crux.resolver.bench import (
    Row, build_cases, problems, read_csv, run_suite, summarize, write_csv,
)
from crux.resolver.families import FAMILIES

CONFIGS = ("C0", "C1", "C2", "C3", "C4")
DESCRIPTION = {
    "C0": "plain backtracking", "C1": "+ AC-3", "C2": "+ MRV",
    "C3": "+ LCV", "C4": "+ backjumping and nogoods",
}


# ── statistics ───────────────────────────────────────────────────────

def _cases(rows):
    by = {}
    for r in rows:
        by.setdefault((r.family, r.size, r.seed), {})[r.config] = r
    return by


def reduction_stats(rows, base: str = "C0", other: str = "C4") -> dict:
    """Node reduction base -> other over the cases both configs ran.

    ratio = base.nodes / other.nodes, so 10 means ten times fewer nodes.
    See the module docstring for how timeouts are treated.
    """
    both, lower = [], []
    out = dict(cases=0, both_finished=0, wins=0, losses=0, ties=0,
               base_timed_out=0, other_timed_out=0, both_timed_out=0,
               median=None, geomean=None, lower_bound_median=None,
               worst=None, best=None)
    for rs in _cases(rows).values():
        if base not in rs or other not in rs:
            continue
        b, o = rs[base], rs[other]
        out["cases"] += 1
        bt, ot = b.status == "TIMEOUT", o.status == "TIMEOUT"
        ratio = b.nodes / max(o.nodes, 1)
        if bt and ot:
            out["both_timed_out"] += 1
            lower.append(0.0)                   # nothing known: smallest value
        elif bt:
            out["base_timed_out"] += 1
            lower.append(ratio)                 # true ratio is >= this
        elif ot:
            out["other_timed_out"] += 1         # the other one lost: counted
            lower.append(0.0)                   # its true effort is unbounded
        else:
            both.append(ratio)
            lower.append(ratio)
            out["both_finished"] += 1
            out["wins"] += o.nodes < b.nodes
            out["losses"] += o.nodes > b.nodes
            out["ties"] += o.nodes == b.nodes
    if both:
        out["median"] = statistics.median(both)
        out["geomean"] = math.exp(statistics.mean(math.log(x) for x in both))
        out["worst"], out["best"] = min(both), max(both)
    if lower:
        out["lower_bound_median"] = statistics.median(lower)
    return out


def reduction_table(rows, base: str = "C0", other: str = "C4"):
    """One dict per (family, size) with the reduction statistics."""
    keys = sorted({(r.family, r.size) for r in rows},
                  key=lambda k: (list(FAMILIES).index(k[0])
                                 if k[0] in FAMILIES else 99, k[1]))
    out = []
    for fam, size in keys:
        sub = [r for r in rows if r.family == fam and r.size == size]
        out.append({"family": fam, "size": size, "base": base, "other": other,
                    **reduction_stats(sub, base, other)})
    return out


# ── running ──────────────────────────────────────────────────────────

def run_ablation(families=None, sizes=None, seeds=range(10), *,
                 timeout_s: float = 10.0, repeats: int = 3,
                 configs=CONFIGS):
    cases = build_cases(families, sizes, seeds)
    return run_suite(cases, configs, timeout_s=timeout_s, repeats=repeats)


# ── report ───────────────────────────────────────────────────────────

def _fmt(x, nd=1):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return f"{x:,}"


def _md_table(head, body):
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def build_report(rows, *, settings: dict, skipped=()) -> str:
    L = ["# Ablation: C0 to C4", ""]
    L.append("Configurations: " + ", ".join(
        f"**{c}** {DESCRIPTION[c]}" for c in CONFIGS if c in
        {r.config for r in rows}) + ".")
    L.append("")
    L.append("Settings: " + ", ".join(f"{k} = {v}" for k, v in settings.items())
             + f". Python {platform.python_version()} on {platform.system()}"
             f". Generated {datetime.now():%Y-%m-%d %H:%M}.")
    if skipped:
        L.append(f"\nNot run (not implemented): {', '.join(skipped)}.")
    bad = problems(rows)
    L.append("")
    L.append(f"**Correctness check:** {len(rows)} runs, "
             + ("no wrong answers, no disagreements between configurations, "
                "no invalid plans." if not bad else
                f"**{len(bad)} PROBLEM ROWS: see rows.csv. Do not quote "
                "these numbers.**"))

    L += ["", "## Headline: node reduction C0 to C4", "",
          "`ratio = C0 nodes / C4 nodes` (10 = ten times fewer). "
          "**median** is over cases both finished; **median >=** is a lower "
          "bound on the median over ALL cases: a case where C0 timed out "
          "counts at its computed ratio (C0's true count is larger), a case "
          "where C4 timed out counts as 0. "
          "**wins/losses** count cases both finished where C4 used fewer/more "
          "nodes; a case where only C0 timed out is also a C4 win. "
          "**C0 t/o** and **C4 t/o** count timeouts (including both).", ""]
    head = ["family", "size", "cases", "median", "median >=", "geo. mean",
            "wins", "losses", "worst", "C0 t/o", "C4 t/o"]
    body = []
    for s in reduction_table(rows, "C0", "C4"):
        body.append([s["family"], str(s["size"]), str(s["cases"]),
                     _fmt(s["median"]), _fmt(s["lower_bound_median"]),
                     _fmt(s["geomean"]), str(s["wins"]), str(s["losses"]),
                     _fmt(s["worst"], 3),
                     str(s["base_timed_out"] + s["both_timed_out"]),
                     str(s["other_timed_out"] + s["both_timed_out"])])
    L.append(_md_table(head, body))

    L += ["", "## What each technique adds", "",
          "Median node reduction of each configuration over the one before it "
          "(same cases, same rules).", ""]
    head = ["family", "size", "C0 to C1", "C1 to C2", "C2 to C3", "C3 to C4"]
    steps = list(zip(CONFIGS, CONFIGS[1:]))
    body = []
    fam_sizes = [(s["family"], s["size"]) for s in reduction_table(rows)]
    for fam, size in fam_sizes:
        sub = [r for r in rows if r.family == fam and r.size == size]
        cells = []
        for a, b in steps:
            st = reduction_stats(sub, a, b)
            if not st["cases"]:
                cells.append("-")
                continue
            cell = _fmt(st["lower_bound_median"], 2)
            lost = st["other_timed_out"] + st["both_timed_out"]
            if lost:                                # a loss, never hidden
                cell += f" ({b} t/o on {lost})"
            cells.append(cell)
        body.append([fam, str(size)] + cells)
    L.append(_md_table(head, body))
    L.append("\nValues are the lower-bound median over all cases (a timeout of "
             "the earlier configuration counts at its computed ratio, a "
             "timeout of the later one counts as 0). 1.00 means no change; "
             "below 1 means the later configuration used more nodes. "
             "\"(X t/o on n)\" means the later configuration X timed out on n "
             "cases.")

    L += ["", "## Per-family detail", ""]
    sums = summarize(rows)
    for fam in [f for f in FAMILIES if any(s["family"] == f for s in sums)]:
        L += [f"### {fam}", "", FAMILIES[fam].doc, ""]
        head = ["size", "config", "runs", "solved", "t/o", "median nodes",
                "median backtracks", "median wall s", "AC-3 pruned",
                "non-latest pins"]
        body = []
        for s in sums:
            if s["family"] != fam:
                continue
            body.append([str(s["size"]), s["config"], str(s["runs"]),
                         str(s["solved"]), str(s["timeouts"]),
                         _fmt(s["median_nodes"], 0),
                         _fmt(s["median_backtracks"], 0),
                         _fmt(s["median_wall_s"], 3),
                         "-" if s["ac3_share_pruned"] is None
                         else f"{s['ac3_share_pruned']:.1%}",
                         _fmt(s["median_non_latest"], 1)])
        L += [_md_table(head, body), ""]

    L += ["## How to read these results", "",
          "* **Synthetic and easier than real resolution.** The families are "
          "small, hand-built and bounded; the graph itself drops extras, "
          "sdist-only releases and unknown dependencies. Do not claim these "
          "ratios hold on real PyPI conflicts or against pip and uv.",
          "* **Medians, not sums.** A single instance can dominate a total "
          "(an MRV instance went from 31 to 87,553 nodes). Check `worst` and "
          "`losses` before trusting a median.",
          "* **Timeouts are censored.** A timed-out run reports the nodes "
          "reached at the deadline. `median >=` accounts for that; the plain "
          "`median` excludes those cases and so understates the gain.",
          "* **Wall-clock is not nodes.** LCV and learning do more work per "
          "node; a node reduction is not a time reduction. Compare "
          "`median wall s` too.",
          "* **Seeds.** `thrash`, `thrash_wide`, `pinned_pair` and `chain` do "
          "not depend on the seed, so they have one case per size; only "
          "`random_csp` has many."]
    return "\n".join(L) + "\n"


# ── plots (optional) ─────────────────────────────────────────────────

def make_plots(rows, out_dir: Path) -> list[Path]:
    """Two figures.  Needs matplotlib; returns the paths written.

    Configurations that give identical node counts (very common: C0 = C1 on
    most families, C2 = C3 on others) would hide one another, so each line is
    drawn thinner and with a smaller marker than the one before it.  Where
    several coincide you see them nested, and the outermost is the earliest
    configuration.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
    except ImportError as exc:                  # pragma: no cover
        raise RuntimeError("plots need matplotlib: pip install matplotlib "
                           "(or pip install -e '.[plots]')") from exc

    colours = {"C0": "#6b7280", "C1": "#2563eb", "C2": "#059669",
               "C3": "#d97706", "C4": "#dc2626"}
    markers = {"C0": "o", "C1": "s", "C2": "^", "C3": "D", "C4": "v"}
    widths = {"C0": 7.0, "C1": 5.0, "C2": 3.4, "C3": 2.2, "C4": 1.2}
    sizes_ms = {"C0": 13, "C1": 10, "C2": 8, "C3": 6, "C4": 4}
    sums = summarize(rows)
    fams = [f for f in FAMILIES if any(s["family"] == f for s in sums)]
    written = []

    # 1. median nodes against size, one panel per family, log y
    cols = min(3, len(fams))
    nrows = math.ceil(len(fams) / cols)
    fig, axes = plt.subplots(nrows, cols, figsize=(4.6 * cols, 3.7 * nrows),
                             squeeze=False)
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, fam in zip(axes.flat, fams):
        ax.set_visible(True)
        fam_sizes = sorted({s["size"] for s in sums if s["family"] == fam})
        for cfg in CONFIGS:                     # earliest drawn first = outermost
            pts = sorted((s["size"], max(s["median_nodes"], 1), s["timeouts"])
                         for s in sums
                         if s["family"] == fam and s["config"] == cfg)
            if not pts:
                continue
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    linestyle="-", marker=markers[cfg], color=colours[cfg],
                    lw=widths[cfg], ms=sizes_ms[cfg], alpha=0.9,
                    solid_capstyle="round", zorder=2 + CONFIGS.index(cfg))
            for x, y, t in pts:                 # hollow marker = timeouts
                if t:
                    ax.plot([x], [y], marker=markers[cfg], ms=sizes_ms[cfg] + 2,
                            markerfacecolor="white",
                            markeredgecolor=colours[cfg], markeredgewidth=2,
                            zorder=20 + CONFIGS.index(cfg))
        ax.set_yscale("log")
        if fam_sizes[-1] / fam_sizes[0] >= 20:           # only if it helps
            ax.set_xscale("log")
        ax.set_xticks(fam_sizes)
        ax.set_xticklabels([str(s) for s in fam_sizes])
        ax.tick_params(axis="x", which="minor", bottom=False,
                       labelbottom=False)
        ax.set_title(fam, fontsize=11)
        ax.set_xlabel("size")
        ax.set_ylabel("median nodes")
        ax.grid(True, which="major", alpha=0.3)
    handles = [Line2D([0], [0], color=colours[c], marker=markers[c], lw=2,
                      ms=7, label=f"{c} {DESCRIPTION[c]}") for c in CONFIGS]
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=8,
               frameon=False)
    fig.suptitle("Median nodes explored (log scale). Hollow marker: at least "
                 "one run timed out, so that value is a lower bound.\n"
                 "Configurations that coincide are drawn nested: the largest, "
                 "greyest marker is the earliest one.", fontsize=9)
    fig.tight_layout(rect=(0, 0.07, 1, 0.92))
    p1 = out_dir / "nodes_by_size.png"
    fig.savefig(p1, dpi=150)
    plt.close(fig)
    written.append(p1)

    # 2. node reduction vs C0 for each configuration, at each family's
    #    largest size.  Bars start at 1 (no change): right = fewer nodes,
    #    left = more nodes.
    height = 0.85
    fig, ax = plt.subplots(figsize=(9, 0.34 * len(fams) * (len(CONFIGS) + 0)
                                    + 2.2))
    ticks, labels, y = [], [], 0
    top = 1.0
    for fam in fams:
        size = max(s["size"] for s in sums if s["family"] == fam)
        sub = [r for r in rows if r.family == fam and r.size == size]
        first = y
        for cfg in CONFIGS[1:]:
            st = reduction_stats(sub, "C0", cfg)
            v = st["lower_bound_median"]
            if v is not None and v > 0:            # 0 cannot sit on a log axis
                ax.barh(y, v - 1, left=1, height=height, color=colours[cfg],
                        label=cfg if fam == fams[0] else None)
                bound = "≥ " if st["base_timed_out"] else ""
                num = f"{v:,.0f}×" if v >= 10 else f"{v:.2f}×"
                # name the configuration on every row, in its own colour, so
                # that a zero-length bar (no change) is still identifiable
                ax.text(max(v, 1) * 1.15, y, f"{cfg}  {bound}{num}",
                        va="center", fontsize=8, color=colours[cfg])
                top = max(top, v)
            else:
                why = (f"{cfg}  timed out" if (st["other_timed_out"]
                                               or st["both_timed_out"])
                       else f"{cfg}  no comparable case")
                ax.text(1.15, y, why, va="center", fontsize=8,
                        color=colours[cfg], style="italic")
            y += 1
        ticks.append((first + y - 1) / 2)
        labels.append(f"{fam} ({size})")
        y += 1
    ax.set_xscale("log")
    ax.set_xlim(0.4, top * 8)
    ax.axvline(1, color="black", lw=1)
    ax.set_yticks(ticks)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("median node reduction vs C0 (log scale)\n"
                  "bars start at 1 = no change; ≥ marks a lower bound "
                  "because C0 timed out", fontsize=9)
    ax.legend(title="config", fontsize=8, loc="lower right")
    ax.grid(True, axis="x", which="major", alpha=0.3)
    fig.tight_layout()
    p2 = out_dir / "reduction.png"
    fig.savefig(p2, dpi=150)
    plt.close(fig)
    written.append(p2)
    return written


# ── csv ──────────────────────────────────────────────────────────────

def write_reduction_csv(rows, path: Path):
    table = []
    for other in CONFIGS[1:]:
        table += reduction_table(rows, "C0", other)
    if not table:
        return
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)


def write_summary_csv(rows, path: Path):
    s = summarize(rows)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(s[0]))
        w.writeheader()
        w.writerows(s)


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="results/ablation")
    ap.add_argument("--families", default=",".join(FAMILIES))
    ap.add_argument("--sizes", default=None,
                    help="comma-separated; default each family's own")
    ap.add_argument("--seeds", type=int, default=10,
                    help="seeds per size, for the families that use a seed")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--repeats", type=int, default=3,
                    help="wall-clock runs per case; the median is reported")
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--from-rows", default=None, metavar="ROWS_CSV",
                    help="do not run anything: rebuild the report, summaries "
                         "and plots from an earlier rows.csv (pass the same "
                         "--seeds/--timeout/--repeats so the report says so)")
    args = ap.parse_args(argv)

    chosen = [f for f in args.families.split(",") if f]
    unknown = [f for f in chosen if f not in FAMILIES]
    if unknown:
        ap.error(f"unknown family {unknown}; choose from {list(FAMILIES)}")
    sizes = [int(s) for s in args.sizes.split(",")] if args.sizes else None

    if args.from_rows:
        rows, skipped = read_csv(args.from_rows), []
    else:
        rows, skipped = run_ablation(chosen, sizes, range(args.seeds),
                                     timeout_s=args.timeout,
                                     repeats=args.repeats)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out / "rows.csv")
    write_summary_csv(rows, out / "summary.csv")
    write_reduction_csv(rows, out / "reduction.csv")
    settings = {"seeds": args.seeds, "timeout_s": args.timeout,
                "repeats": args.repeats,
                "families": ",".join(chosen)}
    if args.from_rows:
        # nothing was run: the settings above are only what was passed on the
        # command line, so say where the numbers really came from
        settings = {"regenerated from": args.from_rows,
                    "settings as passed": f"seeds {args.seeds}, timeout "
                                          f"{args.timeout} s, repeats "
                                          f"{args.repeats}"}
    (out / "report.md").write_text(
        build_report(rows, settings=settings, skipped=skipped),
        encoding="utf-8")
    wrote = ["rows.csv", "summary.csv", "reduction.csv", "report.md"]
    if not args.no_plots:
        try:
            wrote += [p.name for p in make_plots(rows, out)]
        except RuntimeError as exc:
            print(f"plots skipped: {exc}", file=sys.stderr)
    print(f"{len(rows)} runs; wrote {', '.join(wrote)} to {out}")
    bad = problems(rows)
    for r in bad:
        print(f"PROBLEM {r.family}/{r.size}/s{r.seed} {r.config}: "
              f"status={r.status} expected={r.expected or '?'}",
              file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
