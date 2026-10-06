"""Public point transforms: world_point and pad_world_center (ADR-0005, ADR-0014).

``world_point`` is ``geometry._local_offset`` promoted — the one local →
world point transform, composed innermost first: mirror local x on the
back side (KiCad's left-right flip, ADR-0006), rotate by the placement's
90° increment CCW-as-displayed in the y-down board frame, translate to
the anchor. ``pad_world_center`` reads nothing but the same transform —
pad centers (Proximity, NetSpan) and courtyard corners must never fork a
second convention, so the property tests hold them against
``world_courtyard`` across the whole transform space: 4 rotations × 2
sides, exhaustive — there is no other case.
"""

import pytest

from conftest import fresh_board
from net2board.geometry import (
    Point,
    Rectangle,
    pad_world_center,
    world_courtyard,
    world_point,
)

# Exact-value table for RES pad "9", local (-1_000_000, 2_000_000) — an
# asymmetric offset so each case proves its own composition order — at
# anchor (10_000_000, 20_000_000). Back-side cases are mirror-then-rotate;
# rotate-then-mirror would give different offsets.
PAD9_WORLD = {
    ("F.Cu", 0): (9_000_000, 22_000_000),
    ("F.Cu", 90): (12_000_000, 21_000_000),
    ("F.Cu", 180): (11_000_000, 18_000_000),
    ("F.Cu", 270): (8_000_000, 19_000_000),
    ("B.Cu", 0): (11_000_000, 22_000_000),
    ("B.Cu", 90): (12_000_000, 19_000_000),
    ("B.Cu", 180): (9_000_000, 18_000_000),
    ("B.Cu", 270): (8_000_000, 21_000_000),
}

ROTATIONS = (0, 90, 180, 270)
SIDES = ("F.Cu", "B.Cu")
ANCHOR = (10_000_000, 20_000_000)

# RES courtyard (local, nm): x -1_050_000..8_670_000, y -1_500_000..1_500_000.
# BIGRES courtyard (local, nm): center (5_000_000, 0), radius_sq 10e12,
# end (8_000_000, 1_000_000) — off-axis, so the center offset orbits.
RES_YARD_MIN = Point(-1_050_000, -1_500_000)
RES_YARD_MAX = Point(8_670_000, 1_500_000)
BIGRES_CENTER = Point(5_000_000, 0)
BIGRES_END = Point(8_000_000, 1_000_000)


def placed(ref, rotation, side):
    board = fresh_board().with_placement(ref, *ANCHOR, rotation, side)
    return next(p for p in board.placements if p.ref == ref)


class TestWorldPoint:
    @pytest.mark.parametrize("rotation", ROTATIONS)
    @pytest.mark.parametrize("side", SIDES)
    def test_exact_composition_for_every_case(self, side, rotation):
        placement = placed("R1", rotation, side)
        expected = Point(*PAD9_WORLD[(side, rotation)])
        assert world_point(Point(-1_000_000, 2_000_000), placement) == expected

    def test_origin_local_lands_on_the_anchor(self):
        assert world_point(Point(0, 0), placed("R1", 270, "B.Cu")) == Point(*ANCHOR)


class TestAgreesWithWorldCourtyard:
    """The courtyard transform is world_point applied to its local points.

    Post-promotion the agreement is by construction — that is the point
    of the one-transform rule (ADR-0014). The non-circular anchors are
    the hand-computed tables: PAD9_WORLD pins ``world_point`` here, and
    ``test_world_courtyard`` pins ``world_courtyard`` against hand values
    for every rotation and side. This class pins the decomposition, so
    neither side can silently fork its own composition later.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    @pytest.mark.parametrize("side", SIDES)
    def test_rectangle_courtyard_is_its_corners_world_points(self, side, rotation):
        placement = placed("R1", rotation, side)
        yard = Rectangle.from_corners(
            world_point(RES_YARD_MIN, placement),
            world_point(RES_YARD_MAX, placement),
        )
        assert world_courtyard(placement) == yard

    @pytest.mark.parametrize("rotation", ROTATIONS)
    @pytest.mark.parametrize("side", SIDES)
    def test_circle_courtyard_is_center_and_end_world_points(self, side, rotation):
        placement = placed("R3", rotation, side)
        yard = world_courtyard(placement)
        assert yard.center == world_point(BIGRES_CENTER, placement)
        assert yard.end == world_point(BIGRES_END, placement)
        assert yard.radius_sq == 10_000_000_000_000


class TestPadWorldCenter:
    @pytest.mark.parametrize("rotation", ROTATIONS)
    @pytest.mark.parametrize("side", SIDES)
    def test_exact_world_center_for_every_case(self, side, rotation):
        placement = placed("R1", rotation, side)
        assert pad_world_center(placement, "9") == Point(*PAD9_WORLD[(side, rotation)])

    def test_through_hole_pad_on_the_anchor(self):
        # RES pad "1" sits at the footprint origin: side and rotation are
        # invisible — through-hole pads are side-invariant (ADR-0006).
        for rotation in ROTATIONS:
            for side in SIDES:
                placement = placed("R1", rotation, side)
                assert pad_world_center(placement, "1") == Point(*ANCHOR)

    def test_bigres_far_pad_rotates_around_the_anchor(self):
        # BIGRES pad "2", local (10_000_000, 0): rot 90 carries it to
        # (0, -10_000_000) offset from the anchor.
        placement = placed("R3", 90, "F.Cu")
        assert pad_world_center(placement, "2") == Point(10_000_000, 10_000_000)

    @pytest.mark.parametrize("rotation", ROTATIONS)
    @pytest.mark.parametrize("side", SIDES)
    def test_agrees_with_world_point_of_the_pad_local(self, side, rotation):
        placement = placed("R1", rotation, side)
        pad = next(p for p in placement.footprint.pads if p.number == "9")
        assert pad_world_center(placement, "9") == world_point(pad.local_pos, placement)

    def test_unknown_pad_number_raises_value_error(self):
        with pytest.raises(ValueError, match="pad '99'"):
            pad_world_center(placed("R1", 0, "F.Cu"), "99")
