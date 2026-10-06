"""The public repair derivation — legalize and LegalizationResult (#45, ADR-0012).

"Legal" is defined by reference to ``run_drc``: no Violation of error
severity. The contract under test:

- identity on legal input — the very Board object comes back, empty report;
- overlap, containment, and every constraint finding become legal where
  feasible, within the optional global ``max_displacement`` bound, locked
  placements never moving;
- per-part all-or-nothing: a part that cannot be repaired within the bound
  stays exactly where it was and is named in ``unlegalized_refs`` — the
  returned Board is honestly still illegal;
- deterministic: same ``(board, bound)``, same result, no seeds;
- input errors (a malformed bound) raise under ``Net2BoardError``.

The local world: one-pad TINY parts (±0.5 mm rectangle courtyard), placed
on the shared 40 × 30 mm outline — small enough that every repair in these
tests is a few millimetres, and every bound breach is unambiguous.
"""

from enum import Enum

import pytest

from conftest import load_ecc83_spec, make_spec
from net2board.build import build_board
from net2board.drc import Severity, Violation, run_drc
from net2board.errors import Net2BoardError
from net2board.examples.ecc83_placements import beat_overlap_board
from net2board.geometry import Point, Rectangle, pad_world_center
from net2board.ir import CompIR, FootprintIR, NetlistIR, PadIR
from net2board.model import (
    Board,
    Constraint,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Proximity,
    Region,
    Requirement,
)
from net2board.solver import (
    LegalizationError,
    LegalizationResult,
    legalize,
)


def mm(value: int) -> int:
    return value * 1_000_000


TINY_PAD = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(mm(1), mm(1)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
TINY_YARD = Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000))
TINY = {"TINY": FootprintIR(entry_name="TINY", pads=TINY_PAD, courtyard=TINY_YARD)}


def tiny_board(*refs: str) -> Board:
    """An unplaced Board over the given TINY comps, no nets."""
    return build_board(
        make_spec(
            netlist_ir=NetlistIR(
                comps=tuple(CompIR(ref=ref, entry_name="TINY") for ref in refs),
                nets=(),
            ),
            footprint_irs=TINY,
        )
    )


def placed(board: Board, ref: str, x: int, y: int, locked: bool = False) -> Board:
    return board.with_placement(ref, mm(x), mm(y), 0, "F.Cu", locked)


def overlapping_board():
    """R1 and R2 at the same spot — one courtyards_overlap finding."""
    return placed(placed(tiny_board("R1", "R2"), "R1", 10, 10), "R2", 10, 10)


def error_findings(board):
    return tuple(v for v in run_drc(board) if v.severity is Severity.ERROR)


class TestContract:
    def test_identity_on_legal_input(self):
        board = placed(placed(tiny_board("R1", "R2"), "R1", 10, 10), "R2", 12, 10)
        assert error_findings(board) == ()
        result = legalize(board)
        assert isinstance(result, LegalizationResult)
        assert result.board is board
        assert result.unlegalized_refs == ()
        assert result.displacements == {}

    def test_input_errors_raise_under_net2boarderror(self):
        board = overlapping_board()
        for bad in (-1, True, 1.5, "5mm"):
            with pytest.raises(Net2BoardError) as excinfo:
                legalize(board, max_displacement=bad)
            assert isinstance(excinfo.value, LegalizationError)

    def test_result_is_frozen(self):
        board = overlapping_board()
        result = legalize(board)
        with pytest.raises(AttributeError):
            result.board = board


class TestRepair:
    def test_overlap_becomes_legal(self):
        board = overlapping_board()
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        # the canonical first offender is the mover
        assert set(result.displacements) == {"R1"}
        assert next(iter(result.displacements.values())) > 0

    def test_containment_becomes_legal(self):
        board = placed(tiny_board("R1"), "R1", -1, 10)  # 1.5 mm off the left
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.displacements == {"R1": 1_500_000}

    def test_proximity_becomes_legal(self):
        board = (
            tiny_board("R1", "R2")
            .with_constraint(
                Constraint(
                    id="near",
                    kind=Requirement(),
                    relation=Proximity(
                        ref_a="R1", pad_a="1", ref_b="R2", pad_b="1", bound_nm=mm(5)
                    ),
                )
            )
            .with_placement("R1", mm(10), mm(10), 0, "F.Cu")
            .with_placement("R2", mm(30), mm(10), 0, "F.Cu")
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        distance = (
            pad_world_center(placed_map["R1"], "1").x
            - pad_world_center(placed_map["R2"], "1").x
        )
        assert abs(distance) <= mm(5)
        assert set(result.displacements) == {"R1"}

    def test_region_becomes_legal(self):
        rect = Rectangle(Point(mm(10), mm(10)), Point(mm(20), mm(20)))
        board = (
            tiny_board("R1")
            .with_constraint(
                Constraint(
                    id="in-region",
                    kind=Requirement(),
                    relation=Region(target="placement:R1", rect=rect),
                )
            )
            .with_placement("R1", mm(2), mm(2), 0, "F.Cu")
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()

    def test_edge_mount_becomes_legal(self):
        board = (
            tiny_board("R1")
            .with_constraint(
                Constraint(
                    id="west",
                    kind=Requirement(),
                    relation=EdgeMount(target="placement:R1", edge=Edge.LEFT),
                )
            )
            .with_placement("R1", mm(5), mm(15), 0, "F.Cu")
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        (r1,) = result.board.placements
        assert r1.pos.x == 500_000  # face flush on the left edge line

    def test_keep_together_becomes_legal(self):
        board = (
            tiny_board("R1", "R2")
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(
                Constraint(
                    id="pair-fit",
                    kind=Requirement(),
                    relation=KeepTogether(
                        target="group:pair", max_width_nm=mm(3), max_height_nm=mm(3)
                    ),
                )
            )
            .with_placement("R1", mm(10), mm(10), 0, "F.Cu")
            .with_placement("R2", mm(30), mm(30), 0, "F.Cu")
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()

    def test_cascading_repairs_settle(self):
        # three parts in a row, each overlapping its neighbour: clearing the
        # first pair pushes R1 into clear space, not into R3
        board = placed(
            placed(placed(tiny_board("R1", "R2", "R3"), "R1", 10, 10), "R2", 10, 10),
            "R3",
            10,
            12,
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()

    def test_ecc83_overlap_beat_legalizes(self):
        board = beat_overlap_board(build_board(load_ecc83_spec()))
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        assert set(result.displacements) == {"R1"}


class TestBoundsAndMobility:
    def test_max_displacement_respected_when_feasible(self):
        board = placed(placed(tiny_board("R1", "R2"), "R1", 10, 10), "R2", 10, 10)
        result = legalize(board, max_displacement=mm(5))
        assert error_findings(result.board) == ()
        assert all(0 < d <= mm(5) for d in result.displacements.values())

    def test_infeasible_bound_names_and_restores(self):
        # R2 sits exactly on R1; clearing needs ~1 mm, the bound allows 1 nm
        board = placed(placed(tiny_board("R1", "R2"), "R1", 10, 10), "R2", 10, 10)
        result = legalize(board, max_displacement=1_000)
        assert result.unlegalized_refs == ("R1", "R2")
        # both parts rest exactly where they were: honest per-part failure
        assert result.displacements == {}
        for placement in result.board.placements:
            assert placement.pos in (Point(mm(10), mm(10)),)
        # the Board is honestly still illegal, and the offenders are named
        findings = error_findings(result.board)
        assert findings
        for finding in findings:
            assert all(
                ref.removeprefix("placement:") in result.unlegalized_refs
                for ref in finding.offending_refs
                if ref.startswith("placement:")
            )

    def test_locked_never_moves(self):
        board = placed(
            placed(tiny_board("A", "B"), "A", 10, 10, locked=True), "B", 10, 10
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["A"].pos == Point(mm(10), mm(10))
        assert placed_map["A"].locked is True
        assert set(result.displacements) == {"B"}

    def test_locked_illegal_part_is_a_named_failure(self):
        board = placed(tiny_board("A"), "A", -1, 10, locked=True)
        result = legalize(board)
        (finding,) = error_findings(result.board)
        assert finding.offending_refs == ("placement:A",)
        assert result.unlegalized_refs == ("A",)
        assert result.displacements == {}
        (a,) = result.board.placements
        assert a.pos == Point(mm(-1), mm(10))

    def test_stuck_finding_does_not_strand_the_others(self):
        # A is locked and illegal (containment sorts first); B and C
        # overlap and are trivially repairable — the repair must go on
        # and legalize them even though A can never move
        board = placed(
            placed(
                placed(tiny_board("A", "B", "C"), "A", -1, 10, locked=True), "B", 30, 10
            ),
            "C",
            30,
            10,
        )
        result = legalize(board)
        assert result.unlegalized_refs == ("A",)
        findings = error_findings(result.board)
        assert [f.type.value for f in findings] == ["courtyard_outside_outline"]
        assert set(result.displacements) == {"B"}

    def test_unplaced_refs_stay_unplaced(self):
        board = placed(placed(tiny_board("R1", "R2", "R3"), "R1", 10, 10), "R2", 10, 10)
        result = legalize(board)
        assert "R3" not in {p.ref for p in result.board.placements}
        assert error_findings(result.board) == ()


class TestDeterminism:
    def test_same_input_same_result(self):
        board = placed(
            placed(placed(tiny_board("R1", "R2", "R3"), "R1", 10, 10), "R2", 10, 10),
            "R3",
            10,
            12,
        )
        first = legalize(board)
        second = legalize(board)
        assert first.board == second.board
        assert first.unlegalized_refs == second.unlegalized_refs
        assert first.displacements == second.displacements

    def test_bound_changes_result_not_contract(self):
        board = overlapping_board()
        bounded = legalize(board, max_displacement=mm(1))
        unbounded = legalize(board)
        assert all(d <= mm(1) for d in bounded.displacements.values())
        assert error_findings(unbounded.board) == ()


class TestUnknownFindingType:
    """The repairability table is per finding type; a type with no closed
    form degrades to the honest named failure — the part ends exactly
    where it was and is named (ADR-0012's failure-as-value), never a
    wrong board. The degradation is contract, so it is pinned: a check
    whose ViolationType legalize has never seen must not crash, must not
    silently pass, and must not move the offender."""

    def test_unknown_type_names_the_offender_and_moves_nothing(self, monkeypatch):
        import net2board.drc as drc_module
        import net2board.solver._evaluator as evaluator_module

        mystery = Enum("MysteryType", "MYSTERY_UNMET")

        def mystery_check(board):
            return (
                Violation(
                    type=mystery.MYSTERY_UNMET,
                    severity=Severity.ERROR,
                    description="mystery unmet",
                    offending_refs=("placement:R1",),
                    locations=(Point(mm(10), mm(10)),),
                ),
            )

        monkeypatch.setattr(drc_module, "_describe", lambda *_: "mystery unmet")
        patched = drc_module.CHECKS + (mystery_check,)
        monkeypatch.setattr(drc_module, "CHECKS", patched, raising=False)
        monkeypatch.setattr(evaluator_module, "CHECKS", patched, raising=False)

        board = placed(tiny_board("R1"), "R1", 10, 10)
        result = legalize(board)
        assert result.unlegalized_refs == ("R1",)
        assert result.displacements == {}
        assert result.board.placements[0].pos == Point(mm(10), mm(10))
