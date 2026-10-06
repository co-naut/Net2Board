"""DRC — the agent's feedback channel (ADR-0009).

``run_drc(board)`` returns a deterministic, canonically-sorted tuple of
``Violation`` structs — same board, same tuple, immune to check order, scan
order, and upsert semantics. Checks are pure functions registered in the
ordered module-level ``CHECKS`` tuple: adding a check later is appending one
function. M1 shipped exactly one check, courtyard overlap, whose type value
deliberately collides with the KiCad oracle's ``courtyards_overlap``. M2
adds outline containment (ADR-0018), which has no oracle analogue and so
carries a type of its own, and the four constraint checks (#42, ADR-0011):
an unmet **Requirement** Constraint is an ordinary Violation —
``PROXIMITY_UNMET``, ``REGION_UNMET``, ``EDGE_MOUNT_UNMET``,
``KEEP_TOGETHER_UNMET`` — while Preferences never produce Violations (they
belong to the objective, ADR-0014). Every measure these checks read is a
public geometry function, the single definition the objective's Preference
hinges reuse (ADR-0014).

A check may declare a **locality companion** in ``COMPANIONS`` (ADR-0016):
a callable returning exactly the findings whose ``offending_refs`` contain
one moved ref. A check with no companion forces a full recompute of itself
— slower, never wrong. The containment check is the protocol's first
validator, its companion O(1) in the moved placement's geometry; the
constraint checks re-derive only the constraints anchored to the moved ref.

Constraint findings are the first **variable-arity** users of the struct
(ADR-0009's documented-convention door): ``offending_refs`` runs one longer
than ``locations``, the leading ``constraint:<id>`` location-less, the
trailing placement refs pairing with locations positionally. An anchor that
is unplaced makes its constraint *pending*, not violated (ADR-0011) — an
all-unplaced Board stays clean.

Oracle parity notes (probed on kicad-cli 10.0.6): overlap is
strict-interior — touching edges and tangent points are not violations —
cross-side placement pairs are exempt, and courtyard-less placements are
skipped, never flagged. The oracle has no containment check to pair with
(probed, ADR-0018): that check is Net2Board's alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from net2board.geometry import (
    Point,
    Rectangle,
    Shape,
    edge_face_offset_nm,
    pad_distance_nm,
    pad_world_bbox,
    pad_world_center,
    region_shortfall_nm,
    shape_aabb,
    shape_contained_in,
    shapes_overlap,
    union_bbox,
    world_courtyard,
)
from net2board.model import (
    Board,
    Component,
    Constraint,
    Edge,
    EdgeMount,
    KeepTogether,
    Placement,
    Proximity,
    Region,
    Requirement,
    UnknownConstraintRef,
)
from net2board.model.constraints import anchor_parts

__all__ = [
    "CHECKS",
    "COMPANIONS",
    "Companion",
    "Severity",
    "Violation",
    "ViolationType",
    "run_drc",
]


class Severity(Enum):
    """ERROR | WARNING only — suppression is check configuration, not a
    severity (ADR-0009)."""

    ERROR = "error"
    WARNING = "warning"


class ViolationType(Enum):
    """snake_case values that collide with the oracle's type strings —
    naming convention, not a dependency (ADR-0009). Checks with no oracle
    analogue (containment, ADR-0018; the constraint checks, #42) carry a
    snake_case name of their own."""

    COURTYARDS_OVERLAP = "courtyards_overlap"
    COURTYARD_OUTSIDE_OUTLINE = "courtyard_outside_outline"
    PROXIMITY_UNMET = "proximity_unmet"
    REGION_UNMET = "region_unmet"
    EDGE_MOUNT_UNMET = "edge_mount_unmet"
    KEEP_TOGETHER_UNMET = "keep_together_unmet"


@dataclass(frozen=True)
class Violation:
    """One DRC finding, fully actionable without holding the Board.

    Where refs and locations arities match, pairing is positional:
    ``offending_refs[i] ↔ locations[i]``. Constraint findings (#42) are the
    one variable-arity form: ``offending_refs`` runs one longer than
    ``locations``, the leading ``constraint:<id>`` location-less — the
    constraint is the finding's subject, not an offender at a place — and
    the trailing placement refs pair with locations positionally.
    Round-trips through ``dataclasses.asdict`` for a future MCP layer with
    zero adapter code.
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
    board: Board,
) -> str:
    """The pinned wording per type — a pure function of the finding and the
    Board it was found on (ADR-0009); future checks pin theirs here when
    they land. Constraint findings name the relation's offenders and their
    constraint; the pad-bounding-box fallback (ADR-0011, extended to
    EdgeMount by #42) is derived from the named placements' footprints."""
    match violation_type:
        case ViolationType.COURTYARDS_OVERLAP:
            return f"Courtyards overlap: {refs[0]} and {refs[1]}"
        case ViolationType.COURTYARD_OUTSIDE_OUTLINE:
            return f"Courtyard outside board outline: {refs[0]}"
        case ViolationType.PROXIMITY_UNMET:
            return f"Proximity unmet: {refs[1]} and {refs[2]} ({refs[0]})"
        case ViolationType.REGION_UNMET:
            return _constraint_wording("Region", refs, board)
        case ViolationType.EDGE_MOUNT_UNMET:
            return _constraint_wording("EdgeMount", refs, board)
        case ViolationType.KEEP_TOGETHER_UNMET:
            return _constraint_wording("KeepTogether", refs, board)
    raise ValueError(f"no pinned description for violation type {violation_type!r}")


_PAD_BBOX_NOTE = " — measured by pad bounding box"


def _constraint_wording(relation_name: str, refs: tuple[str, ...], board: Board) -> str:
    """The pinned constraint wording: ``<Relation> unmet: <placements>
    (<constraint>)``, with the fallback note when a named placement has no
    courtyard to measure (ADR-0011's "noted in the violation description").
    """
    named = refs[1:]
    if len(named) == 1:
        joined = named[0]
    else:
        joined = ", ".join(named[:-1]) + f" and {named[-1]}"
    wording = f"{relation_name} unmet: {joined} ({refs[0]})"
    placements = _placement_index(board)
    components: dict[str, Component] | None = None
    for ref in named:
        name = ref.removeprefix("placement:")
        placement = placements.get(name)
        if placement is not None:
            footprint = placement.footprint
        else:
            # A solve probe names a ref whose placement is not committed
            # yet; the netlist component carries the same footprint
            # ``with_placement`` embeds, so the answer is identical.
            if components is None:
                components = {comp.ref: comp for comp in board.components}
            footprint = components[name].footprint
        if footprint.courtyard is None:
            wording += _PAD_BBOX_NOTE
            break
    return wording


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
                violations.append(_overlap_violation(a, b, board))
    return tuple(violations)


def _overlap_violation(a: Placement, b: Placement, board: Board) -> Violation:
    refs, locations = _canonical_pairs(
        (f"placement:{a.ref}", f"placement:{b.ref}"), (a.pos, b.pos)
    )
    return Violation(
        type=ViolationType.COURTYARDS_OVERLAP,
        severity=Severity.ERROR,
        description=_describe(ViolationType.COURTYARDS_OVERLAP, refs, locations, board),
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


def _edge_mount_exemptions(board: Board) -> dict[str, frozenset[Edge]]:
    """Per ref, the outline edges a Requirement EdgeMount excuses (ADR-0018).

    Requirement-kind only — a Preference EdgeMount exempts nothing — and
    one edge per stated mount, stacking across ids for corners. Anchors
    expand at check time (ADR-0011): a group-targeted mount exempts each
    member's stated edge. ``with_constraint`` validated every anchor
    against this Board, so the group lookup cannot miss.
    """
    groups = {group.id: group for group in board.groups}
    exempt: dict[str, set[Edge]] = {}
    for constraint in board.constraints:
        if not isinstance(constraint.kind, Requirement):
            continue
        relation = constraint.relation
        if not isinstance(relation, EdgeMount):
            continue
        kind, name = anchor_parts(relation.target)
        refs = groups[name].refs if kind == "group" else (name,)
        for ref in refs:
            exempt.setdefault(ref, set()).add(relation.edge)
    return {ref: frozenset(edges) for ref, edges in exempt.items()}


def _crossed_edges(shape: Shape, outline: Rectangle) -> frozenset[Edge]:
    """Which outline edge lines the shape extends strictly past (ADR-0018).

    The per-edge complement of closed containment: tangent is not
    crossing. Exact-integer throughout — the circle compares squared
    distances against ``radius_sq`` with a sign check, no ``isqrt``
    (ADR-0005, ADR-0006). Lives here, not in ``geometry``: its vocabulary
    is the model's ``Edge``, which geometry never imports.
    """
    if isinstance(shape, Rectangle):
        return frozenset(
            edge
            for edge, over in (
                (Edge.LEFT, outline.min.x - shape.min.x),
                (Edge.RIGHT, shape.max.x - outline.max.x),
                (Edge.TOP, outline.min.y - shape.min.y),
                (Edge.BOTTOM, shape.max.y - outline.max.y),
            )
            if over > 0
        )

    def crosses(distance: int) -> bool:
        return distance < 0 or distance * distance < shape.radius_sq

    return frozenset(
        edge
        for edge, distance in (
            (Edge.LEFT, shape.center.x - outline.min.x),
            (Edge.RIGHT, outline.max.x - shape.center.x),
            (Edge.TOP, shape.center.y - outline.min.y),
            (Edge.BOTTOM, outline.max.y - shape.center.y),
        )
        if crosses(distance)
    )


def _overhang_finding(
    placement: Placement,
    outline: Rectangle,
    exempt: dict[str, frozenset[Edge]],
    board: Board,
) -> Violation | None:
    """The placement's containment finding, or ``None`` when clean.

    Closed containment first; only a non-contained courtyard pays the
    crossed-edge test that scopes the per-edge exemption. One finding per
    placement regardless of how many unstated edges it crosses
    (ADR-0018's single-reporting rule).
    """
    yard = world_courtyard(placement)
    if yard is None or shape_contained_in(yard, outline):
        return None
    unstated = _crossed_edges(yard, outline) - exempt.get(placement.ref, frozenset())
    if not unstated:
        return None
    ref = f"placement:{placement.ref}"
    return Violation(
        type=ViolationType.COURTYARD_OUTSIDE_OUTLINE,
        severity=Severity.ERROR,
        description=_describe(
            ViolationType.COURTYARD_OUTSIDE_OUTLINE, (ref,), (placement.pos,), board
        ),
        offending_refs=(ref,),
        locations=(placement.pos,),
    )


def _courtyard_outside_outline(board: Board) -> tuple[Violation, ...]:
    """A courtyard lying outside the board outline, at ERROR (ADR-0018).

    Closed containment — tangent to an edge is inside. Courtyard-less
    placements are skipped, matching the overlap check (ADR-0006). The
    exemption is a Requirement EdgeMount's stated edge, per edge; the
    rectangle-only scope and its graduation trigger live in ADR-0018.
    """
    exempt = _edge_mount_exemptions(board)
    violations: list[Violation] = []
    for placement in board.placements:
        finding = _overhang_finding(placement, board.outline, exempt, board)
        if finding is not None:
            violations.append(finding)
    return tuple(violations)


def _courtyard_outside_outline_companion(
    board: Board, ref: str
) -> tuple[Violation, ...]:
    """Locality companion of the containment check (ADR-0016, ADR-0018).

    O(1) in the moved placement's geometry: re-test its courtyard against
    the outline — findings on other refs are untouched by construction.
    The ref→placement lookup and the stated-intent scan are flat state on
    the Evaluator's terms when this protocol is consumed (ADR-0016); on
    Boards both are plain scans.
    """
    exempt = _edge_mount_exemptions(board)
    return tuple(
        finding
        for placement in board.placements
        if placement.ref == ref
        if (finding := _overhang_finding(placement, board.outline, exempt, board))
        is not None
    )


def _requirement_relations(
    board: Board,
    relation_type: type[Proximity | Region | EdgeMount | KeepTogether],
):
    """The Board's Requirement constraints over one relation type, in
    stated order. Preferences never reach a check (ADR-0011: they belong
    to the objective); Criticality has no check by construction."""
    for constraint in board.constraints:
        if isinstance(constraint.kind, Requirement) and isinstance(
            constraint.relation, relation_type
        ):
            yield constraint


def _placement_index(board: Board) -> dict[str, Placement]:
    """The ref → placement map every check and companion reads once per
    call — full recompute on Boards; flat state on the Evaluator's terms
    when this protocol is consumed (ADR-0016)."""
    return {placement.ref: placement for placement in board.placements}


def _anchor_members(board: Board, target: str) -> tuple[str, ...]:
    """A relation's target expanded to placement refs (ADR-0011).

    ``placement:<ref>`` is that ref alone; ``group:<id>`` is the Group's
    refs, stored canonically sorted. ``with_constraint`` validated every
    anchor against this Board, and an inconsistent construction raises
    ``UnknownConstraintRef`` here rather than surfacing as a bare miss
    downstream.
    """
    kind, name = anchor_parts(target)
    if kind == "placement":
        return (name,)
    for group in board.groups:
        if group.id == name:
            return group.refs
    raise UnknownConstraintRef(target)


def _constraint_violation(
    violation_type: ViolationType,
    constraint_id: str,
    offenders: tuple[tuple[str, Point], ...],
    board: Board,
) -> Violation:
    """One constraint finding in canonical form (the #42 convention): the
    ``constraint:<id>`` ref leads location-less, the offending placement
    refs follow in lexicographic order, each paired with its location."""
    refs = (f"constraint:{constraint_id}",) + tuple(
        f"placement:{ref}" for ref, _ in offenders
    )
    locations = tuple(location for _, location in offenders)
    return Violation(
        type=violation_type,
        severity=Severity.ERROR,
        description=_describe(violation_type, refs, locations, board),
        offending_refs=refs,
        locations=locations,
    )


def _proximity_unmet(board: Board) -> tuple[Violation, ...]:
    """Pad pairs farther apart than the stated bound (or across sides
    under ``same_side``), at ERROR (#42, ADR-0011).

    The distance is ``pad_distance_nm`` — the isqrt integer-nm measure the
    objective's Proximity hinge reuses verbatim (ADR-0014); the bound is
    inclusive. An unplaced endpoint is pending, not violated.
    """
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, Proximity):
        finding = _proximity_finding(constraint, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _proximity_finding(
    constraint: Constraint, placements: dict[str, Placement], board: Board
) -> Violation | None:
    """One Proximity constraint's finding, or ``None`` when satisfied or
    pending. The shared body of the check and its locality companion."""
    relation = constraint.relation
    a = placements.get(relation.ref_a)
    b = placements.get(relation.ref_b)
    if a is None or b is None:
        return None
    cross_side = relation.same_side and a.side != b.side
    unmet = cross_side or (
        pad_distance_nm(a, relation.pad_a, b, relation.pad_b) > relation.bound_nm
    )
    if not unmet:
        return None
    offenders = sorted(
        (
            (relation.ref_a, pad_world_center(a, relation.pad_a)),
            (relation.ref_b, pad_world_center(b, relation.pad_b)),
        ),
        key=lambda pair: pair[0],
    )
    return _constraint_violation(
        ViolationType.PROXIMITY_UNMET, constraint.id, tuple(offenders), board
    )


def _measure_box(placement: Placement) -> Rectangle | None:
    """The box Region, EdgeMount, and KeepTogether measure: the courtyard's
    AABB, or the pad bounding box when there is no courtyard (ADR-0011's
    fallback, extended to EdgeMount by #42). A footprint with neither has
    nothing to measure: ``None`` — the member is skipped, matching the
    courtyard-less posture of the overlap and containment checks."""
    yard = world_courtyard(placement)
    if yard is not None:
        return shape_aabb(yard)
    try:
        return pad_world_bbox(placement)
    except ValueError:
        return None


def _region_unmet(board: Board) -> tuple[Violation, ...]:
    """Courtyards outside the stated rectangle, at ERROR (#42, ADR-0011).

    Inclusive: the region gathers, it does not fence (ADR-0011) — only
    named members are tested. Containment is closed and measured on
    ``region_shortfall_nm`` over the member's measure box: the zero test
    and the Preference hinge read one number (ADR-0014). A group target
    offends per member — one finding per offending placement, so the
    Locality axiom survives a gathered target (#42's convention).
    """
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, Region):
        for ref in _anchor_members(board, constraint.relation.target):
            finding = _region_finding(constraint, ref, placements, board)
            if finding is not None:
                findings.append(finding)
    return tuple(findings)


def _region_finding(
    constraint: Constraint, ref: str, placements: dict[str, Placement], board: Board
) -> Violation | None:
    """One Region member's finding, or ``None`` when satisfied or pending.
    The shared body of the check and its locality companion."""
    placement = placements.get(ref)
    if placement is None:
        return None
    box = _measure_box(placement)
    if box is None or region_shortfall_nm(box, constraint.relation.rect) == 0:
        return None
    return _constraint_violation(
        ViolationType.REGION_UNMET,
        constraint.id,
        ((ref, placement.pos),),
        board,
    )


def _region_unmet_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the Region check (#42, ADR-0016): re-derive
    only the Region constraints anchoring the moved ref — a member's
    finding turns on its own measure box alone."""
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, Region):
        if ref not in _anchor_members(board, constraint.relation.target):
            continue
        finding = _region_finding(constraint, ref, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _edge_mount_unmet(board: Board) -> tuple[Violation, ...]:
    """Courtyard faces not on their stated edge within the permitted
    overhang, at ERROR (#42, ADR-0011, CONTEXT.md's EdgeMount).

    The band is ``[edge line, edge line + max_overhang_nm]`` — a face short
    of the edge is as unmet as one past the permitted overhang, and
    ``None`` means flush. The measure is ``edge_face_offset_nm`` over the
    member's measure box; the same number is the Preference hinge's input
    (ADR-0014). The stated edge still silences containment regardless of
    the bound (ADR-0018's single-reporting rule): over-bound overhang is
    this check's alone to report. A group target offends per member.
    """
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, EdgeMount):
        relation = constraint.relation
        for ref in _anchor_members(board, relation.target):
            placement = placements.get(ref)
            if placement is None:
                continue
            finding = _edge_mount_finding(constraint, relation, placement, board)
            if finding is not None:
                findings.append(finding)
    return tuple(findings)


def _edge_mount_finding(
    constraint: Constraint, relation: EdgeMount, placement: Placement, board: Board
) -> Violation | None:
    """One EdgeMount member's finding, or ``None`` when satisfied or
    pending. The shared body of the check and its locality companion."""
    box = _measure_box(placement)
    if box is None:
        return None
    offset = edge_face_offset_nm(box, board.outline, relation.edge.value)
    if 0 <= offset <= (relation.max_overhang_nm or 0):
        return None
    return _constraint_violation(
        ViolationType.EDGE_MOUNT_UNMET,
        constraint.id,
        ((placement.ref, placement.pos),),
        board,
    )


def _keep_together_unmet(board: Board) -> tuple[Violation, ...]:
    """Group extents wider or taller than the stated bound, at ERROR
    (#42, ADR-0011).

    The union bounding box of the members' measure boxes must fit
    ``max_width_nm`` × ``max_height_nm`` — a floating bound with no stated
    location. The extent is collective: every member is named on the one
    finding (so any member's move is attributable, the Locality axiom's
    terms), and any member unplaced makes the whole constraint pending —
    an extent cannot be measured short a member.
    """
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, KeepTogether):
        finding = _keep_together_finding(constraint, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _keep_together_finding(
    constraint: Constraint, placements: dict[str, Placement], board: Board
) -> Violation | None:
    """One KeepTogether constraint's finding, or ``None`` when satisfied
    or pending. The shared body of the check and its locality companion."""
    relation = constraint.relation
    members: list[tuple[str, Placement]] = []
    for ref in _anchor_members(board, relation.target):
        placement = placements.get(ref)
        if placement is None:
            return None
        members.append((ref, placement))
    boxes = []
    for _, placement in members:
        box = _measure_box(placement)
        if box is None:
            # a placed member with nothing to measure voids the extent the
            # same way an unplaced one does: silent, never a guess (the
            # courtyard-less skip posture of the overlap/containment checks)
            return None
        boxes.append(box)
    union = union_bbox(tuple(boxes))
    if (
        union.max.x - union.min.x <= relation.max_width_nm
        and union.max.y - union.min.y <= relation.max_height_nm
    ):
        return None
    offenders = tuple((ref, placement.pos) for ref, placement in members)
    return _constraint_violation(
        ViolationType.KEEP_TOGETHER_UNMET, constraint.id, offenders, board
    )


def _proximity_unmet_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the Proximity check (#42, ADR-0016): re-derive
    only the Proximity constraints naming the moved ref — either endpoint's
    move can flip the pair."""
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, Proximity):
        relation = constraint.relation
        if ref not in (relation.ref_a, relation.ref_b):
            continue
        finding = _proximity_finding(constraint, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _edge_mount_unmet_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the EdgeMount check (#42, ADR-0016): re-derive
    only the EdgeMount constraints anchoring the moved ref — a member's
    finding turns on its own face offset alone."""
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, EdgeMount):
        relation = constraint.relation
        if ref not in _anchor_members(board, relation.target):
            continue
        placement = placements.get(ref)
        if placement is None:
            continue
        finding = _edge_mount_finding(constraint, relation, placement, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _keep_together_unmet_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the KeepTogether check (#42, ADR-0016): any
    member's move can change the union extent, so a ref in the group
    recomputes the constraint's whole finding — which names every member,
    keeping the axiom's attribution exact."""
    placements = _placement_index(board)
    findings: list[Violation] = []
    for constraint in _requirement_relations(board, KeepTogether):
        if ref not in _anchor_members(board, constraint.relation.target):
            continue
        finding = _keep_together_finding(constraint, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


CHECKS: tuple[Callable[[Board], tuple[Violation, ...]], ...] = (
    _courtyard_overlap,
    _courtyard_outside_outline,
    _proximity_unmet,
    _region_unmet,
    _edge_mount_unmet,
    _keep_together_unmet,
)

Companion = Callable[[Board, str], tuple[Violation, ...]]
"""What a locality companion is (ADR-0016): ``companion(board, ref)`` returns
exactly the findings ``check(board)`` reports whose ``offending_refs``
contain ``placement:<ref>`` — CONTEXT.md's Locality axiom. ``ref`` is the
bare placement ref, the Evaluator's slot key."""

COMPANIONS: dict[Callable[[Board], tuple[Violation, ...]], Companion] = {
    _courtyard_outside_outline: _courtyard_outside_outline_companion,
    _proximity_unmet: _proximity_unmet_companion,
    _region_unmet: _region_unmet_companion,
    _edge_mount_unmet: _edge_mount_unmet_companion,
    _keep_together_unmet: _keep_together_unmet_companion,
}
"""The locality-companion registry, beside ``CHECKS``: a check declaring
itself here is incrementally re-testable per moved ref; one absent from
here forces a full recompute of itself — slower, never wrong (ADR-0016)."""


def run_drc(board: Board) -> tuple[Violation, ...]:
    """Run every registered check in order, canonicalize, sort explicitly.

    Canonicalization re-pairs refs with their locations in lexicographic
    ref order and rebuilds the description from the canonical form; the
    final sort key is ``(type.value, offending_refs, locations)`` — the
    same board always yields the same tuple (ADR-0009). Constraint
    findings canonicalize under the #42 convention: the leading
    ``constraint:<id>`` stays put, its location-less slot skipped.
    """
    violations = (violation for check in CHECKS for violation in check(board))
    return tuple(sorted((_canonical(v, board) for v in violations), key=_sort_key))


def _canonical(violation: Violation, board: Board) -> Violation:
    refs = violation.offending_refs
    locations = violation.locations
    if len(refs) == len(locations):
        canonical_refs, canonical_locations = _canonical_pairs(refs, locations)
    elif len(refs) == len(locations) + 1 and refs[0].startswith("constraint:"):
        tail_refs, tail_locations = _canonical_pairs(refs[1:], locations)
        canonical_refs = (refs[0],) + tail_refs
        canonical_locations = tail_locations
    else:
        raise ValueError(
            f"variable-arity finding from check emitting {violation.type.value!r}: "
            f"{len(refs)} refs vs {len(locations)} locations — the one "
            f"documented convention (ADR-0009) is a leading location-less "
            f"'constraint:<id>' ref (#42); anything else has none"
        )
    return replace(
        violation,
        offending_refs=canonical_refs,
        locations=canonical_locations,
        description=_describe(
            violation.type, canonical_refs, canonical_locations, board
        ),
    )


def _sort_key(violation: Violation):
    return (
        violation.type.value,
        violation.offending_refs,
        tuple((point.x, point.y) for point in violation.locations),
    )
