"""Measure an error message against what is known about the instance.

Definitions (fixed here so every number in the report means one thing):

message     what a user reads as the explanation.
            pip: every line from the first line starting "ERROR:" to the end
            of pip's output, stdout and stderr merged in the order printed
            (pip puts "ERROR:" on stderr but "The conflict is caused by:" on
            stdout); progress and "INFO:" lines before it are excluded.
            uv: every line of stderr, minus lines starting "warning:".
msg_lines   non-empty lines of the message.
out_lines   non-empty lines of ALL output (stdout + stderr), i.e. everything
            printed before the user can stop reading.
named       packages of the instance that the message mentions as a whole
            word: not preceded by a letter, digit, "_", "-" or "."; not
            followed by one of those, nor by "." plus a letter (so "beacon."
            ending a sentence counts, "a.b" does not).
core_recall     share of the planted core's packages the message names.
core_precision  share of named packages that are in the planted core.
names_root  the message names at least one package the user asked for.
statements  pip: lines under "The conflict is caused by:"; uv: occurrences
            of "depend(s) on" or "you require".  A rough count of the
            requirement statements a reader has to take in.
distinct_statements  the same, counting each different statement once
            (pip sometimes prints the same line twice).
false_unavailable   pip only: packages pip lists as having "no matching
            distributions available" although the instance has releases of
            them, i.e. a false statement in the message.  Why "false" and not
            "true but unhelpful": pip prints a package there when
            ``Factory._has_any_candidates`` returns False, and that method's
            own docstring is "Check if there are any candidates available for
            the project name" -- any release at all, whatever the version
            requirements (pip 26.2.1, pip/_internal/resolution/resolvelib/
            factory.py).  Reproduced against real PyPI: pip 26.2.1 lists
            botocore for ``boto3==1.34.0 botocore==1.29.0``; pip 25.0.1 does
            not print the section.
"""

from __future__ import annotations

import re

_PIP_UNAVAILABLE = ("some packages in these conflicts have no matching "
                    "distributions available")


def message(tool: str, text: str) -> str:
    """``text``: pip's merged output, or uv's stderr."""
    lines = text.splitlines()
    if tool.startswith("pip"):
        for i, line in enumerate(lines):
            if line.startswith("ERROR:"):
                return "\n".join(lines[i:])
        return ""
    if tool.startswith("uv"):
        return "\n".join(l for l in lines if not l.lower().startswith("warning:"))
    raise ValueError(f"no message rule for tool {tool!r}")


def _nonempty(text: str) -> int:
    return sum(1 for l in text.splitlines() if l.strip())


def named_packages(text: str, packages) -> set[str]:
    out = set()
    for p in packages:
        if re.search(rf"(?<![\w.-]){re.escape(p)}(?![\w-]|\.\w)", text):
            out.add(p)
    return out


def _pip_statements(msg: str) -> list[str]:
    lines, out, inside = msg.splitlines(), [], False
    for line in lines:
        if line.strip() == "The conflict is caused by:":
            inside = True
            continue
        if inside:
            if not line.strip():
                break
            out.append(line.strip())
    return out


def _pip_unavailable(msg: str) -> list[str]:
    lines, out, inside = msg.splitlines(), [], False
    for line in lines:
        if _PIP_UNAVAILABLE in line:
            inside = True
            continue
        if inside:
            if not line.strip():
                break
            out.append(line.strip())
    return out


def measure(tool: str, stdout: str, stderr: str, *, packages, roots,
            core_packages=None, has_releases=None) -> dict:
    """``packages``: every package name of the instance (including names that
    have no releases); ``roots``: requested package names;
    ``core_packages``: planted core packages, or None if unknown;
    ``has_releases(name) -> bool`` for pip's availability claims."""
    msg = message(tool, stdout if tool.startswith("pip") else stderr)
    named = named_packages(msg, packages)
    m = {
        "msg_lines": _nonempty(msg),
        "out_lines": _nonempty(stdout) + _nonempty(stderr),
        "named": len(named),
        "names_root": bool(named & set(roots)),
    }
    if core_packages is not None:
        core = set(core_packages)
        hit = named & core
        m["core_recall"] = len(hit) / len(core) if core else None
        m["core_precision"] = len(hit) / len(named) if named else None
    if tool.startswith("pip"):
        stmts = _pip_statements(msg)
        m["statements"], m["distinct_statements"] = len(stmts), len(set(stmts))
        claimed = _pip_unavailable(msg)
        m["unavailable_claimed"] = len(claimed)
        m["false_unavailable"] = (sum(1 for p in claimed if has_releases(p))
                                  if has_releases else None)
    else:
        found = re.findall(r"[^.,;]*\b(?:depends? on|you require)\b[^.,;]*", msg)
        m["statements"] = len(found)
        m["distinct_statements"] = len({" ".join(s.split()) for s in found})
    return m
