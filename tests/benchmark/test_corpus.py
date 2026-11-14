"""Screening rules for real issues, one test per rule.  The issue texts are
minimal hand-written imitations of the formats seen in the collected data."""

import pytest

from crux.benchmark.corpus import screen, screen_issue

FENCE = "`" * 3


def issue(body, created="2025-03-01T10:00:00Z", url="https://github.com/o/r/issues/1"):
    return {"url": url, "repo": "o/r", "source": "gh", "created_at": created,
            "title": "t", "body": body, "body_sha256": "x"}


PIP_A = """Installing fails:
ERROR: Cannot install numpy==2.3.5 and scipy because these package versions have conflicting dependencies.

The conflict is caused by:
    The user requested numpy==2.3.5 (from requirements.txt)
    scipy 1.10.0 depends on numpy<1.27.0,>=1.19.5

To fix this you could try to:
"""


def test_method_a_takes_pip_user_requested_lines():
    c = screen_issue(issue(PIP_A))
    assert c.status == "kept" and c.method == "A"
    assert c.requirements == ["numpy==2.3.5"]
    assert c.original_conflict == ["numpy", "scipy"]
    assert "The user requested numpy==2.3.5" in c.snippet


def test_ci_log_prefixes_are_stripped():
    body = ("2024-05-01T12:00:00.1234567Z The conflict is caused by:\n"
            "2024-05-01T12:00:00.1234567Z     The user requested attrs>=22\n"
            "2024-05-01T12:00:00.1234567Z     cattrs 1.0 depends on attrs<20\n")
    c = screen_issue(issue(body))
    assert c.status == "kept" and c.requirements == ["attrs>=22"]


def test_only_the_first_block_is_used():
    body = PIP_A + "\nThen I tried:\nThe conflict is caused by:\n    The user requested numpy==1.0\n"
    assert screen_issue(issue(body)).requirements == ["numpy==2.3.5"]


@pytest.mark.parametrize("line, reason", [
    ("The user requested (constraint) requests==2.31.0", "constraint"),
    ("The user requested pvlib 0+untagged.1.g04a523f (from /pvlib-python)", "local_or_url"),
    ("The user requested platform-utils 1.6.0 (from git+https://github.com/a/b)", "local_or_url"),
    ("The user requested requests[security]>=2", "extras"),
    ('The user requested foo>=1; python_version < "3.9"', "marker"),
    ("The user requested torch==2.1.0+cu118", "local_version"),
    ("The user requested ???", "unparseable"),
])
def test_method_a_drop_reasons(line, reason):
    body = f"The conflict is caused by:\n    {line}\n    x 1.0 depends on y<1\n"
    c = screen_issue(issue(body))
    assert (c.status, c.reason) == ("dropped", reason)


def cmd_body(cmd):
    return (f"{FENCE}\n$ pip install --upgrade pip\n$ {cmd}\n"
            "ERROR: ResolutionImpossible: for help visit ...\n{FENCE}\n").replace("{FENCE}", FENCE)


def test_method_b_takes_the_last_command_before_the_error():
    c = screen_issue(issue(cmd_body("pip install -U foo==1.0 bar>=2")))
    assert c.status == "kept" and c.method == "B"
    assert c.requirements == ["bar>=2", "foo==1.0"]


def test_method_b_is_preferred_to_a_when_valid():
    body = PIP_A + "\n" + cmd_body("pip install numpy==2.3.5 scipy==1.10.0")
    c = screen_issue(issue(body))
    assert c.method == "B" and c.requirements == ["numpy==2.3.5", "scipy==1.10.0"]


def test_a_is_used_when_the_command_is_not_usable():
    body = PIP_A + "\n" + cmd_body("pip install -r requirements.txt")
    c = screen_issue(issue(body))
    assert c.method == "A" and c.requirements == ["numpy==2.3.5"]


@pytest.mark.parametrize("cmd, reason", [
    ("pip install -r requirements.txt", "requirements_file"),
    ("pip install torch --index-url https://download.pytorch.org/whl/cu121", "non_pypi_index"),
    ("pip install --pre foo", "unsupported_flag"),
    ("pip install ./local_pkg", "local_or_url"),
    ("pip install git+https://github.com/a/b", "local_or_url"),
])
def test_method_b_drop_reasons(cmd, reason):
    c = screen_issue(issue(cmd_body(cmd)))
    assert (c.status, c.reason) == ("dropped", reason)


def test_uv_pip_install_is_a_command():
    body = f"{FENCE}\nuv pip install foo==1 bar==2\n  x No solution found when resolving dependencies:\n{FENCE}\n"
    assert screen_issue(issue(body)).requirements == ["bar==2", "foo==1"]


def test_after_cutoff_and_no_request():
    assert screen_issue(issue(PIP_A, created="2026-11-14T00:00:00Z")).reason == "after_cutoff"
    assert screen_issue(issue("it broke, ResolutionImpossible")).reason == "no_request"


def test_later_duplicates_are_dropped():
    a = issue(PIP_A, created="2025-01-01T00:00:00Z", url="https://github.com/o/r/issues/1")
    b = issue(PIP_A, created="2025-02-01T00:00:00Z", url="https://github.com/o/r/issues/2")
    cases = {c.url: c for c in screen([b, a])}
    assert cases[a["url"]].status == "kept"
    assert cases[b["url"]].reason == "duplicate"


def test_verify_accepts_a_faithful_case_and_catches_tampering():
    import dataclasses
    import hashlib

    from crux.benchmark.corpus import verify
    raw = issue(PIP_A)
    raw["body_sha256"] = hashlib.sha256(PIP_A.encode()).hexdigest()
    case = dataclasses.asdict(screen_issue(raw))
    assert verify([raw], [case]) == []
    forged = dict(case, snippet=case["snippet"] + "\nThe user requested evil==1")
    assert verify([raw], [forged])
    edited = dict(raw, body=PIP_A + " edited")
    assert verify([edited], [case])
