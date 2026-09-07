"""S3 — courtyard parse across the 10 fixture footprints (ADR-0006).

The importer reads ``F.CrtYd`` only: rectangles arrive as 4 closed
axis-aligned ``fp_line``s, circles as one ``fp_circle``. Circles store
integer ``radius_sq`` (the defining ``end`` rides along for export,
ADR-0010); absent courtyard is ``None`` — a valid state the overlap check
skips.
"""

from conftest import ECC83_FP_DIR
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
