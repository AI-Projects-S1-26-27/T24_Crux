"""Read-only dependency graph for the solver.

CLI: python -m crux.data.graph   (prints load time and peak memory)
"""

import sqlite3
from datetime import datetime

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


# why_missing() reasons that are not ingest/clean reason codes (those are in
# crux.data.config and come straight from the drops table).
AFTER_AS_OF = "after_as_of"   # in the database, uploaded after load(as_of=...)
PRERELEASE = "prerelease"     # in the graph, but the specifier does not opt in

# drops-table stages whose rows are whole releases (stage "edge" rows are
# Requires-Dist lines, not versions).
_RELEASE_STAGES = ("ingest", "clean")


def _as_spec(specifier):
    """Accept a SpecifierSet or a plain string (deps() already coerces str)."""
    if isinstance(specifier, SpecifierSet):
        return specifier
    return SpecifierSet(specifier or "")


class Graph:
    def __init__(self):
        self._versions = {}   # pkg -> list[Version], newest first
        self._deps = {}       # (pkg, Version) -> list[(dep_name, SpecifierSet)]
        self._allowed = {}    # (dep_name, str(spec)) -> frozenset[Version]
        self._candidates = {}  # (dep_name, str(spec)) -> tuple[Version, ...]
        self._reverse = None  # dep_name -> tuple[(pkg, Version, SpecifierSet)]
        self.collisions = []  # releases whose version strings compare equal
        self.allowed_misses = 0
        self._db = None       # database path, for why_missing() lookups
        self._drops = {}      # pkg -> list[(version_str, reason)], from_dict / cache
        self._excluded = {}   # pkg -> list[version_str] left out by as_of
        self._missing = {}    # (pkg, str(spec)) -> why_missing() result

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d, drops=None):
        """d = {pkg: {version_str: [[dep_name, spec_str], ...]}}.

        *drops*, optional, is what why_missing() reports for releases that
        are not in *d*: {pkg: [[version_str, reason], ...]}.  Without it a
        dict-built graph knows no drop reasons, only pre-releases.
        """
        rels = [(p, v) for p, vs in d.items() for v in vs]
        edges = [(p, v, dep, spec)
                 for p, vs in d.items() for v, es in vs.items()
                 for dep, spec in es]
        g = cls._build(rels, edges)
        for pkg, rows in (drops or {}).items():
            g._drops.setdefault(canonicalize_name(pkg), []).extend(
                (str(v), str(r)) for v, r in rows)
        return g

    @classmethod
    def load(cls, db_path, as_of=None):
        """Load releases/deps; drop releases uploaded after *as_of*."""
        conn = sqlite3.connect(db_path)
        try:
            rels, excluded = [], []
            for name, ver, ut in conn.execute(
                    "SELECT name, version, upload_time FROM releases"):
                if as_of is not None and ut and \
                        datetime.fromisoformat(ut) > as_of:
                    excluded.append((name, ver))
                    continue
                rels.append((name, ver))
            edges = conn.execute(
                "SELECT name, version, dep_name, specifier FROM deps"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    f"{db_path} has no releases/deps tables yet -- run"
                    " `python -m crux.data.run_ingest` then"
                    " `python -m crux.data.clean` first"
                ) from exc
            raise
        finally:
            conn.close()
        g = cls._build(rels, edges)
        g._db = str(db_path)
        for name, ver in excluded:
            g._excluded.setdefault(canonicalize_name(name), []).append(ver)
        return g

    @classmethod
    def _build(cls, rels, edges):
        g = cls()
        obj = {}   # (pkg, ver_str) -> shared Version
        for pkg, ver in rels:
            pkg = canonicalize_name(pkg)
            v = Version(ver)
            obj[(pkg, ver)] = v
            if (pkg, v) in g._deps:
                # Two distinct version STRINGS that compare equal under
                # PEP 440 -- e.g. "1.0" and "1.0.0".  `releases` is keyed on
                # the string but this graph is keyed on the Version object,
                # so the two releases collapse into one node and their
                # dependency edges merge.  PyPI's Simple API returns
                # normalised versions, so this should not happen; record it
                # rather than let it pass silently.
                g.collisions.append((pkg, ver))
                continue
            g._versions.setdefault(pkg, []).append(v)
            g._deps[(pkg, v)] = []
        for vs in g._versions.values():
            vs.sort(reverse=True)
        specs = {}  # share SpecifierSet objects too
        for pkg, ver, dep, spec in edges:
            v = obj.get((canonicalize_name(pkg), ver))
            if v is None:          # release excluded (as_of) or unknown
                continue
            s = specs.get(spec)
            if s is None:
                s = specs[spec] = SpecifierSet(spec)
            g._deps[(canonicalize_name(pkg), v)].append(
                (canonicalize_name(dep), s))
        return g

    # ── queries ──────────────────────────────────────────────────────

    def versions(self, pkg):
        return self._versions.get(canonicalize_name(pkg), [])

    def deps(self, pkg, version):
        if isinstance(version, str):
            version = Version(version)
        return self._deps.get((canonicalize_name(pkg), version), [])

    def allowed(self, dep_name, specifier):
        """Versions of *dep_name* matching *specifier*, as an unordered set.

        *specifier* may be a SpecifierSet or a plain string.  Use
        candidates() instead when the order matters, which for a solver
        choosing what to try next it usually does.
        """
        dep_name = canonicalize_name(dep_name)
        specifier = _as_spec(specifier)
        key = (dep_name, str(specifier))
        hit = self._allowed.get(key)
        if hit is None:
            self.allowed_misses += 1
            hit = self._allowed[key] = frozenset(
                specifier.filter(self.versions(dep_name)))
        return hit

    def candidates(self, dep_name, specifier):
        """Same versions as allowed(), as a tuple ordered newest first.

        This is the call a solver wants in its inner loop: picking the
        newest matching version is O(1) instead of a scan of every version
        of *dep_name* (about 2,000 of them for boto3).  Memoized on
        (dep_name, str(specifier)), sharing allowed()'s cache, so the filter
        runs once per distinct requirement.
        """
        dep_name = canonicalize_name(dep_name)
        specifier = _as_spec(specifier)
        key = (dep_name, str(specifier))
        hit = self._candidates.get(key)
        if hit is None:
            ok = self.allowed(dep_name, specifier)
            hit = self._candidates[key] = tuple(
                v for v in self.versions(dep_name) if v in ok)
        return hit

    def dependents(self, dep_name):
        """Releases that depend on *dep_name*.

        Returns a tuple of (pkg, Version, SpecifierSet) -- the reverse of
        deps().  This is what answers "which requirements constrain this
        package", for conflict analysis and for explaining a failure.  The
        reverse index is built on first use and then cached, so load() stays
        fast for callers that never ask for it.
        """
        if self._reverse is None:
            rev = {}
            for (pkg, v), es in self._deps.items():
                for dep, spec in es:
                    rev.setdefault(dep, []).append((pkg, v, spec))
            self._reverse = {k: tuple(val) for k, val in rev.items()}
        return self._reverse.get(canonicalize_name(dep_name), ())

    def why_missing(self, dep_name, specifier):
        """Releases of *dep_name* that match *specifier* but that the solver
        cannot use, each with the reason.

        Returns a tuple of (version_str, reason), newest first.  This is what
        an explanation needs when candidates() is empty: whether the version
        asked for was dropped (``no_wheel``, ``yanked``, ``py_version``,
        ``after_cutoff``, ``no_metadata`` and the other reason codes in
        crux.data.config), uploaded after the ``as_of`` the graph was loaded
        with (``after_as_of``), or is a pre-release the specifier does not
        opt into (``prerelease``).  An empty result means no such release
        is known at all.

        Matching includes pre-releases, so a dropped ``2.0rc1`` is reported
        for ``>=2``.  Versions that are not valid PEP 440 (reason
        ``invalid_version``) cannot be compared and are never reported.
        For a loaded graph the drops table is read on first use per package
        (indexed), so load() is unaffected; results are memoized.
        """
        dep_name = canonicalize_name(dep_name)
        specifier = _as_spec(specifier)
        key = (dep_name, str(specifier))
        hit = self._missing.get(key)
        if hit is not None:
            return hit
        rows = set(self._drop_rows(dep_name))
        rows |= {(v, AFTER_AS_OF) for v in self._excluded.get(dep_name, ())}
        usable = self.allowed(dep_name, specifier)
        rows |= {(str(v), PRERELEASE) for v in self.versions(dep_name)
                 if v.is_prerelease and v not in usable}
        out = []
        for ver, reason in rows:
            try:
                v = Version(ver)
            except InvalidVersion:
                continue
            # explicit, so a dropped pre-release is reported whatever
            # packaging's default for contains() is (26.0 already says yes)
            if specifier.contains(v, prereleases=True):
                out.append((v, ver, reason))
        out.sort(key=lambda x: (x[0], x[2]), reverse=True)
        hit = self._missing[key] = tuple((ver, r) for _, ver, r in out)
        return hit

    def _drop_rows(self, pkg):
        if pkg in self._drops or self._db is None:
            return self._drops.get(pkg, [])
        conn = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(_RELEASE_STAGES))
            rows = conn.execute(
                "SELECT version, reason FROM drops"
                f" WHERE stage IN ({marks}) AND name = ?",
                (*_RELEASE_STAGES, pkg)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            rows = []
        finally:
            conn.close()
        self._drops[pkg] = [(v, r) for v, r in rows if v is not None]
        return self._drops[pkg]

    def dangling(self):
        return [(pkg, v, dep)
                for (pkg, v), es in self._deps.items()
                for dep, _ in es if not self._versions.get(dep)]


if __name__ == "__main__":
    import time
    import tracemalloc

    from crux.data.config import DB_PATH

    tracemalloc.start()
    t0 = time.perf_counter()
    g = Graph.load(DB_PATH)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    n_rel = sum(len(v) for v in g._versions.values())
    n_edge = sum(len(e) for e in g._deps.values())
    print(f"packages {len(g._versions)}  releases {n_rel}  edges {n_edge}"
          f"  dangling {len(g.dangling())}"
          f"  collisions {len(g.collisions)}")
    print(f"load {dt:.2f}s  peak {peak / 2**20:.1f} MiB")
