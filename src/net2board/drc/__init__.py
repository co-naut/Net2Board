"""DRC — the agent's feedback channel (ADR-0009).

``run_drc(board)`` returns a deterministic, canonically-sorted tuple of
``Violation`` structs — same board, same tuple, immune to check order,
iteration order, and upsert semantics. Checks are pure functions registered in the
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

Oracle parity notes (probed under the exporter's ``ORACLE_TARGET``):
overlap is strict-interior — touching edges and tangent points are not
violations — cross-side placement pairs are exempt, and courtyard-less
placements are skipped, never flagged. The oracle has no containment
check to pair with (probed, ADR-0018): that check is Net2Board's alone.

The pad-copper checks (#91, ADR-0022) price every copper-bearing pad's
``pad_world_outline`` capsule against ``board.design_rules``: different-net
capsules that intersect are ``shorting_items``, a gap strictly below
``clearance_nm`` (touching included, actual 0) is ``clearance``, pad
copper within ``edge_clearance_nm`` of an outline edge line is
``copper_edge_clearance`` — all three at ERROR, type strings colliding
with the oracle's so the slow tier compares per type field-for-field.
Exemptions, in three rules: same-net pairs (a pad with no net counts as
its own net); pads sharing a ``net_tie_pad_groups`` group; a Requirement
EdgeMount's stated edge (ADR-0018's per-edge mechanism extended to pad
copper). Intra-footprint pairs are checked — placement-invariant
findings with the ref repeated. One finding per pad pair (or pad),
locations at the pad world centres; the shared builders expose private
pad-pair records — indices, kind, gap — the repair vocabulary reads
(#95) so repair and oracle price one geometry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from net2board.geometry import (
    Capsule,
    Point,
    Rectangle,
    Shape,
    _boxes_clear,
    _clear_of_outline,
    capsule_gap_nm,
    capsules_overlap,
    edge_face_offset_nm,
    measure_box,
    pad_distance_nm,
    pad_world_center,
    pad_world_outline,
    region_shortfall_nm,
    shape_contained_in,
    shapes_overlap,
    union_bbox,
    world_courtyard,
    world_point,
)
from net2board.model import (
    Board,
    Constraint,
    Edge,
    EdgeMount,
    Footprint,
    KeepTogether,
    Pad,
    Placement,
    Proximity,
    Region,
    Requirement,
)
from net2board.model.constraints import anchor_parts, constraint_ref

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
    SHORTING_ITEMS = "shorting_items"
    CLEARANCE = "clearance"
    COPPER_EDGE_CLEARANCE = "copper_edge_clearance"


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
        case ViolationType.SHORTING_ITEMS:
            first, second = _pad_pair_wording(refs, locations, board)
            return f"Shorting items: {first} and {second}"
        case ViolationType.CLEARANCE:
            first, second = _pad_pair_wording(refs, locations, board)
            gap = _pair_gap(refs, locations, board)
            return (
                f"Clearance breached: {first} and {second} "
                f"(gap {gap} nm, rule {board.design_rules.clearance_nm} nm)"
            )
        case ViolationType.COPPER_EDGE_CLEARANCE:
            pad_word, gap = _pad_edge_wording(refs, locations, board)
            return (
                f"Copper edge clearance: {pad_word} "
                f"(gap {gap} nm, rule {board.design_rules.edge_clearance_nm} nm)"
            )
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
    for ref in named:
        name = ref.removeprefix("placement:")
        placement = board.placement(name)
        if placement is not None:
            footprint = placement.footprint
        else:
            # A solve probe names a ref whose placement is not committed
            # yet; the netlist component carries the same footprint
            # ``with_placement`` embeds, so the answer is identical.
            footprint = board.component(name).footprint
        if footprint.courtyard is None:
            wording += _PAD_BBOX_NOTE
            break
    return wording


def _placement_of(ref: str, board: Board) -> Placement:
    """The placement behind a ``placement:`` ref — pad findings name only
    placed placements, so the lookup cannot miss. O(1) on both surfaces
    (#111): ``Board.placement`` is an index read (ADR-0001 as amended by
    #29) and the Evaluator's ``_ProbeBoard`` answers the same call from
    its ``_MoveView`` — wording no longer builds an all-placements map
    per finding."""
    placement = board.placement(ref.removeprefix("placement:"))
    if placement is None:
        raise ValueError(f"unknown placement ref {ref!r} in a pad finding")
    return placement


_PAD_AT_CACHE: dict = {}


def _pad_at(placement: Placement, location: Point) -> Pad:
    """The pad whose world centre is this location — pad identity is
    recoverable from the placement plus the location (ADR-0022's
    no-pad-ref-kind rule); the first match wins, the recorded convention."""
    footprint = placement.footprint
    key = (id(footprint), placement.rotation, placement.side)
    entry = _PAD_AT_CACHE.get(key)
    if entry is None or entry[0] is not footprint:
        pose = _AnchorPose(Point(0, 0), placement.rotation, placement.side)
        table = {}
        for pad in footprint.pads:
            table.setdefault(world_point(pad.local_pos, pose), pad)
        entry = (footprint, table)
        _PAD_AT_CACHE[key] = entry
    pad = entry[1].get(
        Point(location.x - placement.pos.x, location.y - placement.pos.y)
    )
    if pad is not None:
        return pad
    raise ValueError(
        f"no pad of {placement.ref!r} sits at {location.x, location.y} — "
        f"the finding's location is not a pad world centre"
    )


def _pad_pair_wording(
    refs: tuple[str, ...], locations: tuple[Point, ...], board: Board
) -> tuple[str, str]:
    """``placement:A pad 1``-form names for the pair, in canonical ref
    order — the pads resolved from the locations."""
    first, second = (_placement_of(ref, board) for ref in refs)
    return (
        f"{refs[0]} pad {_pad_at(first, locations[0]).number}",
        f"{refs[1]} pad {_pad_at(second, locations[1]).number}",
    )


def _pair_gap(refs: tuple[str, ...], locations: tuple[Point, ...], board: Board) -> int:
    """The pair's measured gap, recomputed from the two pads' capsules —
    the same number the pass priced, one definition (ADR-0009's wording
    is a pure function of the finding and the Board)."""
    first, second = (_placement_of(ref, board) for ref in refs)
    gap = capsule_gap_nm(
        pad_world_outline(first, _pad_at(first, locations[0])),
        pad_world_outline(second, _pad_at(second, locations[1])),
    )
    return gap


def _pad_edge_wording(
    refs: tuple[str, ...], locations: tuple[Point, ...], board: Board
) -> tuple[str, int]:
    """The edge finding's ``placement:A pad 1`` name and reported gap —
    recomputed through the check's own body (``_pad_edge_gap``), so the
    wording cannot drift from the verdict."""
    placement = _placement_of(refs[0], board)
    pad = _pad_at(placement, locations[0])
    exempt = _edge_mount_exemptions(board)
    gap = _pad_edge_gap(placement, pad, exempt, board)
    return f"{refs[0]} pad {pad.number}", gap


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


def _edge_mount_exemptions(board: Board) -> Mapping[str, frozenset[Edge]]:
    """Per ref, the outline edges a Requirement EdgeMount excuses (ADR-0018).

    Requirement-kind only — a Preference EdgeMount exempts nothing — and
    one edge per stated mount, stacking across ids for corners. Anchors
    expand at check time (ADR-0011): a group-targeted mount exempts each
    member's stated edge. ``with_constraint`` validated every anchor
    against this Board, so the group lookup cannot miss.

    Read once per constraint set (#98): the map depends only on the
    Board's immutable ``constraints`` and ``groups`` tuples, which every
    ``with_placement`` descendant shares, so the last reading is reused
    while both are the same objects.
    """
    global _last_exemptions
    last = _last_exemptions
    if last is not None and last[0] is board.constraints and last[1] is board.groups:
        return last[2]
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
    exemptions = {ref: frozenset(edges) for ref, edges in exempt.items()}
    _last_exemptions = (board.constraints, board.groups, exemptions)
    return exemptions


_last_exemptions: (
    tuple[tuple[Constraint, ...], tuple, dict[str, frozenset[Edge]]] | None
) = None


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
    The ref→placement lookup and the stated-intent pass are flat state on
    the Evaluator's terms when this protocol is consumed (ADR-0016); on
    Boards both are plain iterations.
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
    """The ref → placement map a full-recompute check builds once per
    call over all placements (ADR-0016). The probe path never enters
    here for a single ref: the wording builders resolve through
    ``board.placement`` in O(1) (#111) — building this map per finding
    was the O(n) term #111 removed."""
    return {placement.ref: placement for placement in board.placements}


def _constraint_violation(
    violation_type: ViolationType,
    constraint_id: str,
    offenders: tuple[tuple[str, Point], ...],
    board: Board,
) -> Violation:
    """One constraint finding in canonical form (the #42 convention): the
    ``constraint:<id>`` ref leads location-less, the offending placement
    refs follow in lexicographic order, each paired with its location."""
    refs = (constraint_ref(constraint_id),) + tuple(
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
    constraint: Constraint, placements: Mapping[str, Placement], board: Board
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
        for ref in board.anchor_members(constraint.relation.target):
            finding = _region_finding(constraint, ref, placements, board)
            if finding is not None:
                findings.append(finding)
    return tuple(findings)


def _region_finding(
    constraint: Constraint, ref: str, placements: Mapping[str, Placement], board: Board
) -> Violation | None:
    """One Region member's finding, or ``None`` when satisfied or pending.
    The shared body of the check and its locality companion."""
    placement = placements.get(ref)
    if placement is None:
        return None
    box = measure_box(placement)
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
        if ref not in board.anchor_members(constraint.relation.target):
            continue
        finding = _region_finding(constraint, ref, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _edge_mount_unmet(board: Board) -> tuple[Violation, ...]:
    """Courtyard faces not on their stated edge within the permitted
    overhang, at ERROR (#42, ADR-0011, GLOSSARY.md's EdgeMount).

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
        for ref in board.anchor_members(relation.target):
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
    box = measure_box(placement)
    if box is None:
        return None
    offset = edge_face_offset_nm(box, board.outline, relation.edge)
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
    constraint: Constraint, placements: Mapping[str, Placement], board: Board
) -> Violation | None:
    """One KeepTogether constraint's finding, or ``None`` when satisfied
    or pending. The shared body of the check and its locality companion."""
    relation = constraint.relation
    members: list[tuple[str, Placement]] = []
    for ref in board.anchor_members(relation.target):
        placement = placements.get(ref)
        if placement is None:
            return None
        members.append((ref, placement))
    boxes = []
    for _, placement in members:
        box = measure_box(placement)
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
        if ref not in board.anchor_members(relation.target):
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
        if ref not in board.anchor_members(constraint.relation.target):
            continue
        finding = _keep_together_finding(constraint, placements, board)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


# --- pad copper (#91, ADR-0022) ---------------------------------------------
#
# The three clearance checks share one geometry pass: every copper-bearing
# pad's capsule, priced pairwise against ``board.design_rules`` and against
# the outline edge lines. The records the pass builds are the private
# pad-pair vocabulary the repairer reads (#95's obligation): indices, kind,
# gap — so repair and oracle price one geometry.


@dataclass(frozen=True)
class _PadPair:
    """One violating pad pair: the shared builder's record (#95)."""

    refs: tuple[str, str]
    pad_indices: tuple[int, int]
    gap_nm: int
    violation: Violation

    @property
    def kind(self) -> str:
        """The pair's verdict — derived from the violation's own type,
        never a second field to keep in step with it."""
        if self.violation.type is ViolationType.SHORTING_ITEMS:
            return "short"
        return "clearance"


@dataclass(frozen=True)
class _PadEdge:
    """One violating pad-to-edge finding: the shared builder's record."""

    ref: str
    pad_index: int
    gap_nm: int
    violation: Violation


def _pad_copper_layers(tokens: tuple[str, ...], side: str, stackup) -> frozenset[str]:
    """The stackup copper layers a pad's copper lands on (ADR-0022).

    ``F.Cu``/``B.Cu`` swap on back-side placements (KiCad's flip — the
    exporter's ``_pad_layer`` rule); ``*.Cu`` spans every stackup copper
    layer; a layer named directly in the stackup (``In1.Cu``) is itself.
    Anything else — mask, paste, silk — is not copper: a pad with no
    copper layers is skipped by the checks entirely.
    """
    coppers = frozenset(stackup)
    layers: set[str] = set()
    for token in tokens:
        if token in ("F.Cu", "B.Cu"):
            if side == "B.Cu":
                token = "B.Cu" if token == "F.Cu" else "F.Cu"
            layers.add(token)
        elif token == "*.Cu":
            layers |= coppers
        elif token in coppers:
            layers.add(token)
    return frozenset(layers)


def _net_map(board: Board) -> Mapping[tuple[str, str], str]:
    """The ``(ref, pad number) → net name`` map; a pad absent from it has
    no net and counts as its own (the same map the exporter prices).
    Reused while the Board's immutable ``nets`` tuple is the same object
    (#98) — every ``with_placement`` descendant shares it."""
    global _last_net_map
    last = _last_net_map
    if last is not None and last[0] is board.nets:
        return last[1]
    net_by_pin: dict[tuple[str, str], str] = {}
    for net in board.nets:
        for ref, number in net.pins:
            net_by_pin.setdefault((ref, number), net.name)
    _last_net_map = (board.nets, net_by_pin)
    return net_by_pin


_last_net_map: tuple[tuple, dict[tuple[str, str], str]] | None = None


@dataclass(frozen=True)
class _PadCopper:
    """One copper-bearing pad, world-resolved and ready to price."""

    index: int
    number: str
    pad: Pad
    capsule: Capsule
    center: Point
    layers: frozenset[str]
    net: str | None


@dataclass(frozen=True)
class _AnchorPose:
    """A placement's turn and side at the world origin — the duck-typed
    pose the geometry transforms read (``.pos``, ``.rotation``, ``.side``)."""

    pos: Point
    rotation: int
    side: str


@dataclass(frozen=True)
class _AnchoredCopper:
    """A footprint's copper-bearing pads resolved at the origin for one
    turn and side — every capsule and centre a pure translation away from
    any anchor (90° multiples keep the transform exact-integer), plus the
    union box of the grown capsules, the pair pass's pre-filter bound."""

    footprint: Footprint
    pads: tuple[tuple[int, Pad, Capsule, Point, frozenset[str]], ...]
    box: Rectangle | None


_ANCHORED_COPPER: dict[tuple[int, int, str, tuple[str, ...]], _AnchoredCopper] = {}
_ANCHORED_COPPER_LIMIT = 4096


def _anchored_copper(placement: Placement, stackup) -> _AnchoredCopper:
    """The placement's copper at the origin, cached per (footprint, turn,
    side, stackup) — unmoved and same-footprint parts share one resolution.
    The entry holds its footprint, so the identity key cannot be reused."""
    footprint = placement.footprint
    key = (id(footprint), placement.rotation, placement.side, tuple(stackup))
    cached = _ANCHORED_COPPER.get(key)
    if cached is not None and cached.footprint is footprint:
        return cached
    pose = _AnchorPose(Point(0, 0), placement.rotation, placement.side)
    pads = []
    for index, pad in enumerate(footprint.pads):
        layers = _pad_copper_layers(pad.layers, placement.side, stackup)
        if not layers:
            continue
        pads.append(
            (
                index,
                pad,
                pad_world_outline(pose, pad),
                world_point(pad.local_pos, pose),
                layers,
            )
        )
    box = None
    if pads:
        boxes = [_capsule_box(capsule) for _, _, capsule, _, _ in pads]
        box = Rectangle(
            min=Point(
                min(box.min.x for box in boxes),
                min(box.min.y for box in boxes),
            ),
            max=Point(
                max(box.max.x for box in boxes),
                max(box.max.y for box in boxes),
            ),
        )
    if len(_ANCHORED_COPPER) >= _ANCHORED_COPPER_LIMIT:
        _ANCHORED_COPPER.clear()
    entry = _AnchoredCopper(footprint=footprint, pads=tuple(pads), box=box)
    _ANCHORED_COPPER[key] = entry
    return entry


def _shifted(rect: Rectangle, pos: Point) -> Rectangle:
    return Rectangle(
        min=Point(rect.min.x + pos.x, rect.min.y + pos.y),
        max=Point(rect.max.x + pos.x, rect.max.y + pos.y),
    )


def _capsule_box(capsule: Capsule) -> Rectangle:
    """The capsule's bounding box — the core grown by the radius per
    axis. The one extent definition: the union boxes the pair pass and
    the edge check skip on, and the boxes the #95 repair vocabulary
    separates, price one geometry."""
    radius = capsule.radius
    return Rectangle(
        min=Point(capsule.core.min.x - radius, capsule.core.min.y - radius),
        max=Point(capsule.core.max.x + radius, capsule.core.max.y + radius),
    )


def _pad_coppers(
    placement: Placement, net_by_pin: Mapping[tuple[str, str], str], stackup
) -> tuple[_PadCopper, ...]:
    """The placement's copper-bearing pads — the layer filter applied,
    capsules and centres world-resolved (the origin resolution shifted to
    the anchor)."""
    pos = placement.pos
    return tuple(
        _PadCopper(
            index=index,
            number=pad.number,
            pad=pad,
            capsule=Capsule(core=_shifted(capsule.core, pos), radius=capsule.radius),
            center=Point(center.x + pos.x, center.y + pos.y),
            layers=layers,
            net=net_by_pin.get((placement.ref, pad.number)),
        )
        for index, pad, capsule, center, layers in _anchored_copper(
            placement, stackup
        ).pads
    )


def _net_tie_exempt(footprint: Footprint, number_a: str, number_b: str) -> bool:
    """Do the two pad numbers share a net-tie group? Groups are
    footprint-local — pads in one group never violate each other
    (ADR-0022's second exemption)."""
    return any(
        number_a in group and number_b in group for group in footprint.net_tie_groups
    )


def _pad_pair_exempt(
    pa: _PadCopper, pb: _PadCopper, tie_footprint: Footprint | None
) -> bool:
    """The pair exemptions, in verdict order (#103): no shared copper
    layer; same net (a pad with no net counts as its own, so the
    fiducials are checked against everything); a shared net-tie group —
    footprint-local, so ``tie_footprint`` rides in only for an
    intra-footprint pair and is ``None`` between placements. The
    exemption half of the one pad-pair verdict; pricing lives in
    ``_pad_pair_price``."""
    if not (pa.layers & pb.layers):
        return True
    if pa.net is not None and pa.net == pb.net:
        return True
    return tie_footprint is not None and _net_tie_exempt(
        tie_footprint, pa.number, pb.number
    )


def _pad_pair_price(
    capsule_a: Capsule, capsule_b: Capsule, clearance_nm: int
) -> tuple[str, int] | None:
    """The pair verdict under the strict rule (#103): intersecting
    capsules short; otherwise a gap strictly below ``clearance_nm`` is a
    clearance — a gap equal to the rule is clean. ``None`` is clean. The
    one definition of ADR-0022's short/clearance split; the exemption
    half lives in ``_pad_pair_exempt``."""
    if capsules_overlap(capsule_a, capsule_b):
        return "short", 0
    gap = capsule_gap_nm(capsule_a, capsule_b)
    if gap >= clearance_nm:
        return None
    return "clearance", gap


_last_pad_pass: tuple[Board, frozenset[str], tuple[_PadPair, ...]] | None = None


def _pad_pair_records(
    board: Board, refs: frozenset[str] = frozenset()
) -> tuple[_PadPair, ...]:
    """Every violating pad pair, or — when ``refs`` names moved refs —
    exactly those touching one (the Locality contract's drop-and-rebuild
    half; ADR-0016). The shared body of the shorting and clearance checks,
    their companions, and the repair vocabulary.

    One pass serves both checks (#98): the last ``(board, refs)`` reading
    is kept, so ``run_drc`` and the Evaluator — which hand every check and
    companion the same Board object — price each pair once. Boards are
    frozen, so the identity match is exact.
    """
    global _last_pad_pass
    last = _last_pad_pass
    if last is not None and last[0] is board and last[1] == refs:
        return last[2]
    records = _pad_pair_pass(board, refs)
    _last_pad_pass = (board, refs, records)
    return records


def _pad_pair_pass(board: Board, refs: frozenset[str]) -> tuple[_PadPair, ...]:
    """The pair pass itself. A placement pair whose copper boxes sit
    provably clear of the rule is skipped unpriced (``_boxes_clear``);
    otherwise every pad pair prices through the one verdict (#103) —
    ``_pad_pair_exempt`` (no shared copper layer; same net; a shared
    net-tie group, intra-footprint only) then ``_pad_pair_price``
    (intersecting capsules short, a gap strictly below ``clearance_nm``
    a clearance).
    """
    net_by_pin = _net_map(board)
    placements = board.placements
    count = len(placements)
    clearance_nm = board.design_rules.clearance_nm
    boxes = [_anchored_copper(p, board.stackup).box for p in placements]
    if refs:
        movers = [i for i, p in enumerate(placements) if p.ref in refs]
        index_pairs = sorted(
            {(min(m, k), max(m, k)) for m in movers for k in range(count)}
        )
    else:
        index_pairs = [(i, j) for i in range(count) for j in range(i, count)]
    copper_cache: dict[int, tuple[_PadCopper, ...]] = {}

    def coppers_of(index: int) -> tuple[_PadCopper, ...]:
        if index not in copper_cache:
            copper_cache[index] = _pad_coppers(
                placements[index], net_by_pin, board.stackup
            )
        return copper_cache[index]

    records: list[_PadPair] = []
    for i, j in index_pairs:
        box_a, box_b = boxes[i], boxes[j]
        if box_a is None or box_b is None:
            continue
        a, b = placements[i], placements[j]
        if i != j and _boxes_clear(box_a, a.pos, box_b, b.pos, clearance_nm):
            continue
        coppers_a = coppers_of(i)
        if a is b:
            pairs = (
                (pa, pb) for k, pa in enumerate(coppers_a) for pb in coppers_a[k + 1 :]
            )
        else:
            pairs = ((pa, pb) for pa in coppers_a for pb in coppers_of(j))
        for pa, pb in pairs:
            if _pad_pair_exempt(pa, pb, a.footprint if a is b else None):
                continue
            priced = _pad_pair_price(pa.capsule, pb.capsule, clearance_nm)
            if priced is None:
                continue
            records.append(_pad_pair_record(a, b, pa, pb, *priced, board))
    return tuple(records)


def _pad_pair_record(
    a: Placement,
    b: Placement,
    pa: _PadCopper,
    pb: _PadCopper,
    kind: str,
    gap: int,
    board: Board,
) -> _PadPair:
    """The finding in canonical form: placement refs sorted, locations
    and pad indices paired to that order, the description derived from
    that form (run_drc's canonicalization rebuilds it identically)."""
    ref_a, ref_b = sorted((f"placement:{a.ref}", f"placement:{b.ref}"))
    if ref_a == f"placement:{a.ref}":
        locations = (pa.center, pb.center)
        pad_indices = (pa.index, pb.index)
    else:
        locations = (pb.center, pa.center)
        pad_indices = (pb.index, pa.index)
    violation_type = (
        ViolationType.SHORTING_ITEMS if kind == "short" else ViolationType.CLEARANCE
    )
    return _PadPair(
        refs=(ref_a, ref_b),
        pad_indices=pad_indices,
        gap_nm=gap,
        violation=Violation(
            type=violation_type,
            severity=Severity.ERROR,
            description=_describe(violation_type, (ref_a, ref_b), locations, board),
            offending_refs=(ref_a, ref_b),
            locations=locations,
        ),
    )


def _shorting_items(board: Board) -> tuple[Violation, ...]:
    """Different-net pad copper that intersects, at ERROR (ADR-0022).

    The short/clearance split is KiCad's own and mutually exclusive: an
    intersecting pair shorts, a separated-or-touching pair prices as a
    clearance. One finding per pad pair, placement-form refs — repeated
    for an intra-footprint pair, which is checked (KiCad checks them).
    """
    return tuple(
        record.violation
        for record in _pad_pair_records(board)
        if record.kind == "short"
    )


def _clearance(board: Board) -> tuple[Violation, ...]:
    """Pad copper separated by a gap strictly below ``clearance_nm`` —
    touching included, actual 0 — at ERROR (ADR-0022). The strict rule
    matches the oracle's measured behaviour: a gap equal to the rule is
    clean."""
    return tuple(
        record.violation
        for record in _pad_pair_records(board)
        if record.kind == "clearance"
    )


def _shorting_items_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the shorting check: the moved ref's pairs."""
    return tuple(
        record.violation
        for record in _pad_pair_records(board, frozenset({ref}))
        if record.kind == "short"
    )


def _clearance_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the clearance check: the moved ref's pairs."""
    return tuple(
        record.violation
        for record in _pad_pair_records(board, frozenset({ref}))
        if record.kind == "clearance"
    )


def _edge_line_gap(capsule: Capsule, outline: Rectangle, edge: Edge) -> int:
    """Signed copper-to-edge-line gap in nm — negative is copper past the
    line (a crossing), which the verdict counts as a zero gap."""
    match edge:
        case Edge.LEFT:
            return (capsule.core.min.x - capsule.radius) - outline.min.x
        case Edge.RIGHT:
            return outline.max.x - (capsule.core.max.x + capsule.radius)
        case Edge.TOP:
            return (capsule.core.min.y - capsule.radius) - outline.min.y
        case Edge.BOTTOM:
            return outline.max.y - (capsule.core.max.y + capsule.radius)
    raise ValueError(f"unknown edge {edge!r}")


def _capsule_beyond(capsule: Capsule, outline: Rectangle, edge: Edge) -> bool:
    """Is the whole capsule strictly beyond this edge line? A pad wholly
    off the board stays silent — the ADR-0018 probe's pinned reading —
    its overhang being the containment check's alone to report."""
    match edge:
        case Edge.LEFT:
            return capsule.core.max.x + capsule.radius < outline.min.x
        case Edge.RIGHT:
            return capsule.core.min.x - capsule.radius > outline.max.x
        case Edge.TOP:
            return capsule.core.max.y + capsule.radius < outline.min.y
        case Edge.BOTTOM:
            return capsule.core.min.y - capsule.radius > outline.max.y
    raise ValueError(f"unknown edge {edge!r}")


def _pad_edge_gap(
    placement: Placement,
    pad: Pad,
    exempt: Mapping[str, frozenset[Edge]],
    board: Board,
) -> int | None:
    """The pad's reported edge gap, or ``None`` when clean — the one body
    behind the check's verdict and the pinned wording.

    Per unstated edge line (a Requirement EdgeMount's stated edge is
    exempt — ADR-0018's mechanism extended to pad copper): copper wholly
    beyond the line is silent, otherwise a signed gap strictly below
    ``edge_clearance_nm`` violates, a crossing counting as zero. The
    reported gap is the worst violated edge's, clamped at zero."""
    rule = board.design_rules.edge_clearance_nm
    capsule = pad_world_outline(placement, pad)
    unstated = exempt.get(placement.ref, frozenset())
    violated = [
        gap
        for edge in Edge
        if edge not in unstated
        and not _capsule_beyond(capsule, board.outline, edge)
        and (gap := _edge_line_gap(capsule, board.outline, edge)) < rule
    ]
    if not violated:
        return None
    return max(min(violated), 0)


def _pad_edge_record(
    placement: Placement,
    copper: _PadCopper,
    exempt: Mapping[str, frozenset[Edge]],
    board: Board,
) -> _PadEdge | None:
    """The pad's edge finding built from ``_pad_edge_gap``'s verdict."""
    gap = _pad_edge_gap(placement, copper.pad, exempt, board)
    if gap is None:
        return None
    ref = f"placement:{placement.ref}"
    location = copper.center
    return _PadEdge(
        ref=placement.ref,
        pad_index=copper.index,
        gap_nm=gap,
        violation=Violation(
            type=ViolationType.COPPER_EDGE_CLEARANCE,
            severity=Severity.ERROR,
            description=_describe(
                ViolationType.COPPER_EDGE_CLEARANCE, (ref,), (location,), board
            ),
            offending_refs=(ref,),
            locations=(location,),
        ),
    )


def _copper_edge_clearance(board: Board) -> tuple[Violation, ...]:
    """Pad copper within ``edge_clearance_nm`` of an outline edge line, at
    ERROR (ADR-0022) — an adjacency check against the board's rules, not
    containment (ADR-0018's probe: that check stays the courtyard's
    alone). One finding per pad regardless of how many edges it
    violates, the location the pad's world centre; a pad wholly off the
    board stays silent."""
    exempt = _edge_mount_exemptions(board)
    findings: list[Violation] = []
    for placement in board.placements:
        findings.extend(_placement_edge_findings(placement, exempt, board))
    return tuple(findings)


def _copper_edge_clearance_companion(board: Board, ref: str) -> tuple[Violation, ...]:
    """Locality companion of the edge check: the moved ref's pads only."""
    exempt = _edge_mount_exemptions(board)
    findings: list[Violation] = []
    for placement in board.placements:
        if placement.ref == ref:
            findings.extend(_placement_edge_findings(placement, exempt, board))
    return tuple(findings)


def _placement_edge_findings(
    placement: Placement, exempt: Mapping[str, frozenset[Edge]], board: Board
) -> list[Violation]:
    """One placement's edge findings. A copper box sitting at least the
    rule inside every edge line is skipped unpriced (#98): each pad's
    signed edge gap is at least its box's, so no edge can violate."""
    box = _anchored_copper(placement, board.stackup).box
    if box is None:
        return []
    box = _shifted(box, placement.pos)
    if _clear_of_outline(box, board.outline, board.design_rules.edge_clearance_nm):
        return []
    findings = []
    for copper in _pad_coppers(placement, _net_map(board), board.stackup):
        record = _pad_edge_record(placement, copper, exempt, board)
        if record is not None:
            findings.append(record.violation)
    return findings


CHECKS: tuple[Callable[[Board], tuple[Violation, ...]], ...] = (
    _courtyard_overlap,
    _courtyard_outside_outline,
    _proximity_unmet,
    _region_unmet,
    _edge_mount_unmet,
    _keep_together_unmet,
    _shorting_items,
    _clearance,
    _copper_edge_clearance,
)

Companion = Callable[[Board, str], tuple[Violation, ...]]
"""What a locality companion is (ADR-0016): ``companion(board, ref)`` returns
exactly the findings ``check(board)`` reports whose ``offending_refs``
contain ``placement:<ref>`` — GLOSSARY.md's Locality axiom. ``ref`` is the
bare placement ref, the Evaluator's slot key."""

COMPANIONS: dict[Callable[[Board], tuple[Violation, ...]], Companion] = {
    _courtyard_outside_outline: _courtyard_outside_outline_companion,
    _proximity_unmet: _proximity_unmet_companion,
    _region_unmet: _region_unmet_companion,
    _edge_mount_unmet: _edge_mount_unmet_companion,
    _keep_together_unmet: _keep_together_unmet_companion,
    _shorting_items: _shorting_items_companion,
    _clearance: _clearance_companion,
    _copper_edge_clearance: _copper_edge_clearance_companion,
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
