"""The solver — public derive and search operations over placement
(ADR-0012, ADR-0013).

The public surface: ``solve`` and its ``SolveResult``/``StopReason``
(ADR-0013, with ``SolveError``/``SolveRefError`` for invalid input), and the
repair derivation ``legalize`` with its ``LegalizationResult``
(ADR-0012). The evaluation core — the flat, per-call Evaluator (ADR-0016)
in ``net2board.solver._evaluator`` — is private: created per call, never
crossing a call or this module's public surface.
"""

from net2board.solver._legalize import (
    LegalizationError,
    LegalizationResult,
    legalize,
)
from net2board.solver._solve import (
    SolveError,
    SolveRefError,
    SolveResult,
    StopReason,
    solve,
)

__all__ = [
    "LegalizationError",
    "LegalizationResult",
    "SolveError",
    "SolveRefError",
    "SolveResult",
    "StopReason",
    "legalize",
    "solve",
]
