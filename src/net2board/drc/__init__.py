"""DRC — the agent's feedback channel (ADR-0009).

``run_drc(board)`` returns a deterministic, canonically-sorted tuple of
``Violation`` structs — same board, same tuple, immune to check order, scan
order, and upsert semantics. Checks are pure functions registered in the
ordered module-level ``CHECKS`` tuple: adding a check later is appending one
function. M1 ships exactly one check, courtyard overlap, whose type value
deliberately collides with the KiCad oracle's ``courtyards_overlap``.

Oracle parity notes (probed on kicad-cli 10.0.6): overlap is
strict-interior — touching edges and tangent points are not violations —
cross-side placement pairs are exempt, and courtyard-less placements are
skipped, never flagged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from net2board.geometry import Point, shapes_overlap, world_courtyard
from net2board.model import Board, Placement

__all__ = ["CHECKS", "Severity", "Violation", "ViolationType", "run_drc"]


class Severity(Enum):
    """ERROR | WARNING only — suppression is check configuration, not a
    severity (ADR-0009)."""

    ERROR = "error"
    WARNING = "warning"


class ViolationType(Enum):
    """snake_case values that collide with the oracle's type strings —
    naming convention, not a dependency (ADR-0009)."""

    COURTYARDS_OVERLAP = "courtyards_overlap"


@dataclass(frozen=True)
class Violation:
    """One DRC finding, fully actionable without holding the Board.

    Where refs and locations arities match, pairing is positional:
    ``offending_refs[i] ↔ locations[i]``. Round-trips through
    ``dataclasses.asdict`` for a future MCP layer with zero adapter code.
    """

    type: ViolationType
    severity: Severity
    description: str
    offending_refs: tuple[str, ...]
    locations: tuple[Point, ...]


def _describe(
    violation_type: ViolationType,
    refs: tuple[str, ...],
    locations: tuple[Point, ...],
) -> str:
    """The pinned wording per type — a pure function of the finding
    (ADR-0009); future checks pin theirs here when they land."""
    match violation_type:
        case ViolationType.COURTYARDS_OVERLAP:
            return f"Courtyards overlap: {refs[0]} and {refs[1]}"
    raise ValueError(f"no pinned description for violation type {violation_type!r}")


def _courtyard_overlap(board: Board) -> tuple[Violation, ...]:
    """Pairwise strict-interior courtyard overlap over same-side placements.

    Courtyard-less placements are skipped; cross-side pairs are exempt —
    both behaviours match the KiCad oracle (ADR-0006).
    """
    placements = board.placements
    courtyards = [world_courtyard(placement) for placement in placements]
    violations: list[Violation] = []
    for i, a in enumerate(placements):
        for j in range(i + 1, len(placements)):
            b = placements[j]
            yard_a, yard_b = courtyards[i], courtyards[j]
            if yard_a is None or yard_b is None or a.side != b.side:
                continue
            if shapes_overlap(yard_a, yard_b):
                violations.append(_overlap_violation(a, b))
    return tuple(violations)


def _overlap_violation(a: Placement, b: Placement) -> Violation:
    refs, locations = _canonical_pairs(
        (f"placement:{a.ref}", f"placement:{b.ref}"), (a.pos, b.pos)
    )
    return Violation(
        type=ViolationType.COURTYARDS_OVERLAP,
        severity=Severity.ERROR,
        description=_describe(ViolationType.COURTYARDS_OVERLAP, refs, locations),
        offending_refs=refs,
        locations=locations,
    )


def _canonical_pairs(
    refs: tuple[str, ...], locations: tuple[Point, ...]
) -> tuple[tuple[str, ...], tuple[Point, ...]]:
    """Re-pair refs with their locations in lexicographic ref order."""
    pairs = sorted(zip(refs, locations), key=lambda pair: pair[0])
    return (
        tuple(ref for ref, _ in pairs),
        tuple(location for _, location in pairs),
    )


CHECKS: tuple[Callable[[Board], tuple[Violation, ...]], ...] = (_courtyard_overlap,)


def run_drc(board: Board) -> tuple[Violation, ...]:
    """Run every registered check in order, canonicalize, sort explicitly.

    Canonicalization re-pairs refs with their locations in lexicographic
    ref order and rebuilds the description from the canonical form; the
    final sort key is ``(type.value, offending_refs, locations)`` — the
    same board always yields the same tuple (ADR-0009).
    """
    violations = (violation for check in CHECKS for violation in check(board))
    return tuple(sorted((_canonical(v) for v in violations), key=_sort_key))


def _canonical(violation: Violation) -> Violation:
    if len(violation.offending_refs) != len(violation.locations):
        raise ValueError(
            f"variable-arity finding from check emitting {violation.type.value!r}: "
            f"{len(violation.offending_refs)} refs vs "
            f"{len(violation.locations)} locations — canonicalization for "
            f"such findings is the emitting check's documented convention "
            f"(ADR-0009); none exists yet"
        )
    refs, locations = _canonical_pairs(violation.offending_refs, violation.locations)
    return replace(
        violation,
        offending_refs=refs,
        locations=locations,
        description=_describe(violation.type, refs, locations),
    )


def _sort_key(violation: Violation):
    return (
        violation.type.value,
        violation.offending_refs,
        tuple((point.x, point.y) for point in violation.locations),
    )
