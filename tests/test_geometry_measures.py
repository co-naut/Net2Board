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

import random
from types import SimpleNamespace

import pytest

from conftest import RES_PADS, fresh_board, make_spec
from net2board.build import build_board
from net2board.drc import _edge_line_gap
from net2board.geometry import (
    Capsule,
    Circle,
    Edge,
    Point,
    Rectangle,
    _apart,
    _boxes_clear,
    _clear_of_outline,
    _grown,
    capsule_gap_nm,
    edge_face_offset_nm,
    measure_box,
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


class TestMeasureBox:
    """The box Region, EdgeMount, and KeepTogether measure (ADR-0011's
    fallback, #42, #65): courtyard AABB first, pad bounding box when
    courtyard-less, ``None`` when there is nothing to measure."""

    def test_rectangle_courtyard_measures_as_its_world_aabb(self):
        # RES courtyard (−1.05M, −1.5M)..(8.67M, 1.5M) at the anchor
        assert measure_box(placed("R1")) == rect(
            8_950_000, 18_500_000, 18_670_000, 21_500_000
        )

    def test_circle_courtyard_resolves_through_the_isqrt_aabb(self):
        # BIGRES: radius² = 10¹³, isqrt = 3 162 277, world center (15M, 20M)
        assert measure_box(placed("R3")) == rect(
            11_837_723, 16_837_723, 18_162_277, 23_162_277
        )

    def test_courtyard_less_footprint_falls_back_to_the_pad_bbox(self):
        # RES pads without a courtyard: the pads bound the box (no courtyard read)
        spec = make_spec(
            netlist_ir=NetlistIR(comps=(CompIR(ref="R1", entry_name="RES"),), nets=()),
            footprint_irs={
                "RES": FootprintIR(entry_name="RES", pads=RES_PADS, courtyard=None)
            },
        )
        board = build_board(spec).with_placement("R1", *ANCHOR, 0, "F.Cu")
        assert measure_box(board.placements[0]) == rect(
            8_550_000, 19_200_000, 18_420_000, 22_450_000
        )

    def test_no_courtyard_and_no_pads_is_nothing_to_measure(self):
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOPAD"),), nets=()
            ),
            footprint_irs={
                "NOPAD": FootprintIR(entry_name="NOPAD", pads=(), courtyard=None)
            },
        )
        board = build_board(spec).with_placement("X1", 0, 0, 0, "F.Cu")
        assert measure_box(board.placements[0]) is None


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
        assert edge_face_offset_nm(box, OUTLINE, Edge.LEFT) == -1_000_000
        assert edge_face_offset_nm(box, OUTLINE, Edge.RIGHT) == -38_000_000
        assert edge_face_offset_nm(box, OUTLINE, Edge.TOP) == -1_000_000
        assert edge_face_offset_nm(box, OUTLINE, Edge.BOTTOM) == -28_000_000

    def test_flush_is_zero(self):
        assert edge_face_offset_nm(rect(0, 1, 2, 3), OUTLINE, Edge.LEFT) == 0
        assert edge_face_offset_nm(rect(2, 1, 40_000_000, 3), OUTLINE, Edge.RIGHT) == 0
        assert edge_face_offset_nm(rect(1, 0, 3, 2), OUTLINE, Edge.TOP) == 0
        assert edge_face_offset_nm(rect(1, 2, 3, 30_000_000), OUTLINE, Edge.BOTTOM) == 0

    def test_past_offsets_are_positive(self):
        assert (
            edge_face_offset_nm(rect(-500_000, 1, 2, 3), OUTLINE, Edge.LEFT) == 500_000
        )
        assert (
            edge_face_offset_nm(rect(1, 2, 40_750_000, 3), OUTLINE, Edge.RIGHT)
            == 750_000
        )
        # y-down: TOP is the minimum-y edge
        assert (
            edge_face_offset_nm(rect(1, -2_000_000, 3, 4), OUTLINE, Edge.TOP)
            == 2_000_000
        )
        assert (
            edge_face_offset_nm(rect(1, 2, 3, 30_050_000), OUTLINE, Edge.BOTTOM)
            == 50_000
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


def _capsule_in_box(rng):
    """A capsule together with the copper box that contains it — the
    relationship the pre-filters lean on (``_anchored_copper``'s box is
    the union of its capsules' ``core ± radius`` extents)."""
    x, y = rng.randrange(0, 500_000), rng.randrange(0, 500_000)
    hw, hh = rng.randrange(0, 50_000), rng.randrange(0, 50_000)
    radius = rng.choice((0, 1_000, 12_345, 100_000))
    core = rect(x - hw, y - hh, x + hw, y + hh)
    capsule = Capsule(core=core, radius=radius)
    box = rect(
        core.min.x - radius,
        core.min.y - radius,
        core.max.x + radius,
        core.max.y + radius,
    )
    return capsule, box


class TestGrownApart:
    """``_apart`` over ``_grown`` boxes never hides a violating pair:
    axis-separated grown boxes force every contained capsule pair's gap
    past the rule — the Evaluator's slot-granularity pre-filter."""

    def test_apart_grown_boxes_mean_no_pair_below_the_rule(self):
        rng = random.Random(20261018)
        checked = 0
        for _ in range(4000):
            cl = rng.choice((5_000, 200_000, 1_000_000))
            capsule_a, box_a = _capsule_in_box(rng)
            capsule_b, box_b = _capsule_in_box(rng)
            if _apart(_grown(box_a, cl), _grown(box_b, cl)):
                assert capsule_gap_nm(capsule_a, capsule_b) >= cl
                checked += 1
        assert checked > 100  # the property was exercised, not vacuous


class TestBoxesClear:
    """``_boxes_clear`` never hides a violating pair: origin boxes
    anchored at their placements, strictly apart by the rule, force every
    contained capsule pair's gap past the rule — the full pass's
    slot-granularity pre-filter."""

    def test_clear_origin_boxes_mean_no_pair_below_the_rule(self):
        rng = random.Random(103)
        checked = 0
        for _ in range(4000):
            cl = rng.choice((5_000, 200_000, 1_000_000))
            capsule_a, box_a = _capsule_in_box(rng)
            capsule_b, box_b = _capsule_in_box(rng)
            dx, dy = (
                rng.randrange(-1_000_000, 1_000_000),
                rng.randrange(-1_000_000, 1_000_000),
            )
            if _boxes_clear(box_a, Point(0, 0), box_b, Point(dx, dy), cl):
                world_b = Capsule(
                    core=rect(
                        capsule_b.core.min.x + dx,
                        capsule_b.core.min.y + dy,
                        capsule_b.core.max.x + dx,
                        capsule_b.core.max.y + dy,
                    ),
                    radius=capsule_b.radius,
                )
                assert capsule_gap_nm(capsule_a, world_b) >= cl
                checked += 1
        assert checked > 100


class TestClearOfOutline:
    """``_clear_of_outline`` never hides an edge finding: a box at least
    the rule inside every outline line forces every contained capsule's
    signed edge gap past the rule — the edge checks' shared pre-filter
    (#98), priced through drc's own ``_edge_line_gap``."""

    def test_box_inside_by_the_rule_means_no_capsule_edge_violation(self):
        rng = random.Random(20261018)
        checked = 0
        for _ in range(4000):
            rule = rng.choice((200_000, 500_000))
            inset = rng.randrange(0, 400_000)
            box = rect(inset, inset, OUTLINE.max.x - inset, OUTLINE.max.y - inset)
            if not _clear_of_outline(box, OUTLINE, rule):
                continue
            capsule, copper_box = _capsule_in_box(rng)
            if not (
                copper_box.min.x >= box.min.x
                and copper_box.max.x <= box.max.x
                and copper_box.min.y >= box.min.y
                and copper_box.max.y <= box.max.y
            ):
                continue
            for edge in Edge:
                assert _edge_line_gap(capsule, OUTLINE, edge) >= rule
            checked += 1
        assert checked > 100
