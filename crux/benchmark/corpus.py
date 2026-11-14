"""Turn collected issues into candidate real-failure cases (offline).

Input: the JSONL written by ``corpus_collect``.  Output: one row per issue,
kept or dropped with a reason, so every count in the report can be traced.

Extracting the failing request
------------------------------
Both methods are tried.  B is used when it yields a valid request, because a
command is the user's whole request; otherwise A.

A. pip's own report.  pip prints the user's requirements verbatim inside
   "The conflict is caused by:" as lines "The user requested <req>".  The
   requests are taken from the FIRST such block in the opening post (a post
   can show several attempts; mixing them would invent a request nobody
   made).  "(from requirements.txt)" is a location and is stripped.
B. The command.  In a fenced code block that also contains the error, the
   LAST "pip install" / "uv pip install" line before the error, if every
   argument is a plain requirement (flags that do not change what is
   resolved, such as -U or --no-cache-dir, are allowed).

A is pip's own record of what was asked, but pip lists only the requirements
directly involved in the failure, so A can be part of the request; the
replay checks whether that part alone fails.

Drop reasons (screening, before any replay)
-------------------------------------------
after_cutoff         reported after the snapshot (2026-11-01)
no_request           neither A nor B found a request
constraint           pip lists a constraint file (-c); not modelled
local_or_url         a local path, VCS or URL install
requirements_file    the command reads -r FILE, whose contents are unknown
non_pypi_index       a different or extra index (--index-url, -i, -f, ...)
unsupported_flag     a flag that changes resolution (--pre, --no-deps, ...)
unparseable          not a PEP 508 requirement
extras / marker      out of scope (docs/SCOPE.md)
local_version        a +local version (e.g. torch==2.1.0+cu118); out of scope
duplicate            the same request as an earlier issue
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

SNAPSHOT_END = "2026-10-31"
ERROR_RE = re.compile(r"ResolutionImpossible|No solution found when resolving"
                      r"|conflicting dependencies")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
CMD_RE = re.compile(r"\b(?:pip3?|python3?(?:\.\d+)? -m pip|uv pip)\s+install\s+(.*)$")
_HARMLESS = {"-U", "--upgrade", "--no-cache-dir", "--no-cache", "-v", "-vv",
             "-vvv", "--verbose", "-q", "--quiet", "--user", "--dry-run",
             "--ignore-installed", "-I", "--force-reinstall",
             "--disable-pip-version-check", "--no-input", "--progress-bar",
             "--break-system-packages", "--system", "--root-user-action"}
_TAKES_VALUE = {"--progress-bar", "--root-user-action"}
_INDEX = {"--index-url", "-i", "--extra-index-url", "-f", "--find-links",
          "--trusted-host"}
_FILE = {"-r", "--requirement", "-c", "--constraint"}
_PY_RE = re.compile(r"\b(?:Python|python|py|cp)\s?(3\.\d{1,2})\b")


@dataclass
class Case:
    url: str
    repo: str
    source: str
    created_at: str
    status: str = "kept"                 # kept | dropped
    reason: str = ""
    method: str = ""                     # A | B
    snippet: str = ""                    # verbatim text the request came from
    requirements: list = field(default_factory=list)
    reported_python: list = field(default_factory=list)
    original_conflict: list = field(default_factory=list)  # packages pip named
    title: str = ""
    body_sha256: str = ""

    def drop(self, reason: str) -> "Case":
        self.status, self.reason = "dropped", reason
        return self


def _strip_prefix(line: str) -> str:
    """Remove quoting and CI-log prefixes ("> ", "#12 0.53 ", timestamps)."""
    line = re.sub(r"^\s*(?:>\s*)+", "", line)
    line = re.sub(r"^\s*#\d+\s+\d+(?:\.\d+)?\s+", "", line)          # docker
    line = re.sub(r"^\s*\d{4}-\d\d-\d\dT[\d:.]+Z?\s+", "", line)      # CI time
    return line


def conflict_blocks(body: str):
    """Yield the cause lines of each "The conflict is caused by:" block."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if "The conflict is caused by:" not in line:
            continue
        block = []
        for nxt in lines[i + 1:]:
            s = _strip_prefix(nxt).strip()
            if "The user requested" in s or " depends on " in s:
                block.append(s)
            elif not s and not block:
                continue
            else:
                break
        yield block


def _user_requested(block):
    """(requirement text, kind) for each 'The user requested' line."""
    out = []
    for s in block:
        m = re.search(r"The user requested(?P<c> \(constraint\))? (?P<r>.+)$", s)
        if m:
            out.append((m.group("r").strip(), "constraint" if m.group("c") else "req"))
    return out


def _conflict_packages(block) -> list[str]:
    names = set()
    for s in block:
        m = re.search(r"^(\S+) \S+ depends on (\S+?)(?:[<>=!~;\[ (]|$)", s)
        if m:
            names.update(canonicalize_name(x) for x in m.groups())
        m = re.search(r"The user requested(?: \(constraint\))? ([A-Za-z0-9][A-Za-z0-9._-]*)", s)
        if m:
            names.add(canonicalize_name(m.group(1)))
    return sorted(names)


def _clean_req(text: str) -> tuple[str | None, str]:
    """Return (requirement, "") or (None, drop reason)."""
    text = re.sub(r"\s+\(from [^)]*\)\s*$", "", text.strip())
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s+\S+$", text) and not re.search(
            r"[<>=!~]", text):
        return None, "local_or_url"          # "pkg 1.0 (from /path)" form
    if "+" in text and re.search(r"[=<>]=?\s*[^,;\s]*\+", text):
        return None, "local_version"
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return None, "unparseable"
    if req.url:
        return None, "local_or_url"
    if req.extras:
        return None, "extras"
    if req.marker is not None:
        return None, "marker"
    return str(req), ""


def _from_command(body: str):
    """Method B: (snippet, [args]) or None."""
    for blk in FENCE_RE.findall(body):
        err = ERROR_RE.search(blk)
        if not err:
            continue
        before = blk[: err.start()].splitlines()
        for line in reversed(before):
            m = CMD_RE.search(_strip_prefix(line))
            if not m:
                continue
            args_text = m.group(1).split("#", 1)[0].strip()
            try:
                args = shlex.split(args_text)
            except ValueError:
                return line.strip(), None
            if args in (["pip"], ["--upgrade", "pip"], ["-U", "pip"]):
                continue                      # upgrading pip, not the request
            return line.strip(), args
    return None


def _method_a(body: str, case: Case) -> Case:
    blocks = [b for b in conflict_blocks(body) if _user_requested(b)]
    if not blocks:
        return case.drop("no_request")
    block = blocks[0]
    case.method, case.snippet = "A", "\n".join(block)
    case.original_conflict = _conflict_packages(block)
    reqs = []
    for text, kind in _user_requested(block):
        if kind == "constraint":
            return case.drop("constraint")
        req, why = _clean_req(text)
        if req is None:
            return case.drop(why)
        reqs.append(req)
    case.requirements = sorted(dict.fromkeys(reqs))
    return case


def _method_b(body: str, case: Case) -> Case:
    found = _from_command(body)
    if not found:
        return case.drop("no_request")
    line, args = found
    case.method, case.snippet = "B", line
    case.original_conflict = _conflict_packages(next(iter(conflict_blocks(body)), []))
    if args is None:
        return case.drop("unparseable")
    reqs, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        flag = a.split("=", 1)[0]
        if flag in _FILE:
            return case.drop("requirements_file")
        if flag in _INDEX:
            return case.drop("non_pypi_index")
        if flag in _HARMLESS:
            skip = flag in _TAKES_VALUE and "=" not in a
            continue
        if a.startswith("-"):
            return case.drop("unsupported_flag")
        if a.startswith((".", "/", "~")) or "://" in a or a.endswith(
                (".whl", ".tar.gz", ".zip")):
            return case.drop("local_or_url")
        req, why = _clean_req(a)
        if req is None:
            return case.drop(why)
        reqs.append(req)
    if not reqs:
        return case.drop("no_request")
    case.requirements = sorted(dict.fromkeys(reqs))
    return case


def screen_issue(issue: dict) -> Case:
    body = issue.get("body") or ""

    def fresh():
        return Case(url=issue["url"], repo=issue["repo"], source=issue["source"],
                    created_at=issue["created_at"], title=issue.get("title", ""),
                    body_sha256=issue.get("body_sha256", ""),
                    reported_python=sorted(set(_PY_RE.findall(body))))

    if issue["created_at"][:10] > SNAPSHOT_END:
        return fresh().drop("after_cutoff")
    b = _method_b(body, fresh())
    if b.status == "kept":
        return b
    a = _method_a(body, fresh())
    if a.status == "kept" or a.reason != "no_request":
        return a
    return b            # B's reason, or no_request when neither found anything


def screen(issues) -> list[Case]:
    """Screen every issue; later duplicates of a request are dropped."""
    cases = sorted((screen_issue(i) for i in issues), key=lambda c: (c.created_at, c.url))
    seen = set()
    for c in cases:
        if c.status != "kept":
            continue
        key = tuple(c.requirements)
        if key in seen:
            c.drop("duplicate")
        seen.add(key)
    return cases


# ── command line ─────────────────────────────────────────────────────

def verify(raw_issues, cases) -> list[str]:
    """Check every kept case against the text it was extracted from:
    the post's hash matches, and every snippet line occurs in the post.
    Returns a list of problems (empty means verified)."""
    import hashlib
    bodies = {i["url"]: i.get("body") or "" for i in raw_issues}
    problems = []
    for c in cases:
        if c["status"] != "kept":
            continue
        body = bodies.get(c["url"])
        if body is None:
            problems.append(f"{c['url']}: not in the collected issues")
            continue
        if hashlib.sha256(body.encode()).hexdigest() != c["body_sha256"]:
            problems.append(f"{c['url']}: post text differs from the hash")
        for line in c["snippet"].splitlines():
            if line.strip() and line.strip() not in body:
                problems.append(f"{c['url']}: snippet line not in post: {line[:60]!r}")
        for r in c["requirements"]:
            name = re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0]
            if name.lower() not in c["snippet"].lower():
                problems.append(f"{c['url']}: {name} not in its snippet")
    return problems


def main(argv=None) -> int:
    import argparse
    import dataclasses
    import json
    import sys
    from collections import Counter

    ap = argparse.ArgumentParser(description="screen or verify the corpus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("screen")
    s.add_argument("--raw", required=True)
    s.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--raw", required=True)
    v.add_argument("--screened", required=True)
    a = ap.parse_args(argv)
    raw = [json.loads(l) for l in open(a.raw)]
    if a.cmd == "screen":
        cases = screen(raw)
        with open(a.out, "w") as f:
            for c in cases:
                f.write(json.dumps(dataclasses.asdict(c), sort_keys=True) + "\n")
        print(Counter((c.status, c.reason) for c in cases))
        return 0
    cases = [json.loads(l) for l in open(a.screened)]
    problems = verify(raw, cases)
    kept = sum(c["status"] == "kept" for c in cases)
    for p in problems:
        print(p)
    print(f"verified {kept} kept cases: {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
