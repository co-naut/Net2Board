"""S18/S20 — acceptance pipeline: one table, two faces (ADR-0010, #17).

The highest test seam: the full M1 spine on the ecc83 fixture —
``from_files`` → ``build_board`` → place all 15 from the shared table →
``run_drc == ()`` → byte-exact export. Includes the detect-and-fix beat
(place into an overlap, see exactly one violation, one move, clean) and
the runnable face (``python -m net2board.examples.ecc83`` writes the
board file). Both faces import the single committed placement table;
nothing here re-authors a placement.
"""

import pkgutil
import subprocess
import sys

import net2board.examples
from conftest import assert_byte_golden, load_ecc83_spec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.examples.ecc83_placements import (
    BEAT_REF,
    PLACEMENTS,
    apply_placements,
    beat_overlap_board,
)
from net2board.export import export_pcb

REGEN_HINT = "regenerate deliberately: UPDATE_GOLDENS=1 uv run pytest tests/test_acceptance_ecc83.py"


def final_board():
    """The acceptance board: the table applied to a fresh ecc83 build."""
    return apply_placements(build_board(load_ecc83_spec()))


class TestPlacementTable:
    def test_covers_exactly_the_netlist_refs_in_order(self):
        board = build_board(load_ecc83_spec())
        assert [row.ref for row in PLACEMENTS] == [
            component.ref for component in board.components
        ]


class TestFinalBoard:
    def test_all_placed_and_drc_clean(self):
        board = final_board()
        assert board.unplaced_refs() == ()
        assert len(board.placements) == len(PLACEMENTS)
        assert run_drc(board) == ()


class TestDetectAndFixBeat:
    def test_placing_into_an_overlap_yields_exactly_one_violation(self):
        board = beat_overlap_board(build_board(load_ecc83_spec()))
        assert board.unplaced_refs() == ()
        violations = run_drc(board)
        assert len(violations) == 1
        (violation,) = violations
        assert violation.type is ViolationType.COURTYARDS_OVERLAP
        assert violation.offending_refs == ("placement:R1", "placement:R2")
        assert (
            violation.description == "Courtyards overlap: placement:R1 and placement:R2"
        )

    def test_one_move_clears_it_and_lands_on_the_final_board(self):
        overlapped = beat_overlap_board(build_board(load_ecc83_spec()))
        table_row = next(row for row in PLACEMENTS if row.ref == BEAT_REF)
        fixed = overlapped.with_placement(*table_row)
        assert run_drc(fixed) == ()
        assert fixed == final_board()


class TestExport:
    def test_acceptance_export_golden_byte_exact(self):
        assert_byte_golden(
            "export",
            "ecc83_acceptance.kicad_pcb",
            export_pcb(final_board()),
            REGEN_HINT,
        )


class TestExamplesSurface:
    def test_surface_is_exactly_the_table_and_the_script(self):
        assert sorted(
            module.name for module in pkgutil.iter_modules(net2board.examples.__path__)
        ) == ["ecc83", "ecc83_placements"]


class TestRunnableExample:
    def test_writes_the_board_file_in_one_command(self, tmp_path):
        output = tmp_path / "acceptance.kicad_pcb"
        result = subprocess.run(
            [sys.executable, "-m", "net2board.examples.ecc83", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "1 violation" in result.stdout
        assert output.read_bytes() == export_pcb(final_board()).encode()
