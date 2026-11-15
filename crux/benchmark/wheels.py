"""Write an instance's graph as metadata-only wheels, so pip and uv resolve
exactly the data Crux resolves.

Each release becomes a minimal valid wheel holding only METADATA (with its
Requires-Dist lines), WHEEL and RECORD.  Resolvers read only metadata, so
nothing else is needed.  Every wheel is tagged ``py3-none-any`` and carries no
Requires-Python: the data layer has already applied the CPython 3.12 /
Linux x86-64 filter, dropped false markers and extras, and the graph holds
what is left.  pip and uv then read the folder with
``--no-index --find-links DIR``: no index server, no network.

This is the per-instance form of the proposal's "local index mirror built from
the same snapshot, so all tools see identical metadata".
"""

from __future__ import annotations

import base64
import hashlib
import re
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

_ZIP_DATE = (2026, 11, 1, 0, 0, 0)     # fixed timestamps: identical bytes


def wheel_name(pkg: str) -> str:
    """Distribution name as written in wheel file names (PEP 427 escaping)."""
    return re.sub(r"[-_.]+", "_", canonicalize_name(pkg))


def _record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return f"{path},sha256={digest.rstrip(b'=').decode()},{len(data)}"


def write_wheel(out: Path, pkg: str, version: str, deps) -> Path:
    name, ver = wheel_name(pkg), str(Version(version))
    dist = f"{name}-{ver}.dist-info"
    meta = ("Metadata-Version: 2.1\n"
            f"Name: {canonicalize_name(pkg)}\n"
            f"Version: {ver}\n"
            + "".join(f"Requires-Dist: {dep}{spec}\n" for dep, spec in deps)
            ).encode()
    wheel = (b"Wheel-Version: 1.0\nGenerator: crux-benchmark\n"
             b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    files = {f"{dist}/METADATA": meta, f"{dist}/WHEEL": wheel}
    record = ("\n".join(_record_line(p, d) for p, d in files.items())
              + f"\n{dist}/RECORD,,\n").encode()
    files[f"{dist}/RECORD"] = record
    path = out / f"{name}-{ver}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for p, d in files.items():
            z.writestr(zipfile.ZipInfo(p, _ZIP_DATE), d)
    return path


def write_index(graph: dict, out) -> int:
    """Write one wheel per release in ``graph`` ({pkg: {ver: [[dep, spec]]}})
    into the directory ``out``.  Returns the number of wheels written."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for pkg, versions in graph.items():
        for ver, deps in versions.items():
            write_wheel(out, pkg, ver, deps)
            n += 1
    return n
