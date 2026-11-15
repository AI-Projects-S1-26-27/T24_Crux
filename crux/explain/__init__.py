"""Explanation: minimal unsatisfiable cores of UNSAT resolution problems.

``core.py``    core extraction (deletion, deletion with refinement, QuickXplain)
``verify.py``  an independent check that a core is unsatisfiable and minimal
``corebench``  experiment comparing the methods (``python -m crux.explain.corebench``)

Full context: docs/EXPLAIN.md.
"""

from crux.explain.core import (  # noqa: F401
    METHODS, CoreResult, extract_core, explain, is_unsat,
)
from crux.explain.verify import CoreCheck, verify_core  # noqa: F401

__all__ = ["METHODS", "CoreResult", "extract_core", "explain", "is_unsat",
           "CoreCheck", "verify_core"]
