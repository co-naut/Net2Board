"""The measure family — the single definition the constraint checks and the
objective's Preference hinges share (ADR-0011, ADR-0014, #42).

Magnitudes are exact-integer nanometres with ``math.isqrt`` — floor-rounded,
platform-independent, sub-nanometre error physically meaningless (ADR-0014);
predicates stay square-root-free by comparing squares. ``shape_aabb`` is the
isqrt resolution ADR-0014 states ("a Circle courtyard resolves to
``center ± isqrt(radius_sq)``"); ``edge_face_offset_nm`` is signed — positive
is past the edge line, zero flush, negative inside — so the same function
answers EdgeMount's Requirement check and its Preference hinge. ``edge`` is
the model's ``Edge`` value string; geometry never imports the model
(``world_point``'s duck-typing rule).
"""

from types import SimpleNamespace

import pytest

from conftest import fresh_board, make_spec
from net2board.build import build_board
from net2board.geometry import (
    Circle,
    Point,
    Rectangle,
    edge_face_offset_nm,
    pad_distance_nm,
    pad_world_bbox,
    region_shortfall_nm,
    shape_aabb,
    union_bbox,
    world_point,
)
from net2board.ir import CompIR, FootprintIR, NetlistIR

OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
ANCHOR = (10_000_000, 20_000_000)


def rect(x0, y0, x1, y1):
    return Rectangle(min=Point(x0, y0), max=Point(x1, y1))


def circle(cx, cy, radius_sq):
    return Circle(center=Point(cx, cy), radius_sq=radius_sq, end=Point(cx, cy))


def placed(ref, rotation=0, side="F.Cu", anchor=ANCHOR):
    board = fresh_board().with_placement(ref, *anchor, rotation, side)
    return next(p for p in board.placements if p.ref == ref)


class TestShapeAabb:
    def test_rectangle_passes_through(self):
        box = rect(1, 2, 3, 4)
        assert shape_aabb(box) == box

    def test_integer_radius_circle_resolves_to_its_center_plus_radius(self):
        # radius 3: center (10, 20) → (7, 17)..(13, 23); `end` is inert here
        assert shape_aabb(circle(10, 20, 9)) == rect(7, 17, 13, 23)

    def test_non_integer_radius_floors_by_isqrt(self):
        # radius √10 ≈ 3.162: the AABB face sits at isqrt(10) = 3
        assert shape_aabb(circle(0, 0, 10)) == rect(-3, -3, 3, 3)

    def test_off_center_circle_floors_per_axis(self):
        # isqrt(2) = 1: a √2 nm radius AABB is one nm each way
        assert shape_aabb(circle(5, 7, 2)) == rect(4, 6, 6, 8)


class TestPadWorldBbox:
    def test_union_of_pad_boxes_at_rotation_zero(self):
        # RES pads (local nm): 1 at (0,0) ±800k; 2 at (7.62M, 0) ±800k;
        # 9 at (−1M, 2M) ±450k — the union spans (−1.45M, −800k)..(8.42M, 2.45M)
        assert pad_world_bbox(placed("R1")) == rect(
            8_550_000, 19_200_000, 18_420_000, 22_450_000
        )

    def test_transform_composes_for_rotation_and_side(self):
        # rot 90 carries local (x, y) to (y, −x): union becomes
        # (−800k, −8.42M)..(2.45M, 1.45M) around the anchor
        assert pad_world_bbox(placed("R1", 90, "F.Cu")) == rect(
            9_200_000, 11_580_000, 12_450_000, 21_450_000
        )
        # B.Cu mirrors local x first, then the rotation applies
        assert pad_world_bbox(placed("R1", 0, "B.Cu")) == rect(
            1_580_000, 19_200_000, 11_450_000, 22_450_000
        )

    def test_a_courtyard_is_not_read(self):
        # BIGRES' circle courtyard never enters: the pads bound the box
        assert pad_world_bbox(placed("R3")) == rect(
            9_000_000, 19_000_000, 21_000_000, 21_000_000
        )

    def test_padless_footprint_raises_value_error(self):
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOPAD"),), nets=()
            ),
            footprint_irs={
                "NOPAD": FootprintIR(entry_name="NOPAD", pads=(), courtyard=None)
            },
        )
        board = build_board(spec).with_placement("X1", 0, 0, 0, "F.Cu")
        with pytest.raises(ValueError, match="no pads"):
            pad_world_bbox(board.placements[0])


class TestPadDistanceNm:
    def test_pad_centers_measure_horizontally(self):
        # R1 pad 1 sits on the anchor; R2 pad 2 is 7.62 mm right of R2's anchor
        assert pad_distance_nm(placed("R1"), "1", placed("R2"), "2") == 7_620_000

    def test_rotation_enters_through_the_pad_center(self):
        # R2 rotated 90°: pad 2 moves to (0, −7.62M) relative to its anchor —
        # same anchor, so the distance is unchanged (a vertical span now)
        assert (
            pad_distance_nm(placed("R1"), "1", placed("R2", 90, "F.Cu"), "2")
            == 7_620_000
        )

    def test_distance_floors_to_isqrt(self):
        # anchors (0,0) and (1,2): d² = 5, isqrt = 2 — the floor stance
        assert (
            pad_distance_nm(
                placed("R1", anchor=(0, 0)), "1", placed("R3", anchor=(1, 2)), "1"
            )
            == 2
        )
        # d² = 25 is a perfect square: exact
        assert (
            pad_distance_nm(
                placed("R1", anchor=(0, 0)), "1", placed("R3", anchor=(3, 4)), "1"
            )
            == 5
        )

    def test_unknown_pad_raises_value_error(self):
        with pytest.raises(ValueError, match="pad '99'"):
            pad_distance_nm(placed("R1"), "99", placed("R2"), "1")

    def test_is_zero_for_the_same_pad(self):
        placement = placed("R1")
        assert pad_distance_nm(placement, "1", placement, "1") == 0


class TestEdgeFaceOffset:
    def test_inside_offsets_are_negative(self):
        box = rect(1_000_000, 1_000_000, 2_000_000, 2_000_000)
        assert edge_face_offset_nm(box, OUTLINE, "left") == -1_000_000
        assert edge_face_offset_nm(box, OUTLINE, "right") == -38_000_000
        assert edge_face_offset_nm(box, OUTLINE, "top") == -1_000_000
        assert edge_face_offset_nm(box, OUTLINE, "bottom") == -28_000_000

    def test_flush_is_zero(self):
        assert edge_face_offset_nm(rect(0, 1, 2, 3), OUTLINE, "left") == 0
        assert edge_face_offset_nm(rect(2, 1, 40_000_000, 3), OUTLINE, "right") == 0
        assert edge_face_offset_nm(rect(1, 0, 3, 2), OUTLINE, "top") == 0
        assert edge_face_offset_nm(rect(1, 2, 3, 30_000_000), OUTLINE, "bottom") == 0

    def test_past_offsets_are_positive(self):
        assert edge_face_offset_nm(rect(-500_000, 1, 2, 3), OUTLINE, "left") == 500_000
        assert (
            edge_face_offset_nm(rect(1, 2, 40_750_000, 3), OUTLINE, "right") == 750_000
        )
        # y-down: TOP is the minimum-y edge
        assert (
            edge_face_offset_nm(rect(1, -2_000_000, 3, 4), OUTLINE, "top") == 2_000_000
        )
        assert (
            edge_face_offset_nm(rect(1, 2, 3, 30_050_000), OUTLINE, "bottom") == 50_000
        )

    def test_unknown_edge_raises_value_error(self):
        with pytest.raises(ValueError, match="edge 'side'"):
            edge_face_offset_nm(rect(0, 0, 1, 1), OUTLINE, "side")


class TestRegionShortfall:
    def test_inside_is_zero(self):
        assert region_shortfall_nm(rect(1, 1, 2, 2), OUTLINE) == 0

    def test_touching_every_edge_is_zero(self):
        assert region_shortfall_nm(OUTLINE, OUTLINE) == 0

    def test_one_axis_poke_is_its_overhang(self):
        assert region_shortfall_nm(rect(-1, 1, 2, 2), OUTLINE) == 1
        assert region_shortfall_nm(rect(1, 1, 40_000_001, 2), OUTLINE) == 1

    def test_two_axis_pokes_sum(self):
        # 3 past the left, 2 past the bottom
        assert region_shortfall_nm(rect(-3, 1, 2, 30_000_002), OUTLINE) == 5

    def test_fully_outside_sums_all_four_pokes(self):
        # 2M past the right, 2M past the bottom; the min-side pokes clamp to 0
        assert (
            region_shortfall_nm(
                rect(41_000_000, 31_000_000, 42_000_000, 32_000_000), OUTLINE
            )
            == 4_000_000
        )


class TestUnionBbox:
    def test_two_boxes_enclose_both(self):
        assert union_bbox((rect(0, 0, 1, 1), rect(3, 4, 5, 6))) == rect(0, 0, 5, 6)

    def test_one_box_is_itself(self):
        box = rect(1, 2, 3, 4)
        assert union_bbox((box,)) == box

    def test_nested_boxes_enclose_the_inner(self):
        assert union_bbox((rect(0, 0, 10, 10), rect(2, 2, 3, 3))) == rect(0, 0, 10, 10)

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError, match="no boxes"):
            union_bbox(())


class TestWorldPoint:
    """The one local → world transform (ADR-0014) — including its
    fail-fast door: an unknown rotation raises rather than silently
    measuring as 0, matching the model's own placement validation."""

    def test_unknown_rotation_raises(self):
        placement = SimpleNamespace(pos=Point(100, 200), rotation=45, side="F.Cu")
        with pytest.raises(ValueError, match="invalid rotation 45"):
            world_point(Point(10, 20), placement)
