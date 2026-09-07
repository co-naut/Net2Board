"""Geometry primitives — exact-integer nanometre coordinates (ADR-0005, ADR-0006).

All coordinates are integer nanometres; millimetres appear only at the
import/export boundaries. Every primitive is a frozen value object so
coordinate-bearing snapshots compare and hash by exact equality (ADR-0001).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Circle",
    "Point",
    "Rectangle",
    "Shape",
    "mirror_local",
    "shapes_overlap",
    "world_courtyard",
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
    footprint-local coordinates must share (ADR-0006): DRC composes it
    into ``world_courtyard``, and the exporter bakes it into the emitted
    locals because KiCad applies translate + rotate only on load.
    """
    return Point(-point.x, point.y) if side == "B.Cu" else point


def world_courtyard(placement) -> Shape | None:
    """A placement's courtyard in world space, or ``None`` if it has none.

    Composes, innermost first: mirror local x on ``"B.Cu"`` (KiCad's
    left-right flip — the writer owns the mirror: the oracle applies
    translate + rotate only, probed on kicad-cli 10.0.6), rotate by the
    placement's 90° increment CCW-as-displayed in the y-down board frame,
    translate to the anchor point. Case analysis per shape: a rectangle
    stays axis-aligned under every legal transform, a circle keeps its
    ``radius_sq`` while ``center`` and ``end`` move as ordinary points
    (ADR-0006, ADR-0010).

    ``placement`` is duck-typed (``.footprint.courtyard``, ``.pos``,
    ``.rotation``, ``.side``) so this module never imports the model.
    """
    courtyard = placement.footprint.courtyard
    if courtyard is None:
        return None
    if isinstance(courtyard, Rectangle):
        return _world_rectangle(courtyard, placement)
    return _world_circle(courtyard, placement)


def _local_offset(point: Point, placement) -> Point:
    """Mirror + rotate a footprint-local offset, before translation."""
    mirrored = mirror_local(point, placement.side)
    x, y = mirrored.x, mirrored.y
    if placement.rotation == 90:
        x, y = y, -x
    elif placement.rotation == 180:
        x, y = -x, -y
    elif placement.rotation == 270:
        x, y = -y, x
    return Point(x, y)


def _world_rectangle(rectangle: Rectangle, placement) -> Rectangle:
    pos = placement.pos
    a = _local_offset(rectangle.min, placement)
    b = _local_offset(rectangle.max, placement)
    return Rectangle.from_corners(
        Point(pos.x + a.x, pos.y + a.y), Point(pos.x + b.x, pos.y + b.y)
    )


def _world_circle(circle: Circle, placement) -> Circle:
    pos = placement.pos
    center = _local_offset(circle.center, placement)
    end = _local_offset(circle.end, placement)
    return Circle(
        center=Point(pos.x + center.x, pos.y + center.y),
        radius_sq=circle.radius_sq,
        end=Point(pos.x + end.x, pos.y + end.y),
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
