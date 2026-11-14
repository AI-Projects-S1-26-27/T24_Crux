"""Run pip, uv and Crux on benchmark instances and record what each does.

All three see identical metadata: each instance's graph is written as
metadata-only wheels (``crux.benchmark.wheels``) and pip and uv read that
folder with ``--no-index --find-links``.  Crux reads the same graph.

Passes (``run``):
  1. outcome: each tool once per instance, ``--jobs`` at a time; full output
     kept.  Timeouts here are re-run alone to rule out contention.
  2. timing: each tool that finished, ``--repeats`` times, one run at a time
     on an otherwise idle machine; the median is reported.
  3. crux configurations: C0..C4 and C4-noLCV in-process on a fresh graph each,
     ``--crux-timeout``; nodes, conflict size, and whether the conflict
     contains / equals the planted core.

pip and uv timings are end to end (interpreter start, reading wheels,
resolving).  Crux's ``crux-e2e`` timing is end to end too (a fresh Python
process via ``crux.benchmark.run_crux``), so the three are comparable.

CLI:
  python -m crux.benchmark.harness --instances benchmarks/planted/instances.jsonl \\
      --out benchmarks/results/planted --pip-python PY312 [--pip-baseline-python PY]
      --uv UV
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import dataclasses
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from crux.benchmark.instance import Instance, load_jsonl
from crux.benchmark.messages import measure
from crux.benchmark.run_crux import CONFIGS
from crux.benchmark.wheels import write_index
from crux.data.graph import Graph
from crux.resolver.checker import check_plan
from crux.resolver.solver import solve

UV_PLATFORM = "x86_64-unknown-linux-gnu"


@dataclass
class Run:
    tool: str
    status: str                     # SAT | UNSAT | TIMEOUT | ERROR
    wall_s: float
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    plan: dict | None = None        # {pkg: version}
    extra: dict = field(default_factory=dict)


def clean_env(home: str) -> dict:
    """No user config, no colour, no proxy: the run is offline anyway."""
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
            "NO_COLOR": "1", "PIP_CONFIG_FILE": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1", "UV_NO_CONFIG": "1"}


def _exec(cmd, timeout, env, merge=False):
    """Run with a hard timeout (kills the whole process group).  ``merge``
    sends stderr into stdout so the text is in the order a user sees it."""
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                         text=True, env=env, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return out, err or "", p.returncode, time.perf_counter() - t0, False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return out, err or "", None, time.perf_counter() - t0, True


def run_pip(tool, python, wheels, reqs, timeout, workdir) -> Run:
    """pip writes "ERROR:" lines to stderr but "The conflict is caused by:"
    and the advice to stdout; a terminal shows them interleaved.  So the two
    streams are merged, unbuffered, and kept in ``stdout``."""
    report = Path(workdir) / f"{tool}-report.json"
    report.unlink(missing_ok=True)
    cmd = [python, "-m", "pip", "install", "--dry-run", "--ignore-installed",
           "--no-index", "--find-links", str(wheels),
           "--disable-pip-version-check", "--no-cache-dir", "--no-input",
           "--no-color", "--report", str(report), *reqs]
    env = clean_env(str(workdir)) | {"PYTHONUNBUFFERED": "1"}
    out, err, rc, wall, timed_out = _exec(cmd, timeout, env, merge=True)
    if timed_out:
        return Run(tool, "TIMEOUT", wall, None, out, err)
    if rc == 0 and report.exists():
        install = json.loads(report.read_text()).get("install", [])
        plan = {canonicalize_name(i["metadata"]["name"]): i["metadata"]["version"]
                for i in install}
        return Run(tool, "SAT", wall, rc, out, err, plan)
    if "ResolutionImpossible" in out or "No matching distribution found" in out:
        return Run(tool, "UNSAT", wall, rc, out, err)
    return Run(tool, "ERROR", wall, rc, out, err)


def run_uv(tool, uv, wheels, reqs, timeout, workdir) -> Run:
    req_file = Path(workdir) / "requirements.in"
    req_file.write_text("\n".join(reqs) + "\n")
    cmd = [uv, "pip", "compile", str(req_file), "--no-index", "--find-links",
           str(wheels), "--python-version", "3.12", "--python-platform",
           UV_PLATFORM, "--no-cache", "--offline", "--no-config",
           "--no-header", "--no-annotate"]
    out, err, rc, wall, timed_out = _exec(cmd, timeout, clean_env(str(workdir)))
    if timed_out:
        return Run(tool, "TIMEOUT", wall, None, out, err)
    if rc == 0:
        plan = {canonicalize_name(m.group(1)): m.group(2) for m in
                re.finditer(r"^([A-Za-z0-9._-]+)==(\S+)", out, re.M)}
        return Run(tool, "SAT", wall, rc, out, err, plan)
    if "No solution found" in err:
        return Run(tool, "UNSAT", wall, rc, out, err)
    return Run(tool, "ERROR", wall, rc, out, err)


def run_crux_e2e(tool, instance_file, config, timeout, workdir) -> Run:
    cmd = [sys.executable, "-m", "crux.benchmark.run_crux", str(instance_file),
           "--config", config, "--timeout", str(timeout)]
    env = clean_env(str(workdir))
    env["PATH"] = os.path.dirname(sys.executable) + ":" + env["PATH"]
    out, err, rc, wall, timed_out = _exec(cmd, timeout + 30, env)
    if timed_out or rc != 0:
        return Run(tool, "TIMEOUT" if timed_out else "ERROR", wall, rc, out, err)
    res = json.loads(out)
    return Run(tool, res["status"], wall, rc, "", err, res["plan"],
               {"nodes": res["nodes"], "solve_s": res["solve_s"],
                "conflict_size": (len(res["conflict"])
                                  if res["conflict"] is not None else None)})


# ── one instance ─────────────────────────────────────────────────────

@dataclass
class Tools:
    pip_python: str
    uv: str
    pip_baseline_python: str | None = None

    def versions(self) -> dict:
        def ver(cmd):
            return subprocess.run(cmd, capture_output=True, text=True,
                                  env=clean_env(tempfile.gettempdir())
                                  ).stdout.strip()
        v = {"pip": ver([self.pip_python, "-m", "pip", "--version"]),
             "pip_python": ver([self.pip_python, "--version"]),
             "uv": ver([self.uv, "--version"])}
        if self.pip_baseline_python:
            v["pip_baseline"] = ver([self.pip_baseline_python, "-m", "pip", "--version"])
        return v


class Prepared:
    """Wheels and an instance file on disk for one instance."""

    def __init__(self, inst: Instance, root: Path):
        self.inst = inst
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", inst.id)
        self.dir = root / safe
        self.wheels = self.dir / "wheels"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n_wheels = write_index(inst.graph, self.wheels)
        self.file = self.dir / "instance.json"
        self.file.write_text(json.dumps(inst.to_json()))

    def run(self, tool: str, tools: Tools, timeout: float) -> Run:
        reqs = self.inst.requirements()
        work = self.dir / f"work-{tool}"
        work.mkdir(exist_ok=True)
        if tool == "pip":
            return run_pip(tool, tools.pip_python, self.wheels, reqs, timeout, work)
        if tool == "pip-baseline":
            return run_pip(tool, tools.pip_baseline_python, self.wheels, reqs, timeout, work)
        if tool == "uv":
            return run_uv(tool, tools.uv, self.wheels, reqs, timeout, work)
        if tool == "crux-e2e":
            return run_crux_e2e(tool, self.file, "C4-noLCV", timeout, work)
        raise ValueError(tool)


def _packages(inst: Instance) -> set[str]:
    names = set(inst.graph) | {d for d, _ in inst.roots}
    for vs in inst.graph.values():
        for deps in vs.values():
            names.update(d for d, _ in deps)
    return names


def message_row(inst: Instance, run: Run) -> dict:
    row = {"instance": inst.id, "tool": run.tool, "status": run.status,
           "exit_code": run.exit_code, "stdout": run.stdout, "stderr": run.stderr}
    if run.tool.startswith(("pip", "uv")) and run.status == "UNSAT":
        row.update(measure(
            run.tool, run.stdout, run.stderr, packages=_packages(inst),
            roots=[canonicalize_name(d) for d, _ in inst.roots],
            core_packages=inst.meta.get("core_packages"),
            has_releases=lambda p: bool(inst.graph.get(canonicalize_name(p)))))
    if run.status == "SAT" and run.plan is not None:
        prob = inst.problem()
        plan = {p: Version(v) for p, v in run.plan.items()}
        row["plan_valid"] = not check_plan(prob, plan)
    return row


def crux_rows(inst: Instance, timeout: float) -> list[dict]:
    rows = []
    core = inst.core
    core_pkgs = set(inst.meta.get("core_packages", []))
    for name, cfg in CONFIGS.items():
        prob = inst.problem(Graph.from_dict(inst.graph))      # fresh caches
        res = solve(prob, dataclasses.replace(cfg, timeout_s=timeout))
        row = {"instance": inst.id, "config": name, "status": res.status,
               "nodes": res.stats.nodes, "backtracks": res.stats.backtracks,
               "wall_s": round(res.stats.wall_s, 6)}
        if res.conflict is not None:
            conf = set(res.conflict)
            pk = {c.dep for c in conf} | {c.pkg for c in conf if c.pkg}
            row.update(conflict_size=len(conf))
            if core is not None:
                row.update(contains_core=core <= conf, equals_core=core == conf,
                           pkg_recall=len(pk & core_pkgs) / len(core_pkgs),
                           pkg_precision=len(pk & core_pkgs) / len(pk) if pk else None)
        if res.plan is not None:
            row["plan_valid"] = not check_plan(prob, res.plan)
        rows.append(row)
    return rows


def _crux_job(args):
    inst_json, timeout = args
    return crux_rows(Instance.from_json(inst_json), timeout)


# ── the run ──────────────────────────────────────────────────────────

def _meta_cols(inst):
    m = inst.meta
    return {"kind": m.get("kind", m.get("source", "")), "depth": m.get("depth", ""),
            "noise": m.get("noise", ""), "core_size": m.get("core_size", "")}


def _write_csv(rows, path, first=()):
    keys = list(first)
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)


def run(instances, out: Path, tools: Tools, *, timeout=60.0, crux_timeout=10.0,
        repeats=5, jobs=2, scratch=None, log=print) -> None:
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch or tempfile.mkdtemp(prefix="crux-bench-"))
    outcome_tools = ["pip", "uv", "crux-e2e"] + (
        ["pip-baseline"] if tools.pip_baseline_python else [])
    timed_tools = ["pip", "uv", "crux-e2e"]
    env = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "machine": {"platform": platform.platform(), "cpus": os.cpu_count(),
                       "python": sys.version.split()[0]},
           "tools": tools.versions(), "timeout_s": timeout,
           "crux_timeout_s": crux_timeout, "repeats": repeats, "jobs": jobs,
           "instances": len(instances)}
    log(f"tools: {env['tools']}")
    prepared = {i.id: Prepared(i, scratch) for i in instances}
    log(f"wrote wheels for {len(prepared)} instances under {scratch}")

    # 1. outcome pass
    t0 = time.perf_counter()
    results: dict[tuple[str, str], Run] = {}
    jobs_list = [(iid, tool) for iid in prepared for tool in outcome_tools]
    with cf.ThreadPoolExecutor(jobs) as ex:
        futs = {ex.submit(prepared[iid].run, tool, tools, timeout): (iid, tool)
                for iid, tool in jobs_list}
        for k, fut in enumerate(cf.as_completed(futs), 1):
            results[futs[fut]] = fut.result()
            if k % 100 == 0:
                log(f"  outcome {k}/{len(jobs_list)} ({time.perf_counter()-t0:.0f}s)")
    for key, r in list(results.items()):          # confirm timeouts alone
        if r.status == "TIMEOUT":
            results[key] = prepared[key[0]].run(key[1], tools, timeout)
    messages = [message_row(prepared[iid].inst, r) for (iid, _), r in sorted(results.items())]
    with open(out / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m, sort_keys=True) + "\n")
    _write_csv([{k: v for k, v in m.items() if k not in ("stdout", "stderr")}
                | _meta_cols(prepared[m["instance"]].inst) for m in messages],
               out / "outcomes.csv", ("instance", "tool", "status"))
    log(f"outcome pass done ({time.perf_counter()-t0:.0f}s)")

    # 2. timing pass, sequential
    t0 = time.perf_counter()
    timing = []
    for iid, p in prepared.items():
        for tool in timed_tools:
            first = results[(iid, tool)]
            if first.status in ("TIMEOUT", "ERROR"):
                timing.append({"instance": iid, "tool": tool, "repeat": 0,
                               "status": first.status, "wall_s": ""} | _meta_cols(p.inst))
                continue
            for rep in range(repeats):
                r = p.run(tool, tools, timeout)
                timing.append({"instance": iid, "tool": tool, "repeat": rep,
                               "status": r.status, "wall_s": round(r.wall_s, 4),
                               "consistent": r.status == first.status,
                               **{k: v for k, v in r.extra.items()}}
                              | _meta_cols(p.inst))
    _write_csv(timing, out / "timing.csv", ("instance", "tool", "repeat", "status", "wall_s"))
    log(f"timing pass done ({time.perf_counter()-t0:.0f}s)")

    # 3. crux configurations
    t0 = time.perf_counter()
    rows = []
    with cf.ProcessPoolExecutor(jobs) as ex:
        for rs in ex.map(_crux_job, [(i.to_json(), crux_timeout) for i in instances]):
            rows += rs
    meta = {i.id: _meta_cols(i) for i in instances}
    _write_csv([r | meta[r["instance"]] for r in rows], out / "crux_configs.csv",
               ("instance", "config", "status"))
    log(f"crux configurations done ({time.perf_counter()-t0:.0f}s)")
    env["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "env.json").write_text(json.dumps(env, indent=2) + "\n")
    shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pip / uv / Crux benchmark harness")
    ap.add_argument("--instances", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pip-python", required=True,
                    help="a CPython 3.12 interpreter with the pinned pip")
    ap.add_argument("--pip-baseline-python",
                    help="optional second pip, for a version-sensitivity check")
    ap.add_argument("--uv", required=True)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--crux-timeout", type=float, default=10.0)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--only", help="regex on instance ids")
    a = ap.parse_args(argv)
    insts = load_jsonl(a.instances)
    if a.only:
        insts = [i for i in insts if re.search(a.only, i.id)]
    run(insts, Path(a.out), Tools(a.pip_python, a.uv, a.pip_baseline_python),
        timeout=a.timeout, crux_timeout=a.crux_timeout, repeats=a.repeats,
        jobs=a.jobs, log=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
