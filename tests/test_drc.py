"""S7 — Violation and run_drc: the agent's feedback channel (ADR-0009).

The struct is pinned field-for-field against the oracle's ``courtyards_overlap``
signal; determinism is the contract — same board, same tuple, immune to scan
order — enforced by canonical ref order inside each violation plus an explicit
final sort. Cross-side pairs are exempt and courtyard-less placements are
skipped, matching the KiCad oracle (probed on kicad-cli 10.0.6).
"""

from dataclasses import FrozenInstanceError, asdict

import pytest

from conftest import FOOTPRINT_IRS, fresh_board, make_spec
from net2board import drc
from net2board.build import build_board
from net2board.drc import CHECKS, Severity, ViolationType, run_drc
from net2board.geometry import Point
from net2board.ir import CompIR, FootprintIR, NetlistIR

X = Y = 10_000_000

R1_AT = (10_500_000, 10_500_000)
R2_AT = (10_000_000, 10_000_000)


def overlapped_board():
    return (
        fresh_board()
        .with_placement("R1", *R1_AT, 0, "F.Cu")
        .with_placement("R2", *R2_AT, 0, "F.Cu")
    )


def no_courtyard_board():
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="X1", entry_name="NOCRT"),
                CompIR(ref="X2", entry_name="NOCRT"),
            ),
            nets=(),
        ),
        footprint_irs={
            "NOCRT": FootprintIR(entry_name="NOCRT", pads=(), courtyard=None)
        },
    )
    board = build_board(spec)
    return board.with_placement("X1", X, Y, 0, "F.Cu").with_placement(
        "X2", X, Y, 0, "F.Cu"
    )


def mixed_courtyard_board():
    """One courtyard-less placement stacked on a courtyard-bearing one."""
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="X1", entry_name="NOCRT"),
                CompIR(ref="R1", entry_name="RES"),
            ),
            nets=(),
        ),
        footprint_irs={
            "NOCRT": FootprintIR(entry_name="NOCRT", pads=(), courtyard=None),
            "RES": FOOTPRINT_IRS["RES"],
        },
    )
    board = build_board(spec)
    return board.with_placement("X1", X, Y, 0, "F.Cu").with_placement(
        "R1", X, Y, 0, "F.Cu"
    )


class TestViolationStruct:
    def test_overlap_yields_exactly_one_violation(self):
        (violation,) = run_drc(overlapped_board())
        assert violation.type is ViolationType.COURTYARDS_OVERLAP
        assert violation.severity is Severity.ERROR

    def test_refs_and_locations_name_both_placements(self):
        (violation,) = run_drc(overlapped_board())
        assert violation.offending_refs == ("placement:R1", "placement:R2")
        assert violation.locations == (Point(*R1_AT), Point(*R2_AT))

    def test_locations_pair_positionally_with_canonical_refs(self):
        # refs sort lexicographically; each location follows its own ref —
        # not the locations' own order
        (violation,) = run_drc(overlapped_board())
        assert R1_AT[0] > R2_AT[0]
        assert violation.locations[0] == Point(*R1_AT)

    def test_description_is_pinned(self):
        (violation,) = run_drc(overlapped_board())
        assert violation.description == (
            "Courtyards overlap: placement:R1 and placement:R2"
        )

    def test_violation_is_frozen(self):
        (violation,) = run_drc(overlapped_board())
        with pytest.raises(FrozenInstanceError):
            violation.severity = Severity.WARNING

    def test_asdict_round_trips(self):
        (violation,) = run_drc(overlapped_board())
        assert asdict(violation) == {
            "type": ViolationType.COURTYARDS_OVERLAP,
            "severity": Severity.ERROR,
            "description": "Courtyards overlap: placement:R1 and placement:R2",
            "offending_refs": ("placement:R1", "placement:R2"),
            "locations": (
                {"x": R1_AT[0], "y": R1_AT[1]},
                {"x": R2_AT[0], "y": R2_AT[1]},
            ),
        }

    def test_type_value_collides_with_oracle(self):
        assert ViolationType.COURTYARDS_OVERLAP.value == "courtyards_overlap"

    def test_severity_is_error_or_warning_only(self):
        assert {s.value for s in Severity} == {"error", "warning"}
        assert not hasattr(Severity, "IGNORE")


class TestDeterminism:
    def test_same_board_same_tuple(self):
        board = overlapped_board()
        assert run_drc(board) == run_drc(board)

    def test_immune_to_scan_order(self):
        r1_first = (
            fresh_board()
            .with_placement("R1", *R1_AT, 0, "F.Cu")
            .with_placement("R2", *R2_AT, 0, "F.Cu")
        )
        r2_first = (
            fresh_board()
            .with_placement("R2", *R2_AT, 0, "F.Cu")
            .with_placement("R1", *R1_AT, 0, "F.Cu")
        )
        assert run_drc(r1_first) == run_drc(r2_first)

    def test_multiple_violations_sorted_by_refs(self):
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X, Y, 0, "F.Cu")
            .with_placement("R3", X, Y, 0, "F.Cu")
        )
        violations = run_drc(board)
        assert [v.offending_refs for v in violations] == [
            ("placement:R1", "placement:R2"),
            ("placement:R1", "placement:R3"),
            ("placement:R2", "placement:R3"),
        ]

    def test_moved_board_changes_only_the_real_difference(self):
        board = overlapped_board()
        (violation,) = run_drc(board)
        moved = board.with_placement("R2", 30_000_000, 25_000_000, 0, "F.Cu")
        assert run_drc(moved) == ()
        assert run_drc(board) == (violation,)  # the snapshot is untouched


class TestCheckScope:
    def test_cross_side_pairs_are_exempt(self):
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X, Y, 0, "B.Cu")
        )
        assert run_drc(board) == ()

    def test_no_courtyard_placements_are_skipped_not_flagged(self):
        assert run_drc(no_courtyard_board()) == ()

    def test_mixed_pair_skips_when_either_courtyard_is_absent(self):
        assert run_drc(mixed_courtyard_board()) == ()

    def test_unplaced_board_is_clean(self):
        assert run_drc(fresh_board()) == ()

    def test_clear_board_is_clean(self):
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 30_000_000, 25_000_000, 0, "F.Cu")
            .with_placement("R3", 15_000_000, 25_000_000, 90, "B.Cu")
        )
        assert run_drc(board) == ()


class TestPipeline:
    def test_checks_is_an_ordered_module_level_tuple(self):
        assert isinstance(CHECKS, tuple)
        assert all(callable(check) for check in CHECKS)

    def test_public_surface_is_pinned(self):
        assert set(drc.__all__) == {
            "run_drc",
            "Violation",
            "ViolationType",
            "Severity",
            "CHECKS",
        }
