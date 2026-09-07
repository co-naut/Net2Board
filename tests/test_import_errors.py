"""S3 — loader failure surface: structured, fail-fast, carrying the key (ADR-0008).

Every failure mode fires at load time with the offending ref / entry name
rideable by the caller. Fixtures are minimal inline ``.net`` / ``.kicad_mod``
texts written to ``tmp_path`` — each test pins exactly one trigger.
"""

import pytest

from conftest import OUTLINE, STACKUP
from net2board.boardspec import (
    BoardSpec,
    BoardSpecError,
    FootprintNotFound,
    FootprintResolveError,
    UnsupportedCourtyardShape,
)

PAD = (
    '(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) '
    '(layers "*.Cu" "*.Mask"))'
)


def netlist_text(footprint_clause: str = '(footprint "Lib:ENTRY")') -> str:
    return (
        '(export (version "E")\n'
        "\t(components\n"
        f'\t\t(comp (ref "R1") (value "V") {footprint_clause})\n'
        "\t)\n"
        "\t(libraries)\n"
        "\t(nets\n"
        '\t\t(net (code "1") (name "GND") (node (ref "R1") (pin "1")))\n'
        "\t)\n"
        ")\n"
    )


def rect_courtyard_lines(x1: float, y1: float, x2: float, y2: float) -> str:
    segments = (
        ((x1, y1), (x1, y2)),
        ((x1, y2), (x2, y2)),
        ((x2, y2), (x2, y1)),
        ((x2, y1), (x1, y1)),
    )
    return "\n".join(
        "\t(fp_line "
        f"(start {sx} {sy}) (end {ex} {ey}) "
        '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        for (sx, sy), (ex, ey) in segments
    )


def footprint_text(
    name: str = "ENTRY",
    crtyd: str | None = None,
    pad: str = PAD,
) -> str:
    lines = [f'(footprint "{name}"', '\t(layer "F.Cu")', "\t(attr through_hole)"]
    if crtyd is not None:
        lines.append(crtyd)
    if pad is not None:
        lines.append(f"\t{pad}")
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_library(tmp_path, footprint: str, *, name: str = "ENTRY"):
    lib_dir = tmp_path / "footprints.pretty"
    lib_dir.mkdir(exist_ok=True)
    (lib_dir / f"{name}.kicad_mod").write_text(footprint)
    return lib_dir


def load(tmp_path, netlist: str, lib_dir) -> BoardSpec:
    netlist_path = tmp_path / "ecc83-pp.net"
    netlist_path.write_text(netlist)
    return BoardSpec.from_files(netlist_path, lib_dir, OUTLINE, STACKUP)


class TestHierarchy:
    @pytest.mark.parametrize(
        "exc", [FootprintNotFound, FootprintResolveError, UnsupportedCourtyardShape]
    )
    def test_loader_errors_are_boardspec_errors(self, exc):
        assert issubclass(exc, BoardSpecError)


class TestFootprintResolveError:
    def test_comp_without_footprint_token(self, tmp_path):
        lib_dir = write_library(tmp_path, footprint_text())
        with pytest.raises(FootprintResolveError) as excinfo:
            load(tmp_path, netlist_text(footprint_clause=""), lib_dir)
        assert excinfo.value.ref == "R1"
        assert "R1" in str(excinfo.value)

    def test_declared_name_mismatches_entry_name(self, tmp_path):
        lib_dir = write_library(tmp_path, footprint_text(name="SOMEHOW_DIFFERENT"))
        with pytest.raises(FootprintResolveError) as excinfo:
            load(tmp_path, netlist_text(), lib_dir)
        assert excinfo.value.ref == "R1"
        assert excinfo.value.entry_name == "ENTRY"
        assert "R1" in str(excinfo.value)
        assert "ENTRY" in str(excinfo.value)

    def test_missing_footprint_token_carries_no_entry_name(self, tmp_path):
        lib_dir = write_library(tmp_path, footprint_text())
        with pytest.raises(FootprintResolveError) as excinfo:
            load(tmp_path, netlist_text(footprint_clause=""), lib_dir)
        assert excinfo.value.entry_name is None


class TestFootprintNotFound:
    def test_entry_file_absent_from_library_dir(self, tmp_path):
        lib_dir = write_library(tmp_path, footprint_text())
        (lib_dir / "ENTRY.kicad_mod").unlink()
        with pytest.raises(FootprintNotFound) as excinfo:
            load(tmp_path, netlist_text(), lib_dir)
        assert excinfo.value.ref == "R1"
        assert excinfo.value.entry_name == "ENTRY"
        assert "ENTRY" in str(excinfo.value)


class TestUnsupportedCourtyardShape:
    def test_fp_poly(self, tmp_path):
        poly = (
            "\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 0 1)) "
            '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(crtyd=poly))
        with pytest.raises(UnsupportedCourtyardShape) as excinfo:
            load(tmp_path, netlist_text(), lib_dir)
        assert excinfo.value.entry_name == "ENTRY"
        assert "ENTRY" in str(excinfo.value)

    def test_fp_arc(self, tmp_path):
        arc = (
            "\t(fp_arc (start -1 0) (mid 0 1) (end 1 0) "
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(crtyd=arc))
        with pytest.raises(UnsupportedCourtyardShape):
            load(tmp_path, netlist_text(), lib_dir)

    def test_diagonal_fp_line(self, tmp_path):
        diagonal = (
            "\t(fp_line (start -1 -1) (end 1 1) "
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(crtyd=diagonal))
        with pytest.raises(UnsupportedCourtyardShape):
            load(tmp_path, netlist_text(), lib_dir)

    def test_two_lines_do_not_close_a_rectangle(self, tmp_path):
        two_lines = (
            "\t(fp_line (start -1 -1) (end -1 1) "
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))\n'
            "\t(fp_line (start -1 1) (end 1 1) "
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(crtyd=two_lines))
        with pytest.raises(UnsupportedCourtyardShape):
            load(tmp_path, netlist_text(), lib_dir)

    def test_circle_mixed_with_lines(self, tmp_path):
        mixed = (
            "\t(fp_circle (center 0 0) (end 1 0) "
            '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
            "\t(fp_line (start -1 -1) (end -1 1) "
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(crtyd=mixed))
        with pytest.raises(UnsupportedCourtyardShape):
            load(tmp_path, netlist_text(), lib_dir)


class TestMalformedInput:
    def test_unclosed_paren_raises_value_error_not_boardspec_error(self, tmp_path):
        lib_dir = write_library(tmp_path, footprint_text())
        with pytest.raises(ValueError, match="ecc83-pp.net") as excinfo:
            load(tmp_path, '(export (version "E")\n', lib_dir)
        assert not isinstance(excinfo.value, BoardSpecError)

    def test_non_integral_nanometre_coordinate_raises(self, tmp_path):
        pad = (
            '(pad "1" thru_hole circle (at 0.0000005 0) (size 1.6 1.6) '
            '(drill 0.8) (layers "*.Cu" "*.Mask"))'
        )
        lib_dir = write_library(tmp_path, footprint_text(pad=pad))
        with pytest.raises(ValueError, match="0.0000005"):
            load(tmp_path, netlist_text(), lib_dir)
