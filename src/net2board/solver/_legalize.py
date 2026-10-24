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
  bound, integer nm) is restored to exactly where it was; it is named in
  ``unlegalized_refs`` while a finding naming it survives to the final
  board (the report reads the board, #96), and the returned Board is
  honestly still illegal.
- **Mobility** — any unlocked placed part may move, already-legal ones
  included; locked parts never move and a locked part that is itself
  illegal parks at once (named while its finding survives, #96);
  unplaced refs stay unplaced; rotation and side are untouched
  (translation only).
- **Intent is a wall, never a trade** — Requirements must be met, never
  relaxed; Preferences do not enter legalization at all.
- **Failure is a value** — exceptions are reserved for invalid input (a
  malformed bound raises ``LegalizationError`` under ``Net2BoardError``).

The repair runs on the warm per-call Evaluator (ADR-0016), unbudgeted;
``solve`` (ADR-0013) ends in this derivation as its final phase, for the
same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isqrt

from net2board.drc import (
    Severity,
    Violation,
    ViolationType,
    _anchored_copper,
    _capsule_box,
    _crossed_edges,
    _edge_mount_exemptions,
    _shifted,
)
from net2board.errors import Net2BoardError
from net2board.geometry import (
    Point,
    Rectangle,
    measure_box,
    pad_world_center,
    pad_world_outline,
    union_bbox,
    world_courtyard,
)
from net2board.model import Board, Edge
from net2board.solver._evaluator import Evaluator, Pose

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
    Board, the canonically sorted Offenders (GLOSSARY.md) among the final Board's
    residual error findings (a locked part that is itself illegal, a part
    that could not be repaired within the bound — named while a finding
    naming it survives, never the repairer's failure history, #96), and
    each moved ref's Euclidean displacement in integer nm — movers only.
    No aggregates, no success boolean: ``unlegalized_refs == ()`` is the
    success test, and ``run_drc`` remains the authority on what, if
    anything, is still wrong."""

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
    ends exactly where it was. The report names the Offenders of
    the final Board's residual findings, so ``unlegalized_refs == ()``
    holds exactly when ``run_drc`` reports no error. Locked placements
    never move; unplaced refs stay unplaced; rotation and side are
    untouched.
    """
    bound = _checked_bound(max_displacement)
    evaluator = Evaluator(board)
    if evaluator.current_key()[0] == 0:
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


def _displacement(a: Point | Pose, b: Point | Pose) -> int:
    """Euclidean distance in integer nm (ADR-0005, the ``isqrt`` stance).
    Reads any x/y pair — a position or a Pose (the rotation never enters
    a distance)."""
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
    """One legalize call's working state: the warm Evaluator — whose
    committed placements and positions are the only live state (#67) —
    this repairer's own originals snapshot, and the greediest repair loop
    in the ADR's family: first finding, first movable offender, nearest
    verified candidate, repeat."""

    def __init__(self, evaluator: Evaluator, board: Board, bound: int | None):
        self._evaluator = evaluator
        self._board = board
        self._bound = bound
        # the Evaluator owns the live placements and positions (#67): the
        # repairer keeps only its own snapshot of where the input board
        # stood — Poses, the one operation's argument (ADR-0025) — and the
        # locked set (locked is carried, never moved)
        self._originals = {
            placement.ref: Pose.of(placement) for placement in board.placements
        }
        self._locked = {
            placement.ref for placement in board.placements if placement.locked
        }
        self._exempt = _edge_mount_exemptions(board)
        self._constraints = {
            constraint.id: constraint for constraint in board.constraints
        }
        self._failed: set[str] = set()
        # mover-selection state only, never the report (#96): a parked
        # ref is not retried, and the result reads the final board
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
                break  # every finding parked: nothing more to try
            offender = self._choose_offender(finding)
            if offender is None:
                # a finding whose offenders are all locked or already
                # failed can never be repaired — park it and let the
                # other findings continue; its Offenders are named
                # by the report iff the finding survives to the final
                # board (#96's report semantics)
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
                if not self._candidates(finding, offender):
                    # No candidate this finding's closed form can offer —
                    # an intra-footprint pad pair (placement-invariant,
                    # ADR-0012 as amended by #83), a cross-side proximity,
                    # a type with no vocabulary at all. Park it without
                    # restoring: the part keeps its successful repairs
                    # and the residual sweep names it — restoring here
                    # would let an unrepairable finding undo the repairs
                    # of the other findings on the same part.
                    self._stuck.add(_Target(finding.type.value, finding.offending_refs))
                continue  # another round may do better; the cap ends it
            self._evaluator.commit(offender, accepted)
        # rounds exhausted: the backstop ends the loop; the report reads
        # the final board (``result``), never the failure history

    def result(self, board: Board) -> LegalizationResult:
        """Materialise the outcome: the movers' Board (through the public
        construction path), the canonically sorted residual placed
        parties (see below), and the movers' displacements. The movers
        read off the Evaluator's live state against this repairer's own
        snapshot (#67) — the snapshot and the live read are Poses
        (ADR-0025), so the mover test is exact. The repairer never
        places an unplaced ref, so its originals are the placed
        universe.

        The report is the final board's, not the repairer's history
        (#96): the named refs are the Offenders of the residual
        error findings, so ``unlegalized_refs == ()`` is exactly the
        success test — ``run_drc`` finds no error. ``_failed`` stays the
        loop's internal mover-selection state; a part whose findings
        other moves cleared is no longer named."""
        movers = {
            ref: pose
            for ref, original in self._originals.items()
            if (pose := self._evaluator.pose(ref)) != original
        }
        if movers:
            for ref in sorted(movers):
                pose = movers[ref]
                placement = self._evaluator.placement(ref)
                board = board.with_placement(
                    ref,
                    pose.x,
                    pose.y,
                    pose.rotation,
                    placement.side,
                    placement.locked,
                )
        displacements = {
            ref: _displacement(self._originals[ref], movers[ref])
            for ref in sorted(movers)
        }
        residual = {
            ref
            for finding in self._evaluator.violations()
            if finding.severity is Severity.ERROR
            for ref in _placement_refs(finding)
            if self._evaluator.placement(ref) is not None
        }
        return LegalizationResult(
            board=board,
            unlegalized_refs=tuple(sorted(residual)),
            displacements=displacements,
        )

    def _choose_offender(self, finding: Violation) -> str | None:
        """The finding's first movable offender in canonical ref order:
        placed, unlocked, not already failed."""
        for prefixed in finding.offending_refs:
            if not prefixed.startswith("placement:"):
                continue
            ref = _after_prefix(prefixed, "placement:")
            if self._evaluator.placement(ref) is None or ref in self._locked:
                continue
            if ref in self._failed:
                continue
            return ref
        return None

    def _first_feasible(self, finding: Violation, offender: str) -> Pose | None:
        """The nearest candidate that clears the finding within the
        mover's displacement bound. A candidate that strictly decreases
        the board's error count wins outright — equal-count trades are
        how a mover bounces between two crowded neighbours, so they are
        the fallback only, first-cleared nearest-first, and the attempt
        cap ends any oscillation they cannot resolve. Verified by a
        probe, so no candidate is trusted on its closed-form derivation
        alone. The repair vocabulary is translation-only — candidates are
        positions, wrapped at the probe in the mover's standing rotation
        (ADR-0025: legalize never rotates)."""
        baseline = self._evaluator.current_key()[0]
        fallback: Pose | None = None
        rotation = self._at(offender).rotation
        for pos in self._candidates(finding, offender):
            if self._bound is not None and (
                _displacement(self._originals[offender], pos) > self._bound
            ):
                continue
            pose = Pose(pos.x, pos.y, rotation)
            probe = self._evaluator.probe(offender, pose)
            if not _is_cleared(finding, probe.violations):
                continue
            errors = sum(
                1
                for violation in probe.violations
                if violation.severity is Severity.ERROR
            )
            if errors < baseline:
                return pose
            if fallback is None:
                fallback = pose
        return fallback

    def _restore(self, ref: str) -> None:
        """A failed part ends exactly where it was (ADR-0012's
        all-or-nothing per part)."""
        original = self._originals[ref]
        if self._evaluator.pose(ref) != original:
            self._evaluator.commit(ref, original)

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
                    self._evaluator.position(ref),
                    _push_out_shifts(
                        measure_box(mover),
                        measure_box(self._at(other_ref)),
                    ),
                )
            case ViolationType.SHORTING_ITEMS | ViolationType.CLEARANCE:
                return self._pad_pair_candidates(finding, ref)
            case ViolationType.COPPER_EDGE_CLEARANCE:
                return self._copper_edge_candidates(ref)
            case ViolationType.COURTYARD_OUTSIDE_OUTLINE:
                return self._containment_candidates(ref, mover)
            case ViolationType.PROXIMITY_UNMET:
                return self._proximity_candidates(finding, ref, mover)
            case ViolationType.REGION_UNMET:
                region = self._relation(finding).rect
                return _sorted_candidates(
                    self._evaluator.position(ref),
                    _clamp_shifts(measure_box(mover), region),
                )
            case ViolationType.EDGE_MOUNT_UNMET:
                return self._edge_mount_candidates(finding, ref, mover)
            case ViolationType.KEEP_TOGETHER_UNMET:
                return self._keep_together_candidates(finding, ref, mover)
        return ()

    def _at(self, ref: str):
        """The placement as it stands — the Evaluator's live read (#67):
        commit already carries the full candidate, so there is nothing to
        rebuild here."""
        return self._evaluator.placement(ref)

    def _relation(self, finding: Violation):
        """The constraint relation a constraint finding reports —
        ``with_constraint`` validated it against this Board, so the
        lookup cannot miss."""
        constraint_id = _after_prefix(finding.offending_refs[0], "constraint:")
        return self._constraints[constraint_id].relation

    def _pad_pair_candidates(self, finding: Violation, ref: str) -> tuple[Point, ...]:
        """Push the mover along one axis until *every* violating pad pair
        between the two refs sits ``clearance_nm + _MARGIN_NM`` apart —
        the largest shift per direction across the shared builder's
        records (#95), so one move ends the whole pair family between the
        refs and a short aims past full clearance: it cannot be "cleared"
        into an equal-count ``clearance`` finding and bounce (#83). The
        pairs and pads come from the checker — ``Evaluator.pad_pairs``
        and the pad indices the records carry — never a second predicate.
        An intra-footprint pair (the ref repeated) has no translation
        repair — placement-invariant, named at entry, the part stays
        movable (ADR-0012 as amended by #83): the empty candidate set."""
        others = tuple(other for other in _placement_refs(finding) if other != ref)
        if not others:
            return ()
        (other,) = others
        mover = self._at(ref)
        partner = self._at(other)
        gap = self._board.design_rules.clearance_nm + _MARGIN_NM
        shifts = [
            _push_out_shifts(
                _capsule_box(pad_world_outline(mover, mover.footprint.pads[index_a])),
                _capsule_box(
                    pad_world_outline(partner, partner.footprint.pads[index_b])
                ),
                gap,
            )
            for index_a, index_b in self._evaluator.pad_pairs(ref, other)
        ]
        if not shifts:
            return ()  # unreachable for a live finding — the parks above hold
        return _sorted_candidates(
            self._evaluator.position(ref),
            (
                (min(s[0][0] for s in shifts), 0),  # the largest −x push …
                (max(s[1][0] for s in shifts), 0),  # … +x push, and the y pair
                (0, min(s[2][1] for s in shifts)),
                (0, max(s[3][1] for s in shifts)),
            ),
        )

    def _copper_edge_candidates(self, ref: str) -> tuple[Point, ...]:
        """Clamp the mover's *whole* copper box — every copper pad's grown
        capsule, not just the offending one, so fixing one pad cannot push
        another over (#83) — inside the outline shrunk by
        ``edge_clearance_nm + _MARGIN_NM`` per unstated edge: ADR-0022's
        containment reading of the edge finding. A
        Requirement EdgeMount's stated edge imposes nothing (the
        ``_edge_mount_exemptions`` map, shared with the courtyard
        containment: the repair must not break the mount it silenced); a
        Preference exempts nothing, so a preference-mounted part is
        pulled inward. An axis the box is too wide for has no shift — the
        part fails by name."""
        anchored = _anchored_copper(self._at(ref), self._board.stackup)
        if anchored.box is None:
            return ()
        pos = self._evaluator.position(ref)
        box = _shifted(anchored.box, pos)
        gap = self._board.design_rules.edge_clearance_nm + _MARGIN_NM
        outline = self._board.outline
        exempt = self._exempt.get(ref, frozenset())
        container = Rectangle(
            min=Point(
                box.min.x if Edge.LEFT in exempt else outline.min.x + gap,
                box.min.y if Edge.TOP in exempt else outline.min.y + gap,
            ),
            max=Point(
                box.max.x if Edge.RIGHT in exempt else outline.max.x - gap,
                box.max.y if Edge.BOTTOM in exempt else outline.max.y - gap,
            ),
        )
        return _sorted_candidates(pos, _clamp_shifts(box, container))

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
        box = measure_box(mover)
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
        pos = self._evaluator.position(ref)
        return (Point(pos.x + dx, pos.y + dy),)

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
        other = self._evaluator.placement(other_ref)
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
                self._evaluator.position(ref).x + slide.x - mine.x,
                self._evaluator.position(ref).y + slide.y - mine.y,
            ),
        )

    def _edge_mount_candidates(self, finding: Violation, ref: str, mover):
        """Put the stated face inside the permitted band: flush on the
        edge line when short of it, at the band's far limit when past it
        (the band ``[edge, edge + overhang]``, ``None`` overhang a band
        of exactly ``{0}`` — ADR-0011)."""
        relation = self._relation(finding)
        box = measure_box(mover)
        if box is None:
            return ()
        overhang = relation.max_overhang_nm or 0
        outline = self._board.outline
        pos = self._evaluator.position(ref)
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
        for member in self._board.anchor_members(relation.target):
            if member == ref:
                continue
            if self._evaluator.placement(member) is None:
                return ()  # pending, never violated — unreachable here
            box = measure_box(self._at(member))
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
            self._evaluator.position(ref), _clamp_shifts(measure_box(mover), union)
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
    mover_box: Rectangle | None,
    other_box: Rectangle | None,
    gap: int = _MARGIN_NM,
) -> tuple[tuple[int, int], ...]:
    """Axis-aligned shifts carrying the mover's box just clear of the
    other's, ``gap`` past the face — ``_MARGIN_NM`` for the courtyard
    family, ``clearance_nm + _MARGIN_NM`` per pad pair (#95). Four
    shifts, fixed order: −x, +x, −y, +y — the per-direction aggregate
    reads them positionally (the −x and −y entries aggregate with
    ``min``, the +x and +y with ``max``)."""
    if mover_box is None or other_box is None:
        return ()
    return (
        ((other_box.min.x - gap) - mover_box.max.x, 0),
        ((other_box.max.x + gap) - mover_box.min.x, 0),
        (0, (other_box.min.y - gap) - mover_box.max.y),
        (0, (other_box.max.y + gap) - mover_box.min.y),
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
