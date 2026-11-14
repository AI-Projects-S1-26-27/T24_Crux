"""C2 (MRV) and C3 (LCV): ordering heuristics for the backtracking search.

Both only change the ORDER in which the search tries things.  They cannot
remove a solution, so SAT/UNSAT never changes; only the effort does.  Each
takes the live ``Search`` object and reads its current state through the same
``domain()`` the search itself uses (so AC-3 pruning, when on, is respected).

MRV (minimum remaining values, Haralick & Elliott 1980): assign next the
active package with the fewest admissible versions left.  The most constrained
package fails soonest, and a package whose domain is already empty is chosen
first, which turns a hidden dead end into an immediate one.  Ties go to the
package that was activated first, so the order is deterministic.

LCV (least constraining value): within a package, try first the version that
rules out the fewest options for the packages it requires.  A version's cost
is the sum, over its dependencies q, of how many of q's current candidate
versions the requirement excludes.  An already-assigned q has one candidate,
so a version incompatible with it costs 1 and is tried last.  Equal costs keep
the newest-first order, which is also the plain-search default, so LCV only
departs from "newest first" when it has a reason to.
"""

from __future__ import annotations


def mrv_select(search):
    """The unassigned active package with the smallest domain, or None."""
    best, best_n = None, None
    for pkg in search.order:                    # activation order = tie-break
        if pkg in search.assign:
            continue
        n = len(search.domain(pkg))
        if best is None or n < best_n:
            best, best_n = pkg, n
            if n == 0:
                break                           # nothing beats an empty domain
    return best


def _candidates(search, q):
    """q's current candidate versions: the live domain if q is active, else
    everything it could be (admissible for no constraint, minus AC-3 losers)."""
    if q in search.active:
        return search.domain(q)
    cands = search.graph.candidates(q, "")
    if search.ac is not None:
        alive = search.ac.domains.get(q, ())
        cands = tuple(v for v in cands if v in alive)
    return cands


def lcv_cost(search, pkg, version) -> int:
    """How many options choosing pkg=version rules out for its dependencies."""
    g = search.graph
    cost = 0
    for con in search._edges_of(pkg, version):
        allowed = g.allowed(con.dep, con.specifier)
        chosen = search.assign.get(con.dep)
        if chosen is not None:
            cost += 0 if chosen in allowed else 1
        else:
            cost += sum(1 for w in _candidates(search, con.dep)
                        if w not in allowed)
    return cost


def _cost_cached(search, pkg, version, cache):
    """Exactly ``lcv_cost``, but each neighbour's domain and each distinct
    requirement are worked out once per ``lcv_order`` call, not once per
    candidate version.

    cost(dep, spec) = |domain| - |domain & allowed| is the number of the
    neighbour's candidates the requirement excludes, the same count as the
    straightforward loop in ``lcv_cost`` (which stays as the reference the
    tests compare this against).  On the real boto3 graph (2,146 candidate
    versions, each with its own exact pin on botocore) this took LCV from
    6.6 s to a fraction of that: the old code rebuilt botocore's 2,420-version
    domain for every candidate and hashed every ``Version`` in it each time.
    """
    g = search.graph
    cost = 0
    for con in search._edges_of(pkg, version):
        dep, spec = con.dep, con.spec
        allowed = cache.get(("allowed", dep, spec))
        if allowed is None:
            allowed = cache[("allowed", dep, spec)] = g.allowed(
                dep, con.specifier)
        chosen = search.assign.get(dep)
        if chosen is not None:
            cost += 0 if chosen in allowed else 1
            continue
        excluded = cache.get(("excluded", dep, spec))
        if excluded is None:
            dom = cache.get(("domain", dep))
            if dom is None:
                dom = cache[("domain", dep)] = frozenset(
                    _candidates(search, dep))
            excluded = cache[("excluded", dep, spec)] = \
                len(dom) - len(dom & allowed)
        cost += excluded
    return cost


def lcv_order(search, pkg, versions):
    """*versions* sorted by ascending LCV cost; ties keep their input order."""
    cache = {}
    scored = [(_cost_cached(search, pkg, v, cache), i, v)
              for i, v in enumerate(versions)]
    scored.sort(key=lambda t: (t[0], t[1]))
    return tuple(v for _, _, v in scored)
