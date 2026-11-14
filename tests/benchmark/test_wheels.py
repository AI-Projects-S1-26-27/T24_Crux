"""Metadata-only wheels: valid structure, exact metadata, identical bytes."""

import base64
import hashlib
import zipfile

from crux.benchmark.wheels import wheel_name, write_index


def test_wheel_name_escaping():
    assert wheel_name("Foo.Bar-baz") == "foo_bar_baz"


def test_index_contents(tmp_path):
    graph = {"a-b": {"1.0": [["c", "<2.0"], ["d", ""]]}, "c": {"1.5": []}}
    assert write_index(graph, tmp_path) == 2
    w = tmp_path / "a_b-1.0-py3-none-any.whl"
    with zipfile.ZipFile(w) as z:
        names = set(z.namelist())
        assert names == {"a_b-1.0.dist-info/METADATA", "a_b-1.0.dist-info/WHEEL",
                         "a_b-1.0.dist-info/RECORD"}
        meta = z.read("a_b-1.0.dist-info/METADATA").decode()
        assert "Name: a-b\n" in meta and "Version: 1.0\n" in meta
        assert "Requires-Dist: c<2.0\n" in meta and "Requires-Dist: d\n" in meta
        assert "Requires-Python" not in meta
        # RECORD hashes are correct
        for line in z.read("a_b-1.0.dist-info/RECORD").decode().splitlines():
            path, digest, size = line.split(",")
            if not digest:
                continue
            data = z.read(path)
            want = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            assert digest == "sha256=" + want.decode() and int(size) == len(data)


def test_bytes_are_deterministic(tmp_path):
    graph = {"a": {"1.0": [["b", ">=1"]]}}
    write_index(graph, tmp_path / "x")
    write_index(graph, tmp_path / "y")
    name = "a-1.0-py3-none-any.whl"
    assert (tmp_path / "x" / name).read_bytes() == (tmp_path / "y" / name).read_bytes()
