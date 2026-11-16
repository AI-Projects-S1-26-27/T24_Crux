"""Explanation: minimal unsatisfiable cores of UNSAT resolution problems.

``core.py``    core extraction (deletion, deletion with refinement, QuickXplain)
``verify.py``  an independent check that a core is unsatisfiable and minimal
``group.py``   a core grouped into the statements a message is made of
``render.py``  the grouped core as a short English message (``message``)
``corebench``  experiment comparing the methods (``python -m crux.explain.corebench``)

Full context: docs/EXPLAIN.md.
"""

from crux.explain.core import (  # noqa: F401
    METHODS, CoreResult, extract_core, explain, is_unsat,
)
from crux.explain.verify import CoreCheck, verify_core  # noqa: F401
from crux.explain.group import Group, group_core, rules_out_package  # noqa: F401
from crux.explain.render import Message, message, render, render_core  # noqa: F401

__all__ = ["METHODS", "CoreResult", "extract_core", "explain", "is_unsat",
           "CoreCheck", "verify_core", "Group", "group_core", "rules_out_package",
           "Message", "message", "render", "render_core"]
