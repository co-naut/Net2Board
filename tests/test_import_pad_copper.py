"""Pad-copper import — the type gate, the ratio, net-tie groups, hulls (#91).

The importer begins reading what ADR-0022's checker needs: the pad type
token (``smd``/``thru_hole`` proceed; ``np_thru_hole``/``connect`` raise —
today's exporter would silently write an NPTH pad as ``thru_hole``, and
the clearance check must not price copper that will not exist), the
``roundrect_rratio`` verbatim (the capsule's floored corner radius reads
it), ``net_tie_pad_groups`` (KiCad's format: a space-separated list of
quoted strings, each a comma-separated pad-number list — pads in one group
never violate each other), and ``custom`` pads' ``(primitives …)``
reduced to a pad-local bounding rectangle of the full copper extent —
never under-covering (stroke widths grow, non-integer radii ceil;
unparseable primitive kinds raise at load, the ADR-0015 posture).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from conftest import TINY_FP_DIR
from net2board.boardspec import (
    UnsupportedPadPrimitive,
    UnsupportedPadType,
)
from net2board.geometry import Point, Rectangle
from net2board.ir import parse_footprint

SOLDER_JUMPER = TINY_FP_DIR / "SolderJumper-2_Bridged_Throughole1.kicad_mod"
NET_TIE = TINY_FP_DIR / "NetTie-2_SMD_Pad0.5mm.kicad_mod"
C_0402 = TINY_FP_DIR / "C_0402_1005Metric.kicad_mod"


def parse(path: Path):
    return parse_footprint(path.read_text())


def pad_of(footprint_ir, number, shape=None):
    pads = [pad for pad in footprint_ir.pads if pad.number == number]
    if shape is not None:
        pads = [pad for pad in pads if pad.shape_enum == shape]
    assert len(pads) == 1, f"expected one pad {number!r}/{shape!r}, got {pads}"
    return pads[0]


class TestCustomHull:
    def test_the_jumper_pad_hulls_its_primitives_not_its_anchor(self):
        # Two r=0.5 mm circles at (0, ±0.25) plus an x∈[0, 0.5] strip —
        # the real copper reaches ±0.75 mm where the 1×0.5 anchor
        # reaches ±0.25 (ADR-0022's under-coverage example).
        pad = pad_of(parse(SOLDER_JUMPER), "1", shape="custom")
        assert pad.hull == Rectangle(
            min=Point(-500_000, -750_000), max=Point(500_000, 750_000)
        )

    def test_a_stroked_line_grows_by_half_its_width(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_line (start -1 0) (end 1 0) (width 0.2) (fill no))\n"
            "    )\n"
            "  )"
        )
        pad = parse_footprint(text).pads[0]
        # the stroke reaches half its width past the endpoints too — round
        # caps; the hull never under-covers either axis
        assert pad.hull == Rectangle(
            min=Point(-1_100_000, -100_000), max=Point(1_100_000, 100_000)
        )

    def test_a_stroked_poly_grows_by_half_its_width(self):
        # the outline stroke is copper too — filled or not, it reaches
        # half its width past the vertex box; the hull never under-covers
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_poly (pts (xy -1 0) (xy 0 1) (xy 1 0)) (width 0.2)"
            " (fill no))\n"
            "    )\n"
            "  )"
        )
        pad = parse_footprint(text).pads[0]
        assert pad.hull == Rectangle(
            min=Point(-1_100_000, -100_000), max=Point(1_100_000, 1_100_000)
        )

    def test_a_stroked_rect_grows_by_half_its_width(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_rect (start -1 -1) (end 1 1) (width 0.1) (fill no))\n"
            "    )\n"
            "  )"
        )
        pad = parse_footprint(text).pads[0]
        assert pad.hull == Rectangle(
            min=Point(-1_050_000, -1_050_000), max=Point(1_050_000, 1_050_000)
        )

    def test_a_stroked_circle_grows_by_half_its_width(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_circle (center 0 0) (end 1 0) (width 0.2) (fill no))\n"
            "    )\n"
            "  )"
        )
        pad = parse_footprint(text).pads[0]
        assert pad.hull == Rectangle(
            min=Point(-1_100_000, -1_100_000), max=Point(1_100_000, 1_100_000)
        )

    def test_a_non_integer_circle_radius_ceils_into_the_hull(self):
        # an on-grid defining point at (1, 1) nm — radius √2 nm, hull ±2
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_circle (center 0 0) (end 0.000001 0.000001) (width 0))\n"
            "    )\n"
            "  )"
        )
        pad = parse_footprint(text).pads[0]
        assert pad.hull == Rectangle(min=Point(-2, -2), max=Point(2, 2))

    def test_an_unparseable_primitive_raises_at_load(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")\n'
            "    (primitives\n"
            "      (gr_arc (start 0 0) (mid 0.5 0.5) (end 1 0) (width 0))\n"
            "    )\n"
            "  )"
        )
        with pytest.raises(UnsupportedPadPrimitive):
            parse_footprint(text)


class TestPadTypeGate:
    def test_smd_and_thru_hole_proceed(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            '  (pad "2" thru_hole circle (at 2 0) (size 2 2) (drill 1) '
            '(layers "*.Cu"))\n'
        )
        assert [pad.shape_enum for pad in parse_footprint(text).pads] == [
            "rect",
            "circle",
        ]

    def test_np_thru_hole_raises(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" np_thru_hole circle (at 0 0) (size 3 3) (drill 3) '
            '(layers "*.Cu" "*.Mask"))\n'
        )
        with pytest.raises(UnsupportedPadType):
            parse_footprint(text)

    def test_connect_raises(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "" connect rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        )
        with pytest.raises(UnsupportedPadType):
            parse_footprint(text)


class TestRoundrectRatio:
    def test_the_ratio_is_imported_verbatim(self):
        pad = pad_of(parse(C_0402), "1")
        assert pad.roundrect_rratio == Decimal("0.25")

    def test_a_missing_ratio_stays_none_the_capsule_defaults_it(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd roundrect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        )
        assert parse_footprint(text).pads[0].roundrect_rratio is None

    def test_non_roundrect_pads_carry_no_ratio(self):
        assert pad_of(parse(C_0402), "1").shape_enum == "roundrect"
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        )
        assert parse_footprint(text).pads[0].roundrect_rratio is None


class TestNetTiePadGroups:
    def test_the_jumper_and_the_net_tie_carry_one_group(self):
        for path in (SOLDER_JUMPER, NET_TIE):
            assert parse(path).net_tie_groups == (frozenset({"1", "2"}),)

    def test_groups_are_separate_quoted_comma_lists(self):
        # KiCad's format: a space-separated list of quoted strings, each a
        # comma-separated pad list (the s-expression format reference).
        text = FOOTPRINT_TMPL.format(pads=NET_TIE_GROUPS_TMPL)
        assert parse_footprint(text).net_tie_groups == (
            frozenset({"1", "2"}),
            frozenset({"A", "B"}),
        )

    def test_a_footprint_without_the_token_reads_empty(self):
        text = FOOTPRINT_TMPL.format(
            pads='  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        )
        assert parse_footprint(text).net_tie_groups == ()


FOOTPRINT_TMPL = (
    '  (footprint "T" (layer "F.Cu")\n'
    "{pads}"
    '  (fp_text reference "REF" (at 0 0) (layer "F.SilkS"))\n'
    "  )\n"
)

NET_TIE_GROUPS_TMPL = (
    '  (net_tie_pad_groups "1, 2" "A,B")\n'
    '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
    '  (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu"))\n'
    '  (pad "A" smd rect (at 4 0) (size 1 1) (layers "F.Cu"))\n'
    '  (pad "B" smd rect (at 6 0) (size 1 1) (layers "F.Cu"))\n'
)
