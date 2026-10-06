"""S3 — courtyard parse across the 10 fixture footprints (ADR-0006).

The importer reads ``F.CrtYd`` only: rectangles arrive as 4 closed
axis-aligned ``fp_line``s, circles as one ``fp_circle``. Circles store
integer ``radius_sq`` (the defining ``end`` rides along for export,
ADR-0010); absent courtyard is ``None`` — a valid state the overlap check
skips.
"""

import pytest

from conftest import ECC83_FP_DIR
from net2board.boardspec import UnsupportedCourtyardShape
from net2board.geometry import Circle, Point, Rectangle
from net2board.ir import parse_footprint

RECT_COURTYARDS = {
    "Altech_AK300_1x02_P5.00mm_45-Degree",
    "C_Axial_L12.0mm_D6.5mm_P20.00mm_Horizontal",
    "C_Disc_D4.7mm_W2.5mm_P5.00mm",
    "PinHeader_1x02_P2.54mm_Vertical",
    "R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
}
CIRCLE_COURTYARDS = {
    "CP_Radial_D10.0mm_P5.00mm",
    "CP_Radial_D12.5mm_P7.50mm",
    "MountingHole_3.2mm_M3_DIN965_Pad",
    "Valve_ECC-83-1",
    "Valve_ECC-83-2",
}


def parse(name: str):
    return parse_footprint((ECC83_FP_DIR / f"{name}.kicad_mod").read_text())


class TestShapeKinds:
    def test_five_rectangles_and_five_circles_across_the_library(self):
        files = sorted(ECC83_FP_DIR.glob("*.kicad_mod"))
        assert len(files) == 10
        assert {f.stem for f in files} == RECT_COURTYARDS | CIRCLE_COURTYARDS
        for path in files:
            courtyard = parse_footprint(path.read_text()).courtyard
            expected = Rectangle if path.stem in RECT_COURTYARDS else Circle
            assert isinstance(courtyard, expected), path.name


class TestExactRectangleValues:
    def test_resistor(self):
        assert parse("R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal").courtyard == (
            Rectangle(
                min=Point(-1_050_000, -1_500_000), max=Point(8_670_000, 1_500_000)
            )
        )

    def test_disc_capacitor(self):
        assert parse("C_Disc_D4.7mm_W2.5mm_P5.00mm").courtyard == Rectangle(
            min=Point(-1_050_000, -1_500_000), max=Point(6_050_000, 1_500_000)
        )

    def test_connector(self):
        assert parse("Altech_AK300_1x02_P5.00mm_45-Degree").courtyard == Rectangle(
            min=Point(-2_750_000, -6_250_000), max=Point(7_750_000, 6_750_000)
        )

    def test_axial_capacitor(self):
        assert parse(
            "C_Axial_L12.0mm_D6.5mm_P20.00mm_Horizontal"
        ).courtyard == Rectangle(
            min=Point(-1_050_000, -3_500_000), max=Point(21_050_000, 3_500_000)
        )

    def test_pin_header(self):
        assert parse("PinHeader_1x02_P2.54mm_Vertical").courtyard == Rectangle(
            min=Point(-1_800_000, -1_800_000), max=Point(1_800_000, 4_350_000)
        )


class TestExactCircleValues:
    def test_radial_capacitor(self):
        assert parse("CP_Radial_D10.0mm_P5.00mm").courtyard == Circle(
            center=Point(2_500_000, 0),
            radius_sq=27_562_500_000_000,
            end=Point(7_750_000, 0),
        )

    def test_valve(self):
        assert parse("Valve_ECC-83-1").courtyard == Circle(
            center=Point(0, 50_000),
            radius_sq=112_360_000_000_000,
            end=Point(0, -10_550_000),
        )

    def test_mounting_hole(self):
        assert parse("MountingHole_3.2mm_M3_DIN965_Pad").courtyard == Circle(
            center=Point(0, 0),
            radius_sq=9_302_500_000_000,
            end=Point(3_050_000, 0),
        )

    def test_radius_sq_stays_exact_integer(self):
        for name in CIRCLE_COURTYARDS:
            assert isinstance(parse(name).courtyard.radius_sq, int), name


class TestAbsentCourtyard:
    def test_no_crtyd_graphics_yields_none(self):
        text = (
            '(footprint "BARE"\n'
            '\t(layer "F.Cu")\n'
            "\t(attr through_hole)\n"
            '\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) '
            '(layers "*.Cu" "*.Mask"))\n'
            ")\n"
        )
        assert parse_footprint(text).courtyard is None


class TestPolylineHull:
    """ADR-0015 — real libraries draw notched pin-1 courtyards (12–20
    axis-aligned ``fp_line``s); the parse reduces them to their bounding
    rectangle, a conservative superset of the upstream keep-out."""

    def _line(self, x1, y1, x2, y2):
        return (
            f"\t(fp_line (start {x1} {y1}) (end {x2} {y2}) "
            '(stroke (width 0.01) (type solid)) (layer "F.CrtYd"))\n'
        )

    def _footprint(self, *lines):
        return '(footprint "NOTCHED"\n\t(layer "F.Cu")\n' + "".join(lines) + ")\n"

    def test_notched_pin1_courtyard_reduces_to_bounding_rectangle(self):
        text = self._footprint(
            self._line(-2.05, -1.7, 2.05, -1.7),
            self._line(-2.05, 1.7, -2.05, -1.7),
            self._line(2.05, -1.7, 2.05, -0.39),
            self._line(2.05, 0.39, 2.05, 1.7),
            self._line(-2.05, 1.7, 2.05, 1.7),
            self._line(1.05, -1.7, 1.05, -1.5),
            self._line(1.05, -1.5, 2.05, -1.5),
        )
        assert parse_footprint(text).courtyard == Rectangle(
            min=Point(-2_050_000, -1_700_000), max=Point(2_050_000, 1_700_000)
        )

    def test_four_closed_lines_still_yield_the_same_rectangle(self):
        text = self._footprint(
            self._line(0, 0, 1, 0),
            self._line(1, 0, 1, 2),
            self._line(1, 2, 0, 2),
            self._line(0, 2, 0, 0),
        )
        assert parse_footprint(text).courtyard == Rectangle(
            min=Point(0, 0), max=Point(1_000_000, 2_000_000)
        )

    def test_diagonal_segment_still_raises(self):
        text = self._footprint(
            self._line(0, 0, 1, 0),
            self._line(1, 0, 1.5, 0.5),
            self._line(1.5, 0.5, 1, 1),
            self._line(1, 1, 0, 1),
            self._line(0, 1, 0, 0),
        )
        with pytest.raises(UnsupportedCourtyardShape, match="NOTCHED"):
            parse_footprint(text)

    def test_circle_among_lines_still_raises(self):
        circle = (
            "\t(fp_circle (center 0 0) (end 0.5 0) "
            '(stroke (width 0.01) (type solid)) (layer "F.CrtYd"))\n'
        )
        text = self._footprint(
            self._line(0, 0, 1, 0),
            self._line(1, 0, 1, 1),
            self._line(1, 1, 0, 1),
            self._line(0, 1, 0, 0),
            circle,
        )
        with pytest.raises(UnsupportedCourtyardShape, match="NOTCHED"):
            parse_footprint(text)

    def test_three_lines_still_raise(self):
        text = self._footprint(
            self._line(0, 0, 1, 0),
            self._line(1, 0, 1, 1),
            self._line(1, 1, 0, 1),
        )
        with pytest.raises(UnsupportedCourtyardShape, match="NOTCHED"):
            parse_footprint(text)
