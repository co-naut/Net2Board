"""The Objective — exact integer nanometres, one continuous term, hinged
Preferences (ADR-0014).

``evaluate(board)`` is the pure, full-recompute reference implementation the
incremental Evaluator (#44, ADR-0016) must match bit for bit: a Board in, an
``Objective`` out — ``total_nm`` plus per-term ``ObjectiveTerm`` rows. It
reads intent off the Board (ADR-0011): no objective argument, no hidden
engine weight — every tunable is a Preference weight already on the
snapshot. The Objective ranks; it never fails a Board and never declares
correctness: ``run_drc`` (oracle cross-checked) owns *correct*, this owns
*well-ranked* (Freerouting ordering-correlation, slow tier).

Terms come from the ordered ``TERMS`` registry — a later term (congestion,
say) is one append:

- **NET_SPAN** — the numeraire (CONTEXT.md's NetSpan): width + height of
  the AABB of a net's *placed pad centers*, integer nm, pad centers via
  ``pad_world_center`` so rotation and the back-side mirror compose exactly
  as everywhere else. One term per net, always: degree ≤ 1 measures 0 but
  still reports, so totals stay structurally comparable. A net with
  unplaced pins measures over its placed pins and reports PARTIAL. The
  weight is the net's Criticality (default 1); when two Criticality
  constraints name one net, the last stated wins — the upsert-by-id mental
  model, deterministic in stated order.
- **PREFERENCE** — the hinges: one term per Preference constraint that is
  not a Criticality (Criticality scales a span, it does not hinge). The
  shortfall reuses the constraint checks' measures verbatim —
  ``pad_distance_nm``, ``region_shortfall_nm``, ``edge_face_offset_nm`` —
  the single definition the checks and this module share (#42): a board
  that fires the matching Requirement check measures a positive hinge, and
  a satisfied Preference measures exactly zero, growing linearly past the
  bound.

Statuses: ACTIVE (measured, in force), PARTIAL (measured over a strict
subset of the anchors — NetSpan's unplaced-pin rule, extended to
group-anchored hinges, an anchor with nothing to measure counting as
unmeasured), PENDING (nothing could be measured; raw and weighted 0 — an
unplaced Proximity endpoint, or a Region/EdgeMount/KeepTogether anchor set
with no placed, measurable member). Satisfied is ACTIVE with
``weighted_nm == 0`` — zero never has to mean "not applicable".

Edge calls ADR-0014 leaves open, pinned here:

- The Proximity hinge is side-insensitive: ``max(0, d − bound)`` exactly as
  pinned, no ``same_side`` clause — a cross-side pair under a ``same_side``
  Preference measures its distance like any other. The side predicate is
  the Requirement check's alone.
- The EdgeMount hinge is the distance from the face offset to the permitted
  band ``[0, max_overhang]``: short of the edge and past the bound are both
  debt; ``None`` overhang is a band of exactly ``{0}``.
- KeepTogether's extent is collective (the #42 posture): any member
  unplaced or unmeasurable voids the measurement — PENDING, never a
  partial guess.

Terms are canonically sorted by ``(kind.value, ref)`` — the ADR-0009 habit:
same board, same tuple, immune to netlist and statement order. The word
*cost* appears nowhere in this API (ADR-0014).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from net2board.geometry import (
    Rectangle,
    edge_face_offset_nm,
    pad_distance_nm,
    pad_world_bbox,
    pad_world_center,
    region_shortfall_nm,
    shape_aabb,
    union_bbox,
    world_courtyard,
)
from net2board.model import (
    Board,
    Criticality,
    EdgeMount,
    KeepTogether,
    Placement,
    Preference,
    Proximity,
    Region,
    UnknownConstraintRef,
)
from net2board.model.constraints import anchor_parts

__all__ = [
    "TERMS",
    "Objective",
    "ObjectiveTerm",
    "TermKind",
    "TermStatus",
    "evaluate",
]


class TermKind(Enum):
    """Which addend family a term belongs to — the ``TERMS`` registry's
    members. A later term (congestion, ADR-0014's named graduate) appends
    itself here and to ``TERMS``."""

    NET_SPAN = "net_span"
    PREFERENCE = "preference"


class TermStatus(Enum):
    """ACTIVE (measured, in force), PARTIAL (measured over a strict subset
    of the anchors), PENDING (nothing could be measured)."""

    ACTIVE = "active"
    PARTIAL = "partial"
    PENDING = "pending"


TERMS: tuple[TermKind, ...] = (TermKind.NET_SPAN, TermKind.PREFERENCE)
"""The ordered term registry (ADR-0014): every kind ``evaluate`` emits is
one of these, in this order — canonical sorting puts earlier kinds first."""


@dataclass(frozen=True)
class ObjectiveTerm:
    """One addend of the Objective (ADR-0014): a named subject in ref form
    (``net:<name>`` or ``constraint:<id>``), an integer weight, raw and
    weighted nanometres, and a status. Satisfied is ACTIVE with
    ``weighted_nm == 0`` — not a status of its own."""

    kind: TermKind
    ref: str
    weight: int
    raw_nm: int
    weighted_nm: int
    status: TermStatus


@dataclass(frozen=True)
class Objective:
    """The ranking of one Board: exact integer nanometres, itemized.

    Totals are comparable only between reports with the same term refs,
    weights, and statuses — placing a part can look worse than leaving it
    unplaced, never better (ADR-0014's loud consequence)."""

    total_nm: int
    terms: tuple[ObjectiveTerm, ...]


def evaluate(board: Board) -> Objective:
    """Rank a Board — the full-recompute reference implementation.

    Every net contributes a NET_SPAN term; every non-Criticality Preference
    contributes a PREFERENCE hinge. Terms are canonically sorted by
    ``(kind.value, ref)`` and ``total_nm`` is the sum of the weighted
    nanometres — pure, deterministic, integer-only.
    """
    placements = {placement.ref: placement for placement in board.placements}
    terms = tuple(
        sorted(
            _net_span_terms(board, placements) + _hinge_terms(board, placements),
            key=lambda term: (term.kind.value, term.ref),
        )
    )
    return Objective(total_nm=sum(term.weighted_nm for term in terms), terms=terms)


def _criticality_weights(board: Board) -> dict[str, int]:
    """Per net, the Criticality weight scaling its span (default 1).

    Two constraints naming one net resolve last-stated-wins — the
    upsert-by-id mental model, deterministic in stated order.
    """
    weights: dict[str, int] = {}
    for constraint in board.constraints:
        if isinstance(constraint.relation, Criticality):
            weights[constraint.relation.net] = constraint.weight
    return weights


def _net_span_terms(
    board: Board, placements: dict[str, Placement]
) -> list[ObjectiveTerm]:
    """One NET_SPAN term per net, in netlist order (canonicalized later).

    Placed pins measure through ``pad_world_center``; an unplaced pin makes
    the net PARTIAL over its placed subset. Degree ≤ 1 measures 0 but still
    reports — 4 of 13 ecc83 nets are single-pin (ADR-0014).
    """
    weights = _criticality_weights(board)
    terms = []
    for net in board.nets:
        centers = []
        unplaced = False
        for ref, pad_number in net.pins:
            placement = placements.get(ref)
            if placement is None:
                unplaced = True
                continue
            centers.append(pad_world_center(placement, pad_number))
        if len(centers) <= 1:
            span = 0
        else:
            xs = [center.x for center in centers]
            ys = [center.y for center in centers]
            span = (max(xs) - min(xs)) + (max(ys) - min(ys))
        weight = weights.get(net.name, 1)
        terms.append(
            ObjectiveTerm(
                kind=TermKind.NET_SPAN,
                ref=f"net:{net.name}",
                weight=weight,
                raw_nm=span,
                weighted_nm=weight * span,
                status=TermStatus.PARTIAL if unplaced else TermStatus.ACTIVE,
            )
        )
    return terms


def _hinge_terms(board: Board, placements: dict[str, Placement]) -> list[ObjectiveTerm]:
    """One PREFERENCE term per non-Criticality Preference constraint, in
    stated order (canonicalized later). Requirements never reach the
    objective (they are run_drc's alone); Criticality scales, never hinges.
    """
    terms = []
    for constraint in board.constraints:
        if not isinstance(constraint.kind, Preference):
            continue
        if isinstance(constraint.relation, Criticality):
            continue
        raw_nm, status = _hinge(constraint.relation, placements, board)
        terms.append(
            ObjectiveTerm(
                kind=TermKind.PREFERENCE,
                ref=f"constraint:{constraint.id}",
                weight=constraint.weight,
                raw_nm=raw_nm,
                weighted_nm=constraint.weight * raw_nm,
                status=status,
            )
        )
    return terms


def _hinge(
    relation: Proximity | Region | EdgeMount | KeepTogether,
    placements: dict[str, Placement],
    board: Board,
) -> tuple[int, TermStatus]:
    """One hinge's ``(raw_nm, status)`` — dispatched per relation, each
    reading the same geometry measure its Requirement check reads."""
    match relation:
        case Proximity():
            return _proximity_hinge(relation, placements)
        case Region():
            return _per_member_hinge(
                board,
                relation.target,
                placements,
                lambda box: region_shortfall_nm(box, relation.rect),
            )
        case EdgeMount():
            permitted = (
                0 if relation.max_overhang_nm is None else relation.max_overhang_nm
            )
            return _per_member_hinge(
                board,
                relation.target,
                placements,
                lambda box: _band_shortfall(
                    edge_face_offset_nm(box, board.outline, relation.edge.value),
                    permitted,
                ),
            )
        case KeepTogether():
            return _keep_together_hinge(relation, placements, board)
    raise ValueError(f"unknown relation {relation!r}")


def _proximity_hinge(
    relation: Proximity, placements: dict[str, Placement]
) -> tuple[int, TermStatus]:
    """``max(0, pad_distance_nm − bound)`` — side-insensitive (pinned in
    the module docstring). An unplaced endpoint is PENDING, never a guess.
    """
    a = placements.get(relation.ref_a)
    b = placements.get(relation.ref_b)
    if a is None or b is None:
        return 0, TermStatus.PENDING
    distance = pad_distance_nm(a, relation.pad_a, b, relation.pad_b)
    return max(0, distance - relation.bound_nm), TermStatus.ACTIVE


def _band_shortfall(offset: int, permitted: int) -> int:
    """Distance from a face offset to the permitted band ``[0, permitted]``:
    short of the edge and past the bound are both debt."""
    return max(0, -offset) + max(0, offset - permitted)


def _per_member_hinge(
    board: Board,
    target: str,
    placements: dict[str, Placement],
    member_shortfall: Callable[[Rectangle], int],
) -> tuple[int, TermStatus]:
    """Sum the per-member shortfall over placed, measurable members —
    Region's and EdgeMount's shared body (both offend per member, #42).

    PENDING when no member measured at all (none placed, or placed members
    all unmeasurable); PARTIAL when measured over a strict subset — an
    anchor unplaced *or* an anchor with nothing to measure; ACTIVE
    otherwise. A courtyard-less member falls back to its pad bounding box
    (``_measure_box``) and a member with neither is skipped — the checks'
    courtyard-less posture, with PARTIAL flagging the skipped subset.
    """
    boxes, any_unmeasured = _measurable_members(board, target, placements)
    if not boxes:
        return 0, TermStatus.PENDING
    raw = sum(member_shortfall(box) for box in boxes)
    if any_unmeasured:
        return raw, TermStatus.PARTIAL
    return raw, TermStatus.ACTIVE


def _keep_together_hinge(
    relation: KeepTogether,
    placements: dict[str, Placement],
    board: Board,
) -> tuple[int, TermStatus]:
    """``max(0, w − max_w) + max(0, h − max_h)`` over the union bounding
    box of the members' measure boxes. The extent is collective (#42):
    any member unplaced or unmeasurable voids the measurement — PENDING,
    never a partial guess.
    """
    boxes: list[Rectangle] = []
    for ref in _anchor_members(board, relation.target):
        placement = placements.get(ref)
        if placement is None:
            return 0, TermStatus.PENDING
        box = _measure_box(placement)
        if box is None:
            return 0, TermStatus.PENDING
        boxes.append(box)
    union = union_bbox(boxes)
    shortfall = max(0, (union.max.x - union.min.x) - relation.max_width_nm) + max(
        0, (union.max.y - union.min.y) - relation.max_height_nm
    )
    return shortfall, TermStatus.ACTIVE


def _measurable_members(
    board: Board, target: str, placements: dict[str, Placement]
) -> tuple[list[Rectangle], bool]:
    """A target's placed, measurable member boxes, plus whether any anchor
    went unmeasured — unplaced, or placed with nothing to measure (the
    PARTIAL signal)."""
    boxes = []
    any_unmeasured = False
    for ref in _anchor_members(board, target):
        placement = placements.get(ref)
        if placement is None:
            any_unmeasured = True
            continue
        box = _measure_box(placement)
        if box is None:
            any_unmeasured = True
        else:
            boxes.append(box)
    return boxes, any_unmeasured


def _measure_box(placement: Placement) -> Rectangle | None:
    """The box Region, EdgeMount, and KeepTogether measure: the courtyard's
    AABB, or the pad bounding box when there is no courtyard (ADR-0011's
    fallback, #42). A footprint with neither has nothing to measure:
    ``None``. A deliberate private mirror of the drc module's helper —
    one definition in geometry, the same call shape both sides.
    """
    yard = world_courtyard(placement)
    if yard is not None:
        return shape_aabb(yard)
    try:
        return pad_world_bbox(placement)
    except ValueError:
        return None


def _anchor_members(board: Board, target: str) -> tuple[str, ...]:
    """A relation's target expanded to placement refs (ADR-0011):
    ``placement:<ref>`` is that ref alone, ``group:<id>`` the Group's
    canonically sorted refs. A deliberate private mirror of the drc
    module's helper — ``with_constraint`` validated every anchor, and an
    inconsistent construction raises ``UnknownConstraintRef`` here
    rather than surfacing as a bare miss downstream.
    """
    kind, name = anchor_parts(target)
    if kind == "placement":
        return (name,)
    for group in board.groups:
        if group.id == name:
            return group.refs
    raise UnknownConstraintRef(target)
