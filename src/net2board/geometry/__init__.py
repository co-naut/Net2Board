"""Geometry primitives — exact-integer nanometre coordinates (ADR-0005, ADR-0006).

All coordinates are integer nanometres; millimetres appear only at the
import/export boundaries. Every primitive is a frozen value object so
coordinate-bearing snapshots compare and hash by exact equality (ADR-0001).

Beside the predicates lives the **measure family** (#42): the one definition
the constraint checks and the objective's Preference hinges share
(ADR-0011, ADR-0014). Predicates stay square-root-free; *magnitudes* use
``math.isqrt`` — exact, platform-independent, floor-rounded, sub-nanometre
error physically meaningless (ADR-0014). ``shape_aabb`` is that ADR's stated
circle resolution (``center ± isqrt(radius_sq)``), and it is what turns a
courtyard into the box Region, EdgeMount, and KeepTogether measure.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isqrt

__all__ = [
    "Circle",
    "Point",
    "Rectangle",
    "Shape",
    "edge_face_offset_nm",
    "mirror_local",
    "pad_distance_nm",
    "pad_world_bbox",
    "pad_world_center",
    "region_shortfall_nm",
    "shape_aabb",
    "shape_contained_in",
    "shapes_overlap",
    "union_bbox",
    "world_courtyard",
    "world_point",
]


@dataclass(frozen=True)
class Point:
    """A position in integer-nanometre board space."""

    x: int
    y: int


@dataclass(frozen=True)
class Rectangle:
    """An axis-aligned bounding box as exact corner coordinates."""

    min: Point
    max: Point

    @classmethod
    def from_corners(cls, a: Point, b: Point) -> Rectangle:
        """The AABB spanned by two opposite corners, in any order.

        The one normalization site for corner pairs — the importer
        normalizes raw ``fp_rect`` endpoints with it, and transforms that
        remap corners (rotation, the back-side mirror) re-normalize
        through it so an AABB stays in-type.
        """
        return cls(
            min=Point(min(a.x, b.x), min(a.y, b.y)),
            max=Point(max(a.x, b.x), max(a.y, b.y)),
        )


@dataclass(frozen=True)
class Circle:
    """A circle as exact-integer squared radius plus its defining end point.

    ``radius_sq`` keeps overlap arithmetic exact-integer (ADR-0006); ``end``
    is the defining point of the KiCad ``fp_circle`` — the defining radius
    vector from ``center`` — carried through the IR so export can transform
    and emit it as an ordinary point (ADR-0010). The three fields are
    redundant by design: each serves a consumer the others cannot.
    """

    center: Point
    radius_sq: int
    end: Point


Shape = Rectangle | Circle


def mirror_local(point: Point, side: str) -> Point:
    """Mirror a footprint-local point's x for a placement on ``"B.Cu"``.

    KiCad's left-right flip — the one convention every writer of
    footprint-local coordinates must share (ADR-0006): the transforms
    compose it (``world_point``, so also ``world_courtyard``), and the
    exporter bakes it into the emitted locals because KiCad applies
    translate + rotate only on load.
    """
    return Point(-point.x, point.y) if side == "B.Cu" else point


def world_point(local: Point, placement) -> Point:
    """A footprint-local point in world coordinates (ADR-0014).

    The one local → world point transform, composed innermost first:
    mirror local x on ``"B.Cu"`` (``mirror_local``), rotate by the
    placement's 90° increment CCW-as-displayed in the y-down board frame,
    translate to the anchor point. ``world_courtyard`` is this transform
    applied to a courtyard's local points; pad consumers (Proximity,
    NetSpan) reach pad centers through ``pad_world_center`` — one
    convention, no second transform (ADR-0014).

    ``placement`` is duck-typed (``.pos``, ``.rotation``, ``.side``) so
    this module never imports the model; an unknown rotation raises
    ``ValueError`` — fail-fast, matching the model's own placement
    validation, so an unvalidated rotation can never silently measure
    as 0.
    """
    mirrored = mirror_local(local, placement.side)
    x, y = mirrored.x, mirrored.y
    rotation = placement.rotation
    if rotation == 0:
        pass
    elif rotation == 90:
        x, y = y, -x
    elif rotation == 180:
        x, y = -x, -y
    elif rotation == 270:
        x, y = -y, x
    else:
        raise ValueError(
            f"invalid rotation {rotation!r}: must be one of "
            f"(0, 90, 180, 270) (90° increments)"
        )
    pos = placement.pos
    return Point(pos.x + x, pos.y + y)


def pad_world_center(placement, pad_number: str) -> Point:
    """A pad's world-coordinate center (ADR-0014 — pad outlines stay deferred).

    ``world_point`` applied to the pad's footprint-local position: correct
    for rotation and back-side mirroring by construction. Unknown pad
    numbers raise ``ValueError`` immediately (ADR-0003's fail-fast upsert
    posture) — a None or a made-up point would let a constraint silently
    anchor to nothing.

    ``placement`` is duck-typed (``.ref``, ``.footprint.pads``, ``.pos``,
    ``.rotation``, ``.side``) so this module never imports the model.
    """
    for pad in placement.footprint.pads:
        if pad.number == pad_number:
            return world_point(pad.local_pos, placement)
    raise ValueError(
        f"unknown pad {pad_number!r} on {placement.ref!r}: footprint has pads "
        f"{tuple(pad.number for pad in placement.footprint.pads)}"
    )


def world_courtyard(placement) -> Shape | None:
    """A placement's courtyard in world space, or ``None`` if it has none.

    ``world_point`` applied to the courtyard's defining local points.
    Case analysis per shape: a rectangle stays axis-aligned under every
    legal transform, a circle keeps its ``radius_sq`` while ``center``
    and ``end`` move as ordinary points (ADR-0006, ADR-0010).

    ``placement`` is duck-typed (``.footprint.courtyard``, ``.pos``,
    ``.rotation``, ``.side``) so this module never imports the model.
    """
    courtyard = placement.footprint.courtyard
    if courtyard is None:
        return None
    if isinstance(courtyard, Rectangle):
        return _world_rectangle(courtyard, placement)
    return _world_circle(courtyard, placement)


def _world_rectangle(rectangle: Rectangle, placement) -> Rectangle:
    return Rectangle.from_corners(
        world_point(rectangle.min, placement), world_point(rectangle.max, placement)
    )


def _world_circle(circle: Circle, placement) -> Circle:
    return Circle(
        center=world_point(circle.center, placement),
        radius_sq=circle.radius_sq,
        end=world_point(circle.end, placement),
    )


def shape_aabb(shape: Shape) -> Rectangle:
    """A shape's axis-aligned bounding box, exact-integer (ADR-0014).

    The isqrt resolution ADR-0014 states: a Circle courtyard measures as
    ``center ± isqrt(radius_sq)`` — floor-rounded, sub-nanometre error
    physically meaningless — while a Rectangle passes through unchanged.
    The measure box behind Region, EdgeMount, and KeepTogether (#42);
    ``end`` (the circle's export point, ADR-0010) plays no part.
    """
    if isinstance(shape, Rectangle):
        return shape
    radius = isqrt(shape.radius_sq)
    center = shape.center
    return Rectangle(
        min=Point(center.x - radius, center.y - radius),
        max=Point(center.x + radius, center.y + radius),
    )


def pad_world_bbox(placement) -> Rectangle:
    """The union bounding box of a placement's pads in world coordinates.

    The courtyard-less fallback for Region, EdgeMount, and KeepTogether
    (ADR-0011, amended by #42 to cover EdgeMount too): every pad measures
    as its box ``local_pos ± half size`` — floor division, sub-nanometre
    stance — transformed by ``world_point`` and normalized per pad, so
    rotation and the back-side mirror compose exactly as they do for
    courtyards. A footprint with no pads has nothing to measure:
    ``ValueError`` names it (``pad_world_center``'s fail-fast posture).

    ``placement`` is duck-typed (``.footprint.pads``, ``.pos``,
    ``.rotation``, ``.side``) so this module never imports the model.
    """
    pads = placement.footprint.pads
    if not pads:
        raise ValueError(
            f"no pads on {placement.ref!r}: footprint "
            f"{placement.footprint.entry_name!r} has neither courtyard nor "
            f"pads to measure"
        )

    def pad_box(pad) -> Rectangle:
        half = Point(pad.size[0] // 2, pad.size[1] // 2)
        return Rectangle.from_corners(
            world_point(
                Point(pad.local_pos.x - half.x, pad.local_pos.y - half.y), placement
            ),
            world_point(
                Point(pad.local_pos.x + half.x, pad.local_pos.y + half.y), placement
            ),
        )

    return union_bbox(pad_box(pad) for pad in pads)


def pad_distance_nm(
    placement_a, pad_number_a: str, placement_b, pad_number_b: str
) -> int:
    """Pad-center to pad-center distance, ``isqrt`` of the squared span.

    Proximity's one distance definition (ADR-0011): Euclidean between the
    two ``pad_world_center`` values, floored by ``isqrt`` (ADR-0014's
    magnitude stance). This integer *is* the measure both consumers read:
    the Requirement check compares it against the inclusive bound and the
    Preference hinge charges ``max(0, it − bound)`` — comparing squares
    instead would disagree with the hinge below one nanometre, where
    ``d² > bound²`` while ``isqrt(d²) ≤ bound``. Unknown pad numbers raise
    ``ValueError`` from ``pad_world_center``.
    """
    a = pad_world_center(placement_a, pad_number_a)
    b = pad_world_center(placement_b, pad_number_b)
    dx, dy = a.x - b.x, a.y - b.y
    return isqrt(dx * dx + dy * dy)


def edge_face_offset_nm(box: Rectangle, outline: Rectangle, edge: str) -> int:
    """A box face's signed offset from an outline edge line, in nm.

    Positive is past the edge line (outside), zero flush, negative inside
    by that many nm — one function answering EdgeMount's Requirement check
    (unmet past the permitted overhang *or* short of the edge) and its
    Preference hinge (shortfall on either side, ADR-0014). ``edge`` is one
    of ``"left"``/``"right"``/``"top"``/``"bottom"`` — the model's ``Edge``
    value strings; geometry never imports the model, so callers pass
    ``relation.edge.value`` (the ``world_point`` duck-typing rule).
    """
    match edge:
        case "left":
            return outline.min.x - box.min.x
        case "right":
            return box.max.x - outline.max.x
        case "top":
            return outline.min.y - box.min.y
        case "bottom":
            return box.max.y - outline.max.y
    raise ValueError(
        f"unknown edge {edge!r}: expected 'left', 'right', 'top', or 'bottom'"
    )


def region_shortfall_nm(box: Rectangle, rect: Rectangle) -> int:
    """Sum of a box's axis overhangs outside a rectangle, 0 when inside.

    Region's one measure (ADR-0014): each axis contributes the nm the box
    pokes past the rectangle's two faces — closed containment, boundary
    contact contributes nothing (ADR-0011) — and the sum is the Preference
    hinge's raw nanometres as well as the Requirement check's zero test.
    """
    return (
        max(0, rect.min.x - box.min.x)
        + max(0, box.max.x - rect.max.x)
        + max(0, rect.min.y - box.min.y)
        + max(0, box.max.y - rect.max.y)
    )


def union_bbox(boxes: Iterable[Rectangle]) -> Rectangle:
    """The smallest rectangle enclosing every box, exact-integer.

    KeepTogether's union bounding box (ADR-0011) — the extents KeepTogether
    measures are this rectangle's width and height. An empty iterable is a
    caller bug: ``ValueError``, not an invented empty box.
    """
    iterator = iter(boxes)
    try:
        first = next(iterator)
    except StopIteration:
        raise ValueError("no boxes: a union bounding box needs at least one") from None
    min_x, min_y, max_x, max_y = first.min.x, first.min.y, first.max.x, first.max.y
    for box in iterator:
        min_x = min(min_x, box.min.x)
        min_y = min(min_y, box.min.y)
        max_x = max(max_x, box.max.x)
        max_y = max(max_y, box.max.y)
    return Rectangle(min=Point(min_x, min_y), max=Point(max_x, max_y))


def shape_contained_in(shape: Shape, rect: Rectangle) -> bool:
    """Closed containment: does ``shape`` lie inside the ``rect`` outline?

    Boundary contact is inside — a courtyard tangent to the outline edge
    is contained (ADR-0018), consistent with overlap's
    touching-never-violates (ADR-0006) and Region's closed membership
    (ADR-0011). ``rect`` is the container (board outline, Region) and is
    a ``Rectangle`` only: the rectangle-only scope is stated in the
    containment check's contract, with a graduation trigger for the first
    non-rectangular outline or cutout (ADR-0018).

    Every case is decided in exact-integer arithmetic — rect-in-rect is
    four comparisons, circle-in-rect compares squared distances against
    ``radius_sq``, and no square root is ever taken (ADR-0005, ADR-0006).
    """
    if isinstance(shape, Rectangle):
        return _rect_in_rect(shape, rect)
    return _circle_in_rect(shape, rect)


def _rect_in_rect(inner: Rectangle, outer: Rectangle) -> bool:
    return (
        outer.min.x <= inner.min.x
        and inner.max.x <= outer.max.x
        and outer.min.y <= inner.min.y
        and inner.max.y <= outer.max.y
    )


def _circle_in_rect(circle: Circle, outer: Rectangle) -> bool:
    # Per-edge: the center-to-edge distance must reach the radius — the
    # sign check separates inside-facing from outside-facing distances,
    # which squaring alone would conflate.
    def edge_reaches(distance: int) -> bool:
        return distance >= 0 and distance * distance >= circle.radius_sq

    return (
        edge_reaches(circle.center.x - outer.min.x)
        and edge_reaches(outer.max.x - circle.center.x)
        and edge_reaches(circle.center.y - outer.min.y)
        and edge_reaches(outer.max.y - circle.center.y)
    )


def shapes_overlap(a: Shape, b: Shape) -> bool:
    """Strict-interior intersection: touching edges and tangent points are
    not overlaps (ADR-0006, empirically matching the KiCad oracle).

    Every case is decided in exact-integer arithmetic — circle radii enter
    as ``radius_sq``, and the circle-circle comparison is rearranged and
    squared so no square root is ever taken.
    """
    if isinstance(a, Rectangle) and isinstance(b, Rectangle):
        return _rects_overlap(a, b)
    if isinstance(a, Circle) and isinstance(b, Circle):
        return _circles_overlap(a, b)
    if isinstance(a, Circle):
        return _circle_rect_overlap(a, b)
    return _circle_rect_overlap(b, a)


def _rects_overlap(a: Rectangle, b: Rectangle) -> bool:
    return (
        a.min.x < b.max.x
        and b.min.x < a.max.x
        and a.min.y < b.max.y
        and b.min.y < a.max.y
    )


def _circle_rect_overlap(circle: Circle, rect: Rectangle) -> bool:
    closest_x = min(max(circle.center.x, rect.min.x), rect.max.x)
    closest_y = min(max(circle.center.y, rect.min.y), rect.max.y)
    dx = circle.center.x - closest_x
    dy = circle.center.y - closest_y
    return dx * dx + dy * dy < circle.radius_sq


def _circles_overlap(a: Circle, b: Circle) -> bool:
    # d² < (rₐ + rᵦ)² with rᵢ = √(radius_sqᵢ), rearranged to
    # d² - rₐ² - rᵦ² < 2·rₐ·rᵦ; both sides non-negative, so square.
    dx = a.center.x - b.center.x
    dy = a.center.y - b.center.y
    lhs = dx * dx + dy * dy - a.radius_sq - b.radius_sq
    if lhs < 0:
        return True
    return lhs * lhs < 4 * a.radius_sq * b.radius_sq
