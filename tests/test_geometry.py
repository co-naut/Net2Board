"""S1 — geometry value semantics (frozen, exact equality, exact hashing)."""

from dataclasses import FrozenInstanceError

import pytest

from net2board.geometry import Circle, Point, Rectangle, Shape, mirror_local

P11 = Point(1, 1)
P22 = Point(2, 2)
P33 = Point(3, 3)


def test_point_is_frozen_and_compares_exact():
    with pytest.raises(FrozenInstanceError):
        P11.x = 5
    assert Point(1, 1) == P11
    assert Point(1, 2) != P11


def test_rectangle_is_frozen_and_compares_exact():
    rect = Rectangle(min=P11, max=P22)
    with pytest.raises(FrozenInstanceError):
        rect.min = Point(0, 0)
    assert Rectangle(min=Point(1, 1), max=P22) == rect
    assert Rectangle(min=P11, max=P33) != rect


def test_circle_is_frozen_and_compares_exact():
    circ = Circle(center=P11, radius_sq=4, end=Point(3, 1))
    with pytest.raises(FrozenInstanceError):
        circ.radius_sq = 9
    assert Circle(center=Point(1, 1), radius_sq=4, end=Point(3, 1)) == circ
    assert Circle(center=P11, radius_sq=9, end=Point(4, 1)) != circ


def test_primitives_hash_consistently_with_equality():
    assert hash(Point(1, 1)) == hash(P11)
    assert hash(Rectangle(P11, P22)) == hash(Rectangle(Point(1, 1), Point(2, 2)))
    assert hash(Circle(P11, 4, Point(3, 1))) == hash(
        Circle(center=P11, radius_sq=4, end=Point(3, 1))
    )


def test_shape_union_accepts_both_variants():
    shapes: tuple[Shape, ...] = (Rectangle(P11, P22), Circle(P11, 4, Point(3, 1)))
    assert len(shapes) == 2


def test_circle_radius_sq_stays_exact_integer():
    assert isinstance(Circle(P11, 4, Point(3, 1)).radius_sq, int)


def test_rectangle_from_corners_normalizes_any_corner_order():
    rect = Rectangle.from_corners(Point(2, 1), Point(1, 3))
    assert rect == Rectangle(min=Point(1, 1), max=Point(2, 3))
    assert Rectangle.from_corners(Point(1, 1), Point(2, 3)) == rect


def test_mirror_local_flips_x_on_back_side_only():
    assert mirror_local(Point(3, -2), "B.Cu") == Point(-3, -2)
    assert mirror_local(Point(3, -2), "F.Cu") == Point(3, -2)
