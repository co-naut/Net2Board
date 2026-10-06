"""#30 — the tiny_tapeout hard-placement fixture through the import path.

150 components / 114 nets / 4 copper layers / 470 SMD + 138 through-hole
pads: the first fixture where placement is genuinely hard (69% courtyard
fill, 9,591 unordered courtyard pairs). Every committed footprint file is
byte-verbatim upstream (see the fixture README); the notched pin-1
courtyards of modern KiCad libraries import through the bounding-hull rule
(ADR-0015).
"""

from conftest import TINY_FP_DIR, TINY_OUTLINE, TINY_STACKUP, load_tiny_spec
from net2board.build import build_board
from net2board.geometry import Circle, Point, Rectangle
from net2board.ir import parse_footprint

DISTINCT_ENTRIES = 33


class TestFixtureLoad:
    def test_pinned_counts(self):
        spec = load_tiny_spec()
        assert len(spec.netlist_ir.comps) == 150
        assert len(spec.netlist_ir.nets) == 114

    def test_distinct_footprint_entries_all_resolved(self):
        spec = load_tiny_spec()
        assert len(spec.footprint_irs) == DISTINCT_ENTRIES
        assert len(list(TINY_FP_DIR.glob("*.kicad_mod"))) == DISTINCT_ENTRIES

    def test_outline_and_four_layer_stackup_pass_through(self):
        spec = load_tiny_spec()
        assert spec.outline == TINY_OUTLINE
        assert spec.stackup == TINY_STACKUP


class TestJoinToBoard:
    def test_build_board_leaves_all_150_unplaced(self):
        board = build_board(load_tiny_spec())
        assert len(board.components) == 150
        assert len(board.unplaced_refs()) == 150

    def test_smd_majority_with_through_hole_remainder(self):
        board = build_board(load_tiny_spec())
        pads = [pad for comp in board.components for pad in comp.footprint.pads]
        assert sum(1 for pad in pads if pad.drill is None) == 470
        assert sum(1 for pad in pads if pad.drill is not None) == 138

    def test_courtyard_mix(self):
        board = build_board(load_tiny_spec())
        courtyards = [comp.footprint.courtyard for comp in board.components]
        assert sum(1 for c in courtyards if isinstance(c, Rectangle)) == 139
        assert sum(1 for c in courtyards if isinstance(c, Circle)) == 8
        assert sum(1 for c in courtyards if c is None) == 3


class TestHulledRealLibraryCourtyards:
    """Upstream notched courtyards reduce to their exact bounding rectangle."""

    def test_sot_23_5_notched_courtyard_hull(self):
        fp = parse_footprint((TINY_FP_DIR / "SOT-23-5.kicad_mod").read_text())
        assert fp.courtyard == Rectangle(
            min=Point(-2_050_000, -1_700_000), max=Point(2_050_000, 1_700_000)
        )

    def test_tt04_breakout_monster_hull(self):
        fp = parse_footprint((TINY_FP_DIR / "TT04_BREAKOUT_SMB.kicad_mod").read_text())
        assert fp.courtyard == Rectangle(
            min=Point(-23_900_000, -25_900_000), max=Point(23_900_000, 25_900_000)
        )

    def test_board_extracted_soic_8_resolves_despite_nickname(self):
        """The extracted file declares `Package_SO:SOIC-8_…` internally."""
        fp = parse_footprint(
            (TINY_FP_DIR / "SOIC-8_5.23x5.23mm_P1.27mm.kicad_mod").read_text()
        )
        assert fp.entry_name == "SOIC-8_5.23x5.23mm_P1.27mm"
        assert fp.courtyard == Rectangle(
            min=Point(-4_650_000, -2_860_000), max=Point(4_650_000, 2_860_000)
        )
