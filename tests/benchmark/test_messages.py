"""Message metrics, measured on real pip 26.2.1 / pip 25.0.1 / uv 0.11.32
output captured from planted/direct/d1/none/s0 (tests/fixtures/benchmark).

The instance: roots glyph>=1.1.0 and tamarack; glyph 1.1.0 needs beacon<1.7.1,
every tamarack needs beacon>=1.7.1; beacon has releases."""

from pathlib import Path

import pytest

from crux.benchmark.messages import measure, message, named_packages

FIX = Path(__file__).parent.parent / "fixtures" / "benchmark"
PACKAGES = {"glyph", "tamarack", "beacon"}
ROOTS = ["glyph", "tamarack"]
CORE = ["beacon", "glyph", "tamarack"]


def _m(tool, name, stream="stdout"):
    text = (FIX / name).read_text()
    out, err = (text, "") if stream == "stdout" else ("", text)
    return measure(tool, out, err, packages=PACKAGES, roots=ROOTS,
                   core_packages=CORE, has_releases=lambda p: p in PACKAGES)


def test_pip_message_starts_at_the_first_error_line():
    msg = message("pip", (FIX / "pip26_direct.txt").read_text())
    assert msg.startswith("ERROR: Cannot install")
    assert "Processing" not in msg and "INFO:" not in msg
    assert "The conflict is caused by:" in msg      # it comes from stdout


def test_pip26_direct_metrics():
    m = _m("pip", "pip26_direct.txt")
    assert m["msg_lines"] == 11
    assert m["statements"] == 3 and m["distinct_statements"] == 3
    assert m["core_recall"] == 1.0 and m["names_root"] is True
    # pip 26 says beacon has no matching distributions; beacon has releases
    assert m["unavailable_claimed"] == 1
    assert m["false_unavailable"] == 1


def test_pip25_does_not_print_the_availability_section():
    m = _m("pip-baseline", "pip25_direct.txt")
    assert m["unavailable_claimed"] == 0 and m["false_unavailable"] == 0
    assert m["statements"] == 4 and m["distinct_statements"] == 3   # a repeat


def test_uv_direct_metrics():
    m = _m("uv", "uv_direct.txt", stream="stderr")
    assert m["msg_lines"] == 6 and m["out_lines"] == 6
    assert m["core_recall"] == 1.0 and m["names_root"] is True
    assert "false_unavailable" not in m


def test_a_true_unavailability_claim_is_not_counted_false():
    """impossible/d0/none/s0 asks for a package with no releases at all."""
    text = (FIX / "pip26_missing.txt").read_text()
    m = measure("pip", text, "", packages={"x"}, roots=["x"], core_packages=None,
                has_releases=lambda p: False)
    assert m["msg_lines"] == 2
    assert m["false_unavailable"] == 0


@pytest.mark.parametrize("text, expected", [
    ("a depends on b>=1", {"a", "b"}),
    ("requires a-b==1.0", {"a-b"}),          # whole hyphenated name
    ("requires a-bc", set()),                # "a-b" is not "a-bc"
    ("see foo.a and a.b", set()),            # dotted words are not names
    ("all versions of b depend on a", {"a", "b"}),
    ("you require a.", {"a"}),               # sentence-final name counts
])
def test_named_packages_matches_whole_names(text, expected):
    assert named_packages(text, {"a", "b", "a-b"}) == expected


def test_unknown_tool_is_rejected():
    with pytest.raises(ValueError):
        message("poetry", "")
