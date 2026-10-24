"""pad_world_outline — the pad-copper capsule (ADR-0022, #91).

Every pad's copper is an axis-aligned core rectangle grown by a radius,
exact for ``rect``/``circle``/``oval``/``roundrect``, a bounding-rectangle
superset for everything else (the importer hulls ``custom`` pads from
their primitives). Rounding is one-directional — a half-nanometre circle
or oval radius ceils, the roundrect corner radius floors — so the modelled
shape never under-covers the true copper. ``local_rotation`` composes into
the world geometry (local turn innermost, then the placement's
mirror-and-rotate; an axis-aligned box cannot change shape under either).
Pair algebra is exact-integer: the overlap verdict squares, the gap
measure floors through ``isqrt`` (ADR-0005, ADR-0006).

Pad-centre expectations read off ``world_point``: local (1, 2) mm at
placement (10, 5) mm rot 0 → centre (11, 7) mm; rot 90 turns it to
(12, 4) mm.
"""

from decimal import Decimal

from net2board.geometry import (
    Capsule,
    Point,
    Rectangle,
    capsule_gap_nm,
    capsules_overlap,
    pad_world_outline,
)
from net2board.model import Footprint, Pad, Placement

MM = 1_000_000


def pad(shape="rect", size=(1 * MM, 600_000), number="1", rotation=0, **extra):
    return Pad(
        number=number,
        local_pos=Point(1 * MM, 2 * MM),
        size=size,
        drill=None,
        layers=("F.Cu",),
        shape_enum=shape,
        local_rotation=rotation,
        **extra,
    )


def placed(a_pad, x=10 * MM, y=5 * MM, rotation=0, side="F.Cu"):
    footprint = Footprint(entry_name="T", pads=(a_pad,), courtyard=None)
    return Placement(
        ref="T1", footprint=footprint, pos=Point(x, y), rotation=rotation, side=side
    )


def extents(capsule):
    return (
        capsule.core.max.x - capsule.core.min.x,
        capsule.core.max.y - capsule.core.min.y,
    )


class TestRectOutline:
    def test_front_rot_0_grows_around_the_world_center(self):
        capsule = pad_world_outline(placed(pad()), pad())
        assert capsule.core.min == Point(10 * MM + 500_000, 5 * MM + 1_700_000)
        assert capsule.core.max == Point(10 * MM + 1_500_000, 5 * MM + 2_300_000)
        assert capsule.radius == 0

    def test_placement_rotation_90_swaps_extents_and_moves_the_center(self):
        capsule = pad_world_outline(placed(pad(), rotation=90), pad())
        assert capsule.core.min == Point(11 * MM + 700_000, 3 * MM + 500_000)
        assert capsule.core.max == Point(12 * MM + 300_000, 4 * MM + 500_000)

    def test_local_rotation_90_swaps_extents(self):
        capsule = pad_world_outline(placed(pad(rotation=90)), pad(rotation=90))
        assert capsule.core.min == Point(10 * MM + 700_000, 5 * MM + 1_500_000)
        assert capsule.core.max == Point(10 * MM + 1_300_000, 5 * MM + 2_500_000)

    def test_local_rotation_180_swaps_nothing(self):
        capsule = pad_world_outline(placed(pad(rotation=180)), pad(rotation=180))
        assert capsule.core.min == Point(10 * MM + 500_000, 5 * MM + 1_700_000)
        assert capsule.core.max == Point(10 * MM + 1_500_000, 5 * MM + 2_300_000)

    def test_back_side_mirror_cannot_change_an_axis_aligned_box(self):
        front = pad_world_outline(placed(pad()), pad())
        back = pad_world_outline(placed(pad(), side="B.Cu"), pad())
        assert extents(front) == extents(back)
        assert front.radius == back.radius


class TestShapeRadii:
    def test_circle_is_a_point_core_at_half_size(self):
        a_pad = pad("circle", (MM, MM))
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.core.min == capsule.core.max == Point(11 * MM, 7 * MM)
        assert capsule.radius == 500_000

    def test_circle_radius_ceils_a_half_nanometre(self):
        a_pad = pad("circle", (999_999, 999_999))
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.radius == 500_000

    def test_oval_core_is_a_segment_along_the_long_axis(self):
        a_pad = pad("oval", (2 * MM, MM))
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.core.min == Point(11 * MM - 500_000, 7 * MM)
        assert capsule.core.max == Point(11 * MM + 500_000, 7 * MM)
        assert capsule.radius == 500_000

    def test_oval_segment_turns_with_the_placement(self):
        a_pad = pad("oval", (2 * MM, MM))
        capsule = pad_world_outline(placed(a_pad, rotation=90), a_pad)
        assert capsule.core.min == Point(12 * MM, 3 * MM + 500_000)
        assert capsule.core.max == Point(12 * MM, 4 * MM + 500_000)

    def test_oval_radius_ceils_an_odd_short_side(self):
        a_pad = pad("oval", (2 * MM, 999_999))
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.radius == 500_000

    def test_roundrect_shrinks_the_core_by_the_floored_ratio(self):
        a_pad = pad("roundrect", roundrect_rratio=Decimal("0.25"))
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.core.min == Point(11 * MM - 350_000, 7 * MM - 150_000)
        assert capsule.core.max == Point(11 * MM + 350_000, 7 * MM + 150_000)
        assert capsule.radius == 150_000

    def test_custom_hull_is_the_imported_extent(self):
        hull = Rectangle(min=Point(-500_000, -750_000), max=Point(500_000, 750_000))
        a_pad = pad("custom", hull=hull)
        capsule = pad_world_outline(placed(a_pad), a_pad)
        assert capsule.core.min == Point(10 * MM + 500_000, 5 * MM + 1_250_000)
        assert capsule.core.max == Point(10 * MM + 1_500_000, 5 * MM + 2_750_000)
        assert capsule.radius == 0

    def test_hull_turns_local_innermost_then_the_placement(self):
        # local 90 turns the tall hull flat; placement 90 turns it tall
        # again — the composed turn is 180, the hull's own extents back.
        hull = Rectangle(min=Point(-500_000, -750_000), max=Point(500_000, 750_000))
        a_pad = pad("custom", rotation=90, hull=hull)
        capsule = pad_world_outline(placed(a_pad, rotation=90), a_pad)
        assert capsule.core.min == Point(11 * MM + 500_000, 3 * MM + 250_000)
        assert capsule.core.max == Point(12 * MM + 500_000, 4 * MM + 750_000)


class TestCapsulesOverlap:
    def test_touching_capsules_are_tangent_not_overlap(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(
            core=Rectangle(min=Point(2 * MM, 0), max=Point(2 * MM, 0)), radius=MM
        )
        assert capsules_overlap(a, b) is False

    def test_separated_capsules_do_not_overlap(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(
            core=Rectangle(min=Point(3 * MM, 4 * MM), max=Point(3 * MM, 4 * MM)),
            radius=MM,
        )
        assert capsules_overlap(a, b) is False

    def test_interpenetrating_capsules_overlap(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(
            core=Rectangle(min=Point(2 * MM - 1, 0), max=Point(2 * MM - 1, 0)),
            radius=MM,
        )
        assert capsules_overlap(a, b) is True

    def test_crossing_rect_cores_overlap_at_zero_radius(self):
        a = Capsule(
            core=Rectangle(min=Point(0, 0), max=Point(2 * MM, 2 * MM)), radius=0
        )
        b = Capsule(core=Rectangle(min=Point(MM, -MM), max=Point(3 * MM, MM)), radius=0)
        assert capsules_overlap(a, b) is True

    def test_edge_touching_rect_cores_are_tangent(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(MM, MM)), radius=0)
        b = Capsule(core=Rectangle(min=Point(MM, 0), max=Point(2 * MM, MM)), radius=0)
        assert capsules_overlap(a, b) is False

    def test_rect_core_inside_a_fatter_capsule_overlaps(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(10, 10)), radius=0)
        b = Capsule(core=Rectangle(min=Point(20, 0), max=Point(20, 0)), radius=15)
        assert capsules_overlap(a, b) is True


class TestCapsuleGap:
    def test_the_gap_is_core_distance_minus_radii(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(
            core=Rectangle(min=Point(3 * MM, 4 * MM), max=Point(3 * MM, 4 * MM)),
            radius=MM,
        )
        assert capsule_gap_nm(a, b) == 3 * MM

    def test_touching_reads_zero(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(
            core=Rectangle(min=Point(2 * MM, 0), max=Point(2 * MM, 0)), radius=MM
        )
        assert capsule_gap_nm(a, b) == 0

    def test_overlap_clamps_at_zero(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(0, 0)), radius=MM)
        b = Capsule(core=Rectangle(min=Point(1, 0), max=Point(1, 0)), radius=MM)
        assert capsule_gap_nm(a, b) == 0

    def test_rect_gap_is_the_axis_gaps_euclidean_diagonal(self):
        a = Capsule(core=Rectangle(min=Point(0, 0), max=Point(MM, MM)), radius=0)
        b = Capsule(
            core=Rectangle(min=Point(3 * MM, 2 * MM), max=Point(4 * MM, 3 * MM)),
            radius=0,
        )
        # isqrt((2 mm)² + (1 mm)²) — the floored diagonal of the axis gaps.
        assert capsule_gap_nm(a, b) == 2_236_067
