"""The library-wide error root (ADR-0008, ADR-0013).

``Net2BoardError`` is the root of the library's structured failure
surface. It owns nothing itself — the phase roots keep their homes and
their contracts: ``BoardSpecError`` (spec binding, ADR-0008) extends it
here, and the solve surface's ``SolveError`` (ADR-0013) joins the same
tree from ``net2board.solver``. Malformed-syntax ``ValueError`` s stay
outside the tree by design (ADR-0008). The reconciliation is structural
only: no subclass set, carried field, or message moves.
"""

from __future__ import annotations

__all__ = [
    "Net2BoardError",
]


class Net2BoardError(Exception):
    """Root of the library's failure surface (ADR-0013's reconciliation)."""
