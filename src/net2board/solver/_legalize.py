"""The public repair derivation — ``legalize`` (ADR-0012).

"Legal" is defined by reference, not enumeration: a Board is legal when
``run_drc`` reports no Violation of error severity, so repairer and
verifier cannot drift and every check registered in ``CHECKS`` joins the
legality set for free. The promise is small, bounded movement — not
minimal: a greedy nearest-feasible repair over violation-driven candidate
positions (closed-form per finding type, each verified by an Evaluator
probe before it is committed), deterministic by construction, with no
seeds, no wall clock, and no effort knob.

The guarantees, from the ADR:

- **Identity on legal input** — a legal Board returns itself, empty report
  tuples; safe to call speculatively anywhere in the loop.
- **Per-part all-or-nothing within the bound** — a part that cannot be
  repaired within ``max_displacement`` (one optional global Euclidean
  bound, integer nm) is restored to exactly where it was and named in
  ``unlegalized_refs``; the returned Board is honestly still illegal.
- **Mobility** — any unlocked placed part may move, already-legal ones
  included; locked parts never move and a locked part that is itself
  illegal is an immediate per-part failure; unplaced refs stay unplaced;
  rotation and side are untouched (translation only).
- **Intent is a wall, never a trade** — Requirements must be met, never
  relaxed; Preferences do not enter legalization at all.
- **Failure is a value** — exceptions are reserved for invalid input (a
  malformed bound raises ``LegalizationError`` under ``Net2BoardError``).

The repair runs on the warm per-call Evaluator (ADR-0016), unbudgeted;
``solve`` (ADR-0013) ends in this derivation as its final phase, for the
same reason.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from math import isqrt

from net2board.drc import (
    Severity,
    Violation,
    ViolationType,
    _anchor_members,
    _crossed_edges,
    _edge_mount_exemptions,
    _measure_box,
)
from net2board.errors import Net2BoardError
from net2board.geometry import (
    Point,
    Rectangle,
    pad_world_center,
    union_bbox,
    world_courtyard,
)
from net2board.model import Board, Edge
from net2board.solver._evaluator import Evaluator

__all__ = [
    "LegalizationError",
    "LegalizationResult",
    "legalize",
]

_MARGIN_NM = 1_000
"""The clearance a repair candidate aims for past a face it must clear —
small movement, not minimal; verified exactly by the probe."""

_MAX_REPAIR_ROUNDS = 10_000
"""Deterministic global cap on repair rounds: the loop always either
clears the first finding, names a part, or stops; this cap is the
backstop against pathological oscillation."""

_MAX_ATTEMPTS = 4
"""Deterministic cap on repair attempts per (finding, mover) pair — a
target that resisted this many rounds of candidates fails per-part."""


class LegalizationError(Net2BoardError):
    """A malformed ``legalize`` input (ADR-0008, ADR-0012): the only
    invalid input is the ``max_displacement`` bound. Partial repair
    failure is never an exception — it is
    ``LegalizationResult.unlegalized_refs``."""


@dataclass(frozen=True)
class LegalizationResult:
    """The outcome of one ``legalize`` call (ADR-0012): the repaired
    Board, the canonically sorted refs that could not be legalized (a
    locked part that is itself illegal, a part that could not be repaired
    within the bound), and each moved ref's Euclidean displacement in
    integer nm — movers only. No aggregates, no success boolean:
    ``unlegalized_refs == ()`` is the success test, and ``run_drc``
    remains the authority on what, if anything, is still wrong."""

    board: Board
    unlegalized_refs: tuple[str, ...]
    displacements: dict[str, int]


def legalize(board: Board, max_displacement: int | None = None) -> LegalizationResult:
    """Repair a Board with small, bounded movement — a pure derive op
    (ADR-0012): Board in, ``LegalizationResult`` out, deterministic,
    identity on legal input.

    Legal means no error-severity Violation in ``run_drc``. Every moved
    part ends within ``max_displacement`` (Euclidean, integer nm, from
    its original position — ``None`` is unbounded); a part that cannot,
    ends exactly where it was and is named. Locked placements never
    move; unplaced refs stay unplaced; rotation and side are untouched.
    """
    bound = _checked_bound(max_displacement)
    evaluator = Evaluator(board)
    if not any(
        finding.severity is Severity.ERROR for finding in evaluator.violations()
    ):
        return LegalizationResult(board=board, unlegalized_refs=(), displacements={})
    repairer = _Repairer(evaluator, board, bound)
    repairer.run()
    return repairer.result(board)


@dataclass(frozen=True)
class _Target:
    """A repair-loop key: which finding, and optionally which mover is
    being driven against it."""

    finding_type: str
    offending_refs: tuple[str, ...]
    mover: str = ""


def _checked_bound(max_displacement: int | None) -> int | None:
    """Validate the displacement bound — the one invalid-input surface
    (ADR-0012); everything else about a repair failure is a value."""
    if max_displacement is None:
        return None
    if isinstance(max_displacement, bool) or not isinstance(max_displacement, int):
        raise LegalizationError(
            f"max_displacement must be None or an integer (nm), "
            f"got {max_displacement!r}"
        )
    if max_displacement < 0:
        raise LegalizationError(
            f"max_displacement must be non-negative (nm), got {max_displacement!r}"
        )
    return max_displacement


def _displacement(a: Point, b: Point) -> int:
    """Euclidean distance in integer nm (ADR-0005, the ``isqrt`` stance)."""
    dx, dy = a.x - b.x, a.y - b.y
    return isqrt(dx * dx + dy * dy)


def _placement_refs(finding: Violation) -> tuple[str, ...]:
    """A finding's placement refs, the ``constraint:<id>`` lead dropped
    (ADR-0009's variable-arity convention)."""
    return tuple(
        _after_prefix(ref, "placement:")
        for ref in finding.offending_refs
        if ref.startswith("placement:")
    )


def _after_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :]


class _Repairer:
    """One legalize call's working state: the warm Evaluator, the
    positions bookkeeping, and the greediest repair loop in the ADR's
    family — first finding, first movable offender, nearest verified
    candidate, repeat."""

    def __init__(self, evaluator: Evaluator, board: Board, bound: int | None):
        self._evaluator = evaluator
        self._board = board
        self._bound = bound
        self._placements = {placement.ref: placement for placement in board.placements}
        self._positions = {
            ref: placement.pos for ref, placement in self._placements.items()
        }
        self._originals = {
            ref: placement.pos for ref, placement in self._placements.items()
        }
        self._locked = {
            ref for ref, placement in self._placements.items() if placement.locked
        }
        self._exempt = _edge_mount_exemptions(board)
        self._constraints = {
            constraint.id: constraint for constraint in board.constraints
        }
        self._failed: set[str] = set()
        self._stuck: set[_Target] = set()
        self._attempts: dict[_Target, int] = {}

    def run(self) -> None:
        for _ in range(_MAX_REPAIR_ROUNDS):
            errors = [
                finding
                for finding in self._evaluator.violations()
                if finding.severity is Severity.ERROR
            ]
            if not errors:
                return
            finding = next(
                (
                    finding
                    for finding in errors
                    if _Target(finding.type.value, finding.offending_refs)
                    not in self._stuck
                ),
                None,
            )
            if finding is None:
                break  # every finding parked: the residual sweep reports
            offender = self._choose_offender(finding)
            if offender is None:
                # a finding whose offenders are all locked or already
                # failed can never be repaired — park it, name its locked
                # parts at once (ADR-0012's immediate per-part failure),
                # and let the other findings continue
                self._failed |= {
                    ref
                    for ref in _placement_refs(finding)
                    if ref in self._locked and ref not in self._failed
                }
                self._stuck.add(_Target(finding.type.value, finding.offending_refs))
                continue
            mover_key = _Target(finding.type.value, finding.offending_refs, offender)
            self._attempts[mover_key] = self._attempts.get(mover_key, 0) + 1
            if self._attempts[mover_key] > _MAX_ATTEMPTS:
                self._restore(offender)
                self._failed.add(offender)
                continue
            accepted = self._first_feasible(finding, offender)
            if accepted is None:
                continue  # another round may do better; the cap ends it
            self._evaluator.commit(offender, accepted)
            self._positions[offender] = accepted
        # rounds exhausted: the residual sweep names the survivors
        for finding in self._evaluator.violations():
            if finding.severity is Severity.ERROR:
                self._failed.update(
                    ref for ref in _placement_refs(finding) if ref in self._placements
                )

    def result(self, board: Board) -> LegalizationResult:
        """Materialise the outcome: the movers' Board (through the public
        construction path), the canonically sorted failures, and the
        movers' displacements."""
        movers = {
            ref: pos
            for ref, pos in self._positions.items()
            if pos != self._originals[ref]
        }
        if movers:
            for ref in sorted(movers):
                placement = self._placements[ref]
                pos = self._positions[ref]
                board = board.with_placement(
                    ref,
                    pos.x,
                    pos.y,
                    placement.rotation,
                    placement.side,
                    placement.locked,
                )
        displacements = {
            ref: _displacement(self._originals[ref], self._positions[ref])
            for ref in sorted(movers)
        }
        return LegalizationResult(
            board=board,
            unlegalized_refs=tuple(sorted(self._failed)),
            displacements=displacements,
        )

    def _choose_offender(self, finding: Violation) -> str | None:
        """The finding's first movable offender in canonical ref order:
        placed, unlocked, not already failed."""
        for prefixed in finding.offending_refs:
            if not prefixed.startswith("placement:"):
                continue
            ref = _after_prefix(prefixed, "placement:")
            if ref not in self._placements or ref in self._locked:
                continue
            if ref in self._failed:
                continue
            return ref
        return None

    def _first_feasible(self, finding: Violation, offender: str) -> Point | None:
        """The nearest candidate that clears the finding within the
        mover's displacement bound. A candidate that strictly decreases
        the board's error count wins outright — equal-count trades are
        how a mover bounces between two crowded neighbours, so they are
        the fallback only, first-cleared nearest-first, and the attempt
        cap ends any oscillation they cannot resolve. Verified by a
        probe, so no candidate is trusted on its closed-form derivation
        alone."""
        baseline = sum(
            1
            for finding_ in self._evaluator.violations()
            if finding_.severity is Severity.ERROR
        )
        fallback = None
        for pos in self._candidates(finding, offender):
            if self._bound is not None and (
                _displacement(self._originals[offender], pos) > self._bound
            ):
                continue
            probe = self._evaluator.probe(offender, pos)
            if not _is_cleared(finding, probe.violations):
                continue
            errors = sum(
                1
                for violation in probe.violations
                if violation.severity is Severity.ERROR
            )
            if errors < baseline:
                return pos
            if fallback is None:
                fallback = pos
        return fallback

    def _restore(self, ref: str) -> None:
        """A failed part ends exactly where it was (ADR-0012's
        all-or-nothing per part)."""
        original = self._originals[ref]
        if self._positions[ref] != original:
            self._evaluator.commit(ref, original)
            self._positions[ref] = original

    def _candidates(self, finding: Violation, ref: str) -> tuple[Point, ...]:
        """Violation-driven candidate positions, nearest-first — closed
        forms per finding type, each aiming for ``_MARGIN_NM`` of
        clearance where a face must clear another."""
        mover = self._at(ref)
        match finding.type:
            case ViolationType.COURTYARDS_OVERLAP:
                (other_ref,) = tuple(
                    other for other in _placement_refs(finding) if other != ref
                )
                return _sorted_candidates(
                    self._positions[ref],
                    _push_out_shifts(
                        _measure_box(mover),
                        _measure_box(self._at(other_ref)),
                    ),
                )
            case ViolationType.COURTYARD_OUTSIDE_OUTLINE:
                return self._containment_candidates(ref, mover)
            case ViolationType.PROXIMITY_UNMET:
                return self._proximity_candidates(finding, ref, mover)
            case ViolationType.REGION_UNMET:
                region = self._relation(finding).rect
                return _sorted_candidates(
                    self._positions[ref],
                    _clamp_shifts(_measure_box(mover), region),
                )
            case ViolationType.EDGE_MOUNT_UNMET:
                return self._edge_mount_candidates(finding, ref, mover)
            case ViolationType.KEEP_TOGETHER_UNMET:
                return self._keep_together_candidates(finding, ref, mover)
        return ()

    def _at(self, ref: str):
        """The placement at its current position (the input Board's
        placements never change; the position map is the working state)."""
        return dataclasses.replace(self._placements[ref], pos=self._positions[ref])

    def _relation(self, finding: Violation):
        """The constraint relation a constraint finding reports —
        ``with_constraint`` validated it against this Board, so the
        lookup cannot miss."""
        constraint_id = _after_prefix(finding.offending_refs[0], "constraint:")
        return self._constraints[constraint_id].relation

    def _containment_candidates(self, ref: str, mover):
        """Shift the mover just inside the outline, moving only the edges
        it crosses unstated — a Requirement EdgeMount's stated edge is
        exempt from containment (ADR-0018) and the repair must not break
        the mount it silenced."""
        yard = world_courtyard(mover)
        if yard is None:
            return ()
        unstated = _crossed_edges(yard, self._board.outline) - self._exempt.get(
            ref, frozenset()
        )
        box = _measure_box(mover)
        outline = self._board.outline
        dx = 0
        if Edge.LEFT in unstated and Edge.RIGHT in unstated:
            return ()  # wider than the board: no translation fits
        if Edge.LEFT in unstated:
            dx = outline.min.x - box.min.x
        elif Edge.RIGHT in unstated:
            dx = outline.max.x - box.max.x
        dy = 0
        if Edge.TOP in unstated and Edge.BOTTOM in unstated:
            return ()
        if Edge.TOP in unstated:
            dy = outline.min.y - box.min.y
        elif Edge.BOTTOM in unstated:
            dy = outline.max.y - box.max.y
        return (Point(self._positions[ref].x + dx, self._positions[ref].y + dy),)

    def _proximity_candidates(self, finding: Violation, ref: str, mover):
        """Slide the mover along the pads' connecting line until the pair
        sits just inside the bound. A cross-side pair under
        ``same_side`` has no translation repair — the empty candidate
        set fails the part by name."""
        relation = self._relation(finding)
        if relation.ref_a == ref:
            pad_mine, pad_other, other_ref = (
                relation.pad_a,
                relation.pad_b,
                relation.ref_b,
            )
        else:
            pad_mine, pad_other, other_ref = (
                relation.pad_b,
                relation.pad_a,
                relation.ref_a,
            )
        other = self._placements.get(other_ref)
        if other is None:
            return ()  # pending, never violated — unreachable for a finding
        if relation.same_side and other.side != mover.side:
            return ()  # a cross-side pair has no translation repair
        mine = pad_world_center(mover, pad_mine)
        theirs = pad_world_center(self._at(other_ref), pad_other)
        dx, dy = mine.x - theirs.x, mine.y - theirs.y
        distance = isqrt(dx * dx + dy * dy)
        if distance == 0:
            return ()
        target = relation.bound_nm - min(_MARGIN_NM, relation.bound_nm)
        slide = Point(
            theirs.x + dx * target // distance,
            theirs.y + dy * target // distance,
        )
        return (
            Point(
                self._positions[ref].x + slide.x - mine.x,
                self._positions[ref].y + slide.y - mine.y,
            ),
        )

    def _edge_mount_candidates(self, finding: Violation, ref: str, mover):
        """Put the stated face inside the permitted band: flush on the
        edge line when short of it, at the band's far limit when past it
        (the band ``[edge, edge + overhang]``, ``None`` overhang a band
        of exactly ``{0}`` — ADR-0011)."""
        relation = self._relation(finding)
        box = _measure_box(mover)
        if box is None:
            return ()
        overhang = relation.max_overhang_nm or 0
        outline = self._board.outline
        pos = self._positions[ref]
        if relation.edge in (Edge.LEFT, Edge.TOP):
            # a minimum-face edge: the offset measures how far inside the
            # face sits, and moving the part by the offset slides the
            # face onto the edge line
            if relation.edge is Edge.LEFT:
                offset, face_pos = outline.min.x - box.min.x, pos.x
            else:
                offset, face_pos = outline.min.y - box.min.y, pos.y
            if 0 <= offset <= overhang:
                return ()
            shift = offset if offset < 0 else offset - overhang
        else:
            # a maximum-face edge: past-the-edge offsets read positive,
            # and the shift back into the band is the offset's mirror
            if relation.edge is Edge.RIGHT:
                offset, face_pos = box.max.x - outline.max.x, pos.x
            else:
                offset, face_pos = box.max.y - outline.max.y, pos.y
            if 0 <= offset <= overhang:
                return ()
            shift = -offset if offset < 0 else overhang - offset
        if relation.edge in (Edge.LEFT, Edge.RIGHT):
            return (Point(face_pos + shift, pos.y),)
        return (Point(pos.x, face_pos + shift),)

    def _keep_together_candidates(self, finding: Violation, ref: str, mover):
        """Pull the mover into the other members' union box, when that
        union already fits the extent — a floating bound the mover alone
        cannot fix otherwise."""
        relation = self._relation(finding)
        others = []
        for member in _anchor_members(self._board, relation.target):
            if member == ref:
                continue
            if member not in self._placements:
                return ()  # pending, never violated — unreachable here
            box = _measure_box(self._at(member))
            if box is None:
                return ()
            others.append(box)
        if not others:
            return ()  # a lone member's extent travels with it: unfixable
        union = union_bbox(others)
        if (
            union.max.x - union.min.x > relation.max_width_nm
            or union.max.y - union.min.y > relation.max_height_nm
        ):
            return ()
        return _sorted_candidates(
            self._positions[ref], _clamp_shifts(_measure_box(mover), union)
        )


def _is_cleared(finding: Violation, verdict: tuple[Violation, ...]) -> bool:
    """Whether the probe's verdict no longer carries the finding —
    matched by type and canonical refs (the locations move with the
    parts, the identity does not)."""
    return not any(
        violation.type is finding.type
        and violation.offending_refs == finding.offending_refs
        for violation in verdict
    )


def _push_out_shifts(
    mover_box: Rectangle | None, other_box: Rectangle | None
) -> tuple[tuple[int, int], ...]:
    """Axis-aligned shifts carrying the mover's box just clear of the
    other's, ``_MARGIN_NM`` past the face — the minimal-penetration family."""
    if mover_box is None or other_box is None:
        return ()
    return (
        ((other_box.min.x - _MARGIN_NM) - mover_box.max.x, 0),
        ((other_box.max.x + _MARGIN_NM) - mover_box.min.x, 0),
        (0, (other_box.min.y - _MARGIN_NM) - mover_box.max.y),
        (0, (other_box.max.y + _MARGIN_NM) - mover_box.min.y),
    )


def _clamp_shifts(
    box: Rectangle | None, container: Rectangle
) -> tuple[tuple[int, int], ...]:
    """Shifts carrying a box inside a rectangle: align to the poked-past
    face per axis (either face when both poke — the box straddles), zero
    where the axis is already inside. An axis the box is too wide for
    has no shift."""
    if box is None:
        return ()

    def axis(box_lo: int, box_hi: int, c_lo: int, c_hi: int) -> tuple[int, ...]:
        if box_lo < c_lo and box_hi > c_hi:
            return ()
        shifts = []
        if box_lo < c_lo:
            shifts.append(c_lo - box_lo)
        if box_hi > c_hi:
            shifts.append(c_hi - box_hi)
        return tuple(shifts) or (0,)

    return tuple(
        (dx, dy)
        for dx in axis(box.min.x, box.max.x, container.min.x, container.max.x)
        for dy in axis(box.min.y, box.max.y, container.min.y, container.max.y)
    )


def _sorted_candidates(
    current: Point, shifts: tuple[tuple[int, int], ...]
) -> tuple[Point, ...]:
    """Deduplicated candidate positions, nearest-first — the greedy
    nearest-feasible order, ties broken lexicographically."""
    candidates = {
        Point(current.x + dx, current.y + dy): (dx * dx + dy * dy, dx, dy)
        for dx, dy in shifts
    }
    return tuple(
        point for point, _ in sorted(candidates.items(), key=lambda item: item[1])
    )
