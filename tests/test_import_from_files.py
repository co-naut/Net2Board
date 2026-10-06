"""S3 — BoardSpec.from_files over the committed ecc83 fixture (ADR-0007, #14).

The file-driven path: one call loads netlist + footprint library, strips the
library nickname, parses only the referenced footprints, and yields a
BoardSpec ready for ``build_board`` — with no file-format knowledge leaking
past the call.
"""

from pathlib import Path

from conftest import ECC83_FP_DIR, ECC83_NETLIST, OUTLINE, STACKUP, load_ecc83_spec
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.geometry import Point
from net2board.ir import parse_footprint

COMP_ORDER = (
    "C1",
    "C2",
    "P1",
    "P2",
    "P3",
    "P4",
    "P5",
    "P6",
    "P7",
    "P8",
    "R1",
    "R2",
    "R3",
    "R4",
    "U1",
)
NET_ORDER = (
    "GND",
    "Net-(P1-PM)",
    "Net-(P2-P1)",
    "Net-(P3-P1)",
    "Net-(P4-P1)",
    "Net-(P4-PM)",
    "Net-(U1A-G)",
    "Net-(U1A-K)",
    "Net-(U1B-K)",
    "unconnected-(P5-Pad1)",
    "unconnected-(P6-Pad1)",
    "unconnected-(P7-Pad1)",
    "unconnected-(P8-Pad1)",
)
BARE_ENTRIES = {
    "C1": "CP_Radial_D10.0mm_P5.00mm",
    "C2": "C_Disc_D4.7mm_W2.5mm_P5.00mm",
    "P1": "Altech_AK300_1x02_P5.00mm_45-Degree",
    "P5": "MountingHole_3.2mm_M3_DIN965_Pad",
    "R1": "R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
    "U1": "Valve_ECC-83-1",
}
GND_NODES = (
    ("C1", "2"),
    ("P1", "1"),
    ("P2", "2"),
    ("P3", "2"),
    ("R2", "2"),
    ("R3", "2"),
    ("R4", "2"),
)


class TestFixtureLoad:
    def test_pinned_counts(self):
        spec = load_ecc83_spec()
        assert len(spec.netlist_ir.comps) == 15
        assert len(spec.netlist_ir.nets) == 13
        assert sum(len(net.nodes) for net in spec.netlist_ir.nets) == 33
        assert len(spec.netlist_ir.libparts) == 6

    def test_comps_in_netlist_order_with_bare_entry_names(self):
        comps = load_ecc83_spec().netlist_ir.comps
        assert [comp.ref for comp in comps] == list(COMP_ORDER)
        for ref, entry in BARE_ENTRIES.items():
            assert next(c for c in comps if c.ref == ref).entry_name == entry

    def test_footprint_irs_keyed_by_bare_entry_only_referenced(self):
        spec = load_ecc83_spec()
        assert set(spec.footprint_irs) == set(BARE_ENTRIES.values())
        assert "PinHeader_1x02_P2.54mm_Vertical" not in spec.footprint_irs
        assert "Valve_ECC-83-2" not in spec.footprint_irs
        assert "C_Axial_L12.0mm_D6.5mm_P20.00mm_Horizontal" not in spec.footprint_irs

    def test_nets_in_file_order_gnd_nodes_verbatim(self):
        nets = load_ecc83_spec().netlist_ir.nets
        assert [net.name for net in nets] == list(NET_ORDER)
        assert tuple(nets[0].nodes) == GND_NODES

    def test_libparts_minimal_in_file_order(self):
        libparts = load_ecc83_spec().netlist_ir.libparts
        assert [(lp.lib, lp.part) for lp in libparts] == [
            ("ecc83-pp", "C"),
            ("ecc83-pp", "CONN_1"),
            ("ecc83-pp", "CONN_2"),
            ("ecc83-pp", "CP"),
            ("ecc83-pp", "ECC83"),
            ("ecc83-pp", "R"),
        ]

    def test_outline_and_stackup_pass_through(self):
        spec = load_ecc83_spec()
        assert spec.outline == OUTLINE
        assert spec.stackup == STACKUP

    def test_accepts_plain_string_paths(self):
        spec = BoardSpec.from_files(
            str(ECC83_NETLIST), str(ECC83_FP_DIR), OUTLINE, STACKUP
        )
        assert len(spec.netlist_ir.comps) == 15


class TestJoinToBoard:
    def test_build_board_then_unplaced_refs_lists_all_15_in_order(self):
        board = build_board(load_ecc83_spec())
        assert board.unplaced_refs() == COMP_ORDER

    def test_gnd_joins_seven_pins(self):
        board = build_board(load_ecc83_spec())
        gnd = next(net for net in board.nets if net.name == "GND")
        assert set(gnd.pins) == set(GND_NODES)

    def test_all_nine_valve_pins_bind(self):
        board = build_board(load_ecc83_spec())
        u1_pins = {pin for net in board.nets for pin in net.pins if pin[0] == "U1"}
        assert u1_pins == {("U1", str(n)) for n in range(1, 10)}


class TestPadsCarryExportData:
    def test_resistor_pads_in_library_order(self):
        fp = load_ecc83_spec().footprint_irs[
            "R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"
        ]
        assert [pad.number for pad in fp.pads] == ["1", "2"]
        pad1, pad2 = fp.pads
        assert (
            pad1.shape_enum,
            pad1.local_pos,
            pad1.size,
            pad1.drill,
            pad1.layers,
        ) == (
            "circle",
            Point(0, 0),
            (1_600_000, 1_600_000),
            800_000,
            ("*.Cu", "*.Mask"),
        )
        assert (pad2.shape_enum, pad2.local_pos) == ("oval", Point(7_620_000, 0))

    def test_radial_cap_polarity_pad_is_rect(self):
        fp = load_ecc83_spec().footprint_irs["CP_Radial_D10.0mm_P5.00mm"]
        assert [pad.number for pad in fp.pads] == ["1", "2"]
        assert fp.pads[0].shape_enum == "rect"
        assert fp.pads[0].size == (2_000_000, 2_000_000)
        assert fp.pads[1].shape_enum == "circle"
        assert fp.pads[1].local_pos == Point(5_000_000, 0)

    def test_valve_nine_pads_in_library_order(self):
        fp = load_ecc83_spec().footprint_irs["Valve_ECC-83-1"]
        assert [pad.number for pad in fp.pads] == [str(n) for n in range(1, 10)]
        assert all(pad.drill == 1_020_000 for pad in fp.pads)


class TestInternalNicknameStrip:
    """ADR-0015 — a footprint file may declare `"Lib:Entry"` internally
    (board-embedded extractions do); the parser strips it exactly as
    netlist comp footprints are stripped."""

    def test_prefixed_internal_name_parses_to_bare_entry(self):
        text = (
            '(footprint "SomeLib:R_Axial_DIN0207"\n'
            '\t(layer "F.Cu")\n'
            '\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) '
            '(drill 0.8) (layers "*.Cu" "*.Mask"))\n'
            ")\n"
        )
        assert parse_footprint(text).entry_name == "R_Axial_DIN0207"

    def test_bare_internal_name_unchanged(self):
        text = (
            '(footprint "R_Axial_DIN0207"\n'
            '\t(layer "F.Cu")\n'
            '\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) '
            '(drill 0.8) (layers "*.Cu" "*.Mask"))\n'
            ")\n"
        )
        assert parse_footprint(text).entry_name == "R_Axial_DIN0207"


class TestRuntimePurity:
    def test_loader_reads_only_net_and_kicad_mod_each_once(self, monkeypatch):
        read = []
        real_read_text = Path.read_text

        def spy(path, *args, **kwargs):
            read.append(path.suffix)
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)
        load_ecc83_spec()
        assert read.count(".net") == 1
        assert (
            sorted(suffix for suffix in read if suffix != ".net") == [".kicad_mod"] * 6
        )

    def test_package_never_shells_out_or_parses_schematics(self):
        """The engine never shells out and never reads schematics — no
        oracle call in any runtime path (ADR-0007, README's moat). The
        one exemption is the session harness (``examples/session.py``,
        ADR-0019's example-plus-slow-tier module): its slow face runs the
        ``kicad-cli`` oracle exactly as this suite's slow tier does — a
        harness duty beside the examples, never an engine path — so only
        ``subprocess`` may appear there, and anywhere else any banned
        token fails here."""
        pkg = Path(__file__).resolve().parent.parent / "src" / "net2board"
        harness = pkg / "examples" / "session.py"
        engine_banned = ("subprocess", "os.system", "os.popen", "kicad_sch")
        harness_banned = tuple(t for t in engine_banned if t != "subprocess")
        for src in sorted(pkg.rglob("*.py")):
            banned = harness_banned if src == harness else engine_banned
            text = src.read_text()
            for token in banned:
                assert token not in text, (src, token)
