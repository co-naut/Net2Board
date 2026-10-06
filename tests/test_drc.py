"""S7 — Violation and run_drc: the agent's feedback channel (ADR-0009).

The struct is pinned field-for-field against the oracle's ``courtyards_overlap``
signal; determinism is the contract — same board, same tuple, immune to scan
order — enforced by canonical ref order inside each violation plus an explicit
final sort. Cross-side pairs are exempt and courtyard-less placements are
skipped, matching the KiCad oracle (probed on kicad-cli 10.0.6).

The M2 containment check (``courtyard_outside_outline``, ADR-0018) has no
oracle analogue: closed containment, courtyard-less placements skipped, and
the exemption is per stated edge, Requirement-kind only. The locality
companion protocol (ADR-0016) is validated here against the containment
check, its first declared companion.
"""

from dataclasses import FrozenInstanceError, asdict

import pytest

from conftest import FOOTPRINT_IRS, fresh_board, make_spec
from net2board import drc
from net2board.build import build_board
from net2board.drc import CHECKS, Severity, ViolationType, run_drc
from net2board.geometry import Point
from net2board.ir import CompIR, FootprintIR, NetlistIR
from net2board.model import Constraint, Edge, EdgeMount, Group, Preference, Requirement

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


def left_overhang_board():
    """R1's rectangle courtyard pokes 50 µm past the left edge only."""
    return fresh_board().with_placement("R1", 1_000_000, 10_000_000, 0, "F.Cu")


def corner_overhang_board():
    """R1's courtyard pokes past two edges — left and top (a P1 corner)."""
    return fresh_board().with_placement("R1", 1_000_000, 1_000_000, 0, "F.Cu")


def circle_overhang_board():
    """R3's circle courtyard (r = √10 mm) crosses the left edge line only."""
    return fresh_board().with_placement("R3", -2_000_000, 15_000_000, 0, "F.Cu")


REQUIREMENT = Requirement()


def mount(ref: str, edge: Edge, kind=REQUIREMENT, bound=None, id: str | None = None):
    return Constraint(
        id=id or f"{ref}-{edge.value}",
        kind=kind,
        relation=EdgeMount(target=f"placement:{ref}", edge=edge, max_overhang_nm=bound),
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
        # R3 sits at y=20M, not 25M: rotated onto B.Cu its circle courtyard
        # would otherwise poke ~3.16 mm past the bottom edge — a real
        # containment finding since the M2 check (ADR-0018)
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 30_000_000, 25_000_000, 0, "F.Cu")
            .with_placement("R3", 15_000_000, 20_000_000, 90, "B.Cu")
        )
        assert run_drc(board) == ()


class TestCourtyardOutsideOutline:
    def test_overhang_yields_exactly_one_error(self):
        (violation,) = run_drc(left_overhang_board())
        assert violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        assert violation.severity is Severity.ERROR

    def test_finding_names_the_placement_anchor_at_its_position(self):
        (violation,) = run_drc(left_overhang_board())
        assert violation.offending_refs == ("placement:R1",)
        assert violation.locations == (Point(1_000_000, 10_000_000),)

    def test_description_is_pinned(self):
        (violation,) = run_drc(left_overhang_board())
        assert violation.description == (
            "Courtyard outside board outline: placement:R1"
        )

    def test_type_value_is_snake_case_with_no_oracle_collision(self):
        assert (
            ViolationType.COURTYARD_OUTSIDE_OUTLINE.value == "courtyard_outside_outline"
        )

    def test_tangent_to_the_edge_is_contained(self):
        # Closed containment: courtyard min.x = 0 exactly, C2-style (ADR-0018)
        board = fresh_board().with_placement("R1", 1_050_000, 10_000_000, 0, "F.Cu")
        assert run_drc(board) == ()

    def test_fully_interior_is_clean(self):
        board = fresh_board().with_placement("R1", 2_000_000, 10_000_000, 0, "F.Cu")
        assert run_drc(board) == ()

    def test_courtyard_less_off_board_placement_is_skipped_not_flagged(self):
        board = no_courtyard_board().with_placement("X1", 0, 0, 0, "F.Cu")
        assert run_drc(board) == ()

    def test_circle_courtyard_crossing_an_edge_is_reported(self):
        (violation,) = run_drc(circle_overhang_board())
        assert violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        assert violation.offending_refs == ("placement:R3",)

    def test_circle_mounts_on_the_edge_it_crosses(self):
        board = circle_overhang_board().with_constraint(
            mount("R3", Edge.LEFT, bound=1_000_000)
        )
        assert run_drc(board) == ()

    def test_circle_mounted_on_a_different_edge_is_still_reported(self):
        # the RIGHT mount leaves LEFT unstated: containment is reported —
        # and the RIGHT mount itself is unmet (the face sits 33.84 mm short
        # of the right edge), the check #42 added
        board = circle_overhang_board().with_constraint(
            mount("R3", Edge.RIGHT, bound=6_750_000, id="R3-right")
        )
        violations = run_drc(board)
        assert [v.type for v in violations] == [
            ViolationType.COURTYARD_OUTSIDE_OUTLINE,
            ViolationType.EDGE_MOUNT_UNMET,
        ]

    def test_requirement_mount_exempts_its_stated_edge(self):
        board = left_overhang_board().with_constraint(
            mount("R1", Edge.LEFT, bound=550_000)
        )
        assert run_drc(board) == ()

    def test_exemption_is_scoped_to_the_stated_edge(self):
        # ADR-0018's P3 case: exempt on left, still checked on top. The
        # bound covers the measured 50 µm overhang (#42's check), so the
        # finding is the unstated top edge's alone
        board = corner_overhang_board().with_constraint(
            mount("R1", Edge.LEFT, bound=550_000)
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        assert violation.offending_refs == ("placement:R1",)

    def test_stacking_one_mount_per_edge_exempts_the_corner(self):
        board = (
            corner_overhang_board()
            .with_constraint(mount("R1", Edge.LEFT, bound=550_000, id="R1-left"))
            .with_constraint(mount("R1", Edge.TOP, bound=550_000, id="R1-top"))
        )
        assert run_drc(board) == ()

    def test_exempt_edge_is_silent_regardless_of_the_bound(self):
        # A 1 nm bound on a 50_000 nm overhang: containment stays silent on
        # the stated edge, and the over-bound overhang is EDGE_MOUNT_UNMET's
        # to report — one geometry, one finding (#42)
        board = left_overhang_board().with_constraint(mount("R1", Edge.LEFT, bound=1))
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.EDGE_MOUNT_UNMET

    def test_preference_mount_exempts_nothing(self):
        board = left_overhang_board().with_constraint(
            mount("R1", Edge.LEFT, kind=Preference(), id="soft")
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE

    def test_group_targeted_mount_expands_to_its_members(self):
        # Anchors expand at check time (ADR-0011) — a group mount exempts
        # each member's stated edge; the bound covers the measured overhang
        board = (
            left_overhang_board()
            .with_group(Group(id="edge", refs=("R1",)))
            .with_constraint(
                Constraint(
                    id="edge-left",
                    kind=Requirement(),
                    relation=EdgeMount(
                        target="group:edge",
                        edge=Edge.LEFT,
                        max_overhang_nm=550_000,
                    ),
                )
            )
        )
        assert run_drc(board) == ()

    def test_multiple_overhangs_sort_by_ref_regardless_of_placement_order(self):
        r2_first = (
            fresh_board()
            .with_placement("R2", 1_000_000, 20_000_000, 0, "F.Cu")
            .with_placement("R1", 1_000_000, 5_000_000, 0, "F.Cu")
        )
        r1_first = (
            fresh_board()
            .with_placement("R1", 1_000_000, 5_000_000, 0, "F.Cu")
            .with_placement("R2", 1_000_000, 20_000_000, 0, "F.Cu")
        )
        assert run_drc(r1_first) == run_drc(r2_first)
        assert [v.offending_refs for v in run_drc(r1_first)] == [
            ("placement:R1",),
            ("placement:R2",),
        ]

    def test_coexists_with_the_overlap_check_in_the_sorted_tuple(self):
        board = left_overhang_board().with_placement(
            "R2", 1_000_000, 10_000_000, 0, "F.Cu"
        )
        assert [v.type.value for v in run_drc(board)] == [
            "courtyard_outside_outline",
            "courtyard_outside_outline",
            "courtyards_overlap",
        ]


class TestLocalityCompanion:
    """The ADR-0016 protocol, validated by its first check (ADR-0018)."""

    def test_companions_declare_registered_checks_only(self):
        assert set(drc.COMPANIONS) <= set(CHECKS)

    def test_the_containment_check_was_the_protocols_first_validator(self):
        # ADR-0018's check validated the protocol alone; #42's four
        # constraint checks declare companions of their own (five of six
        # checks — only the all-pairs overlap scan has none)
        declaring = [check for check in CHECKS if check in drc.COMPANIONS]
        assert len(declaring) == 5
        assert declaring[0] is CHECKS[1]  # containment: first to declare
        (violation,) = declaring[0](left_overhang_board())
        assert violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE

    def test_companion_returns_exactly_the_moved_refs_findings(self):
        # The single-moved-placement case: R1 hangs off the left edge, R2
        # sits interior — the affected set is R1's finding and nothing else
        board = left_overhang_board().with_placement(
            "R2", 20_000_000, 20_000_000, 0, "F.Cu"
        )
        check, companion = next(iter(drc.COMPANIONS.items()))
        expected = tuple(
            violation
            for violation in check(board)
            if "placement:R1" in violation.offending_refs
        )
        assert expected != ()
        assert companion(board, "R1") == expected
        assert companion(board, "R2") == ()

    def test_moving_the_ref_inside_empties_its_companion(self):
        board = left_overhang_board().with_placement(
            "R1", 20_000_000, 15_000_000, 0, "F.Cu"
        )
        _, companion = next(iter(drc.COMPANIONS.items()))
        assert companion(board, "R1") == ()

    def test_companion_matches_the_full_check_for_every_ref(self):
        board = (
            corner_overhang_board()  # R1 crosses left+top, left mounted
            .with_constraint(mount("R1", Edge.LEFT))
            .with_placement("R2", 1_000_000, 20_000_000, 0, "F.Cu")
            .with_constraint(mount("R2", Edge.LEFT, id="R2-left"))
            .with_placement("R3", -2_000_000, 15_000_000, 0, "F.Cu")
        )
        check, companion = next(iter(drc.COMPANIONS.items()))
        assert check(board) != ()
        for ref in ("R1", "R2", "R3"):
            expected = tuple(
                violation
                for violation in check(board)
                if f"placement:{ref}" in violation.offending_refs
            )
            assert companion(board, ref) == expected
        assert companion(board, "NOSUCH") == ()


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
            "COMPANIONS",
            "Companion",
        }


# One sample (refs, locations) pair per type, shaped to the convention the
# type's wording reads: plain findings pair refs with locations 1:1, the
# constraint findings lead with the location-less ``constraint:<id>``.
_SAMPLE_FINDINGS = {
    ViolationType.COURTYARDS_OVERLAP: (
        ("placement:R1", "placement:R2"),
        (Point(X, Y), Point(X, Y)),
    ),
    ViolationType.COURTYARD_OUTSIDE_OUTLINE: (("placement:R1",), (Point(X, Y),)),
    ViolationType.PROXIMITY_UNMET: (
        ("constraint:c1", "placement:R1", "placement:R2"),
        (Point(X, Y), Point(X, Y)),
    ),
    ViolationType.REGION_UNMET: (("constraint:c1", "placement:R1"), (Point(X, Y),)),
    ViolationType.EDGE_MOUNT_UNMET: (("constraint:c1", "placement:R1"), (Point(X, Y),)),
    ViolationType.KEEP_TOGETHER_UNMET: (
        ("constraint:c1", "placement:R1"),
        (Point(X, Y),),
    ),
}


class TestDescribeExhaustive:
    """Every ViolationType member has pinned wording: a type added to the
    enum without a ``_describe`` case fails here at test time, not at the
    first finding's canonicalization."""

    @pytest.mark.parametrize("violation_type", list(ViolationType))
    def test_every_type_has_pinned_wording(self, violation_type):
        refs, locations = _SAMPLE_FINDINGS[violation_type]
        description = drc._describe(violation_type, refs, locations, overlapped_board())
        assert isinstance(description, str)
        assert description
