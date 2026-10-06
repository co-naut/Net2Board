"""The four constraint checks — unmet Requirements as Violations (#42, ADR-0011).

One code per relation: ``PROXIMITY_UNMET``, ``REGION_UNMET``,
``EDGE_MOUNT_UNMET``, ``KEEP_TOGETHER_UNMET``, each at ERROR severity.
Preferences never produce Violations — they belong to the objective
(ADR-0014); an unplaced anchor makes a constraint *pending*, not violated
(ADR-0011). The measures are the geometry module's — the single definition
the objective's Preference hinges will reuse (ADR-0014) — so every
boundary here is an exact integer-nanometre table value.

The local world: ``TINY`` is a one-pad footprint with a ±0.5 mm rectangle
courtyard, so two TINY parts 7.62 mm apart overlap nowhere and every
finding is the constraint's alone; ``NOYARD`` keeps the pad but drops the
courtyard — the pad-bounding-box fallback's fixture (and the only way to
bring two pads within nanometres of each other without their courtyards
overlapping).

Findings establish the first **variable-arity convention** (ADR-0009's
door, open since M1): ``offending_refs`` runs one longer than
``locations``, the leading ``constraint:<id>`` location-less, locations
pairing with the trailing placement refs. Descriptions are pinned per
type; Region/EdgeMount/KeepTogether note the pad-bounding-box fallback a
courtyard-less footprint measures by (ADR-0011, amended by #42 to cover
EdgeMount).
"""

from conftest import fresh_board, make_spec
from net2board.build import build_board
from net2board.drc import Severity, ViolationType, run_drc
from net2board.geometry import Point, Rectangle
from net2board.ir import CompIR, FootprintIR, NetlistIR, PadIR
from net2board.model import (
    Constraint,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Preference,
    Proximity,
    Region,
    Requirement,
)

TINY_PAD = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(1_600_000, 1_600_000),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
TINY_COURTYARD = Rectangle(min=Point(-500_000, -500_000), max=Point(500_000, 500_000))

X = 10_000_000
Y = 10_000_000


def tiny_board():
    """Two TINY comps (R1, R2), no nets, no placements."""
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="R1", entry_name="TINY"),
                CompIR(ref="R2", entry_name="TINY"),
            ),
            nets=(),
        ),
        footprint_irs={
            "TINY": FootprintIR(
                entry_name="TINY", pads=TINY_PAD, courtyard=TINY_COURTYARD
            )
        },
    )
    return build_board(spec)


def noyard_board():
    """Two NOYARD comps — one pad each, no courtyard."""
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="R1", entry_name="NOYARD"),
                CompIR(ref="R2", entry_name="NOYARD"),
            ),
            nets=(),
        ),
        footprint_irs={
            "NOYARD": FootprintIR(entry_name="NOYARD", pads=TINY_PAD, courtyard=None)
        },
    )
    return build_board(spec)


def proximity(
    ref_a="R1",
    pad_a="1",
    ref_b="R2",
    pad_b="1",
    bound=7_620_000,
    same_side=False,
    kind=None,
    id="cap-near",
):
    return Constraint(
        id=id,
        kind=kind or Requirement(),
        relation=Proximity(
            ref_a=ref_a,
            pad_a=pad_a,
            ref_b=ref_b,
            pad_b=pad_b,
            bound_nm=bound,
            same_side=same_side,
        ),
    )


def decoupled_board():
    """R1 and R2 7.62 mm apart on F.Cu — pad centers exactly at the bound."""
    return (
        tiny_board()
        .with_constraint(proximity())
        .with_placement("R1", X, Y, 0, "F.Cu")
        .with_placement("R2", X + 7_620_000, Y, 0, "F.Cu")
    )


class TestProximityUnmet:
    def test_bound_inclusive_is_satisfied(self):
        assert run_drc(decoupled_board()) == ()

    def test_one_nm_short_is_unmet(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=7_619_999, id="tight"))
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.PROXIMITY_UNMET
        assert violation.severity is Severity.ERROR

    def test_isqrt_boundary_is_the_measure(self):
        # anchors (0,0) and (1,2): true distance √5 ≈ 2.236, the integer-nm
        # measure isqrt(5) = 2 — satisfied at bound 2, unmet at bound 1.
        # Courtyard-less parts: nothing but the constraint can speak here.
        for bound, clean in ((2, True), (1, False)):
            board = (
                noyard_board()
                .with_constraint(proximity(bound=bound, id=f"b{bound}"))
                .with_placement("R1", 0, 0, 0, "F.Cu")
                .with_placement("R2", 1, 2, 0, "F.Cu")
            )
            proximity_findings = [
                v for v in run_drc(board) if v.type is ViolationType.PROXIMITY_UNMET
            ]
            assert (not proximity_findings) if clean else proximity_findings

    def test_constraint_leads_the_refs_locationless(self):
        # the variable-arity convention: constraint:<id> first with no
        # location; the two pad centers pair with their placement refs
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=1, id="tight"))
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == (
            "constraint:tight",
            "placement:R1",
            "placement:R2",
        )
        assert violation.locations == (Point(X, Y), Point(X + 7_620_000, Y))

    def test_locations_follow_refs_when_stated_order_reverses(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(ref_a="R2", ref_b="R1", bound=1))
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == (
            "constraint:cap-near",
            "placement:R1",
            "placement:R2",
        )
        assert violation.locations == (Point(X, Y), Point(X + 7_620_000, Y))

    def test_description_is_pinned(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=1, id="tight"))
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "Proximity unmet: placement:R1 and placement:R2 (constraint:tight)"
        )

    def test_preference_produces_no_violation(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=1, kind=Preference(), id="wish"))
        )
        assert run_drc(board) == ()

    def test_requirement_weight_changes_nothing(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(
                Constraint(
                    id="weighted",
                    kind=Requirement(),
                    relation=Proximity(
                        ref_a="R1", pad_a="1", ref_b="R2", pad_b="1", bound_nm=1
                    ),
                    weight=99,
                )
            )
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.PROXIMITY_UNMET

    def test_cross_side_pairs_satisfy_by_default(self):
        board = decoupled_board().with_placement("R2", X + 7_620_000, Y, 0, "B.Cu")
        assert run_drc(board) == ()

    def test_same_side_knob_flags_cross_side_pairs(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(same_side=True))
            .with_placement("R2", X + 7_620_000, Y, 0, "B.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.PROXIMITY_UNMET

    def test_unplaced_anchor_is_pending_not_violated(self):
        # neither placed — an all-unplaced Board stays clean (ADR-0011)
        assert run_drc(tiny_board().with_constraint(proximity())) == ()

    def test_partly_placed_pair_is_pending_too(self):
        board = (
            tiny_board()
            .with_constraint(proximity())
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_immune_to_stating_and_placing_order(self):
        placed_first = (
            tiny_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 7_620_000, Y, 0, "F.Cu")
            .with_constraint(proximity(bound=1, id="tight"))
        )
        stated_first = (
            tiny_board()
            .with_constraint(proximity(bound=1, id="tight"))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 7_620_000, Y, 0, "F.Cu")
        )
        assert run_drc(placed_first) == run_drc(stated_first)
        assert len(run_drc(stated_first)) == 1


IN_RECT = Rectangle(Point(5_000_000, 5_000_000), Point(25_000_000, 25_000_000))


def region(target="placement:R1", rect=None, kind=None, id="power-area"):
    return Constraint(
        id=id,
        kind=kind or Requirement(),
        relation=Region(target=target, rect=rect or IN_RECT),
    )


def placed_region_board():
    """R1 inside the region rect, R2 poking 1M past its max-x face."""
    return (
        tiny_board()
        .with_group(Group(id="pair", refs=("R1", "R2")))
        .with_constraint(region())
        .with_placement("R1", 15_000_000, 15_000_000, 0, "F.Cu")
        .with_placement("R2", 25_500_000, 15_000_000, 0, "F.Cu")
    )


class TestRegionUnmet:
    def test_courtyard_inside_is_satisfied(self):
        board = (
            tiny_board()
            .with_constraint(region())
            .with_placement("R1", 15_000_000, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_tangent_to_the_rect_face_is_inside(self):
        # closed containment: yard min corner exactly on the rect's min corner
        board = (
            tiny_board()
            .with_constraint(region())
            .with_placement("R1", 5_500_000, 5_500_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_one_axis_poke_is_unmet(self):
        board = (
            tiny_board()
            .with_constraint(region())
            .with_placement("R1", 4_999_999, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.REGION_UNMET
        assert violation.severity is Severity.ERROR

    def test_finding_names_constraint_and_offender_at_its_position(self):
        board = (
            tiny_board()
            .with_constraint(region(id="tight"))
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == ("constraint:tight", "placement:R1")
        assert violation.locations == (Point(4_000_000, 15_000_000),)

    def test_description_is_pinned(self):
        board = (
            tiny_board()
            .with_constraint(region(id="tight"))
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "Region unmet: placement:R1 (constraint:tight)"
        )

    def test_group_target_offends_per_member(self):
        # R1 inside, R2 out: exactly R2's finding; the constraint is the
        # subject, the offender is named alone (the Locality axiom, #42)
        board = (
            placed_region_board()
            .without_constraint("power-area")
            .with_constraint(region(target="group:pair", id="gather"))
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == ("constraint:gather", "placement:R2")
        assert violation.locations == (Point(25_500_000, 15_000_000),)

    def test_two_offenders_sort_by_ref(self):
        board = (
            placed_region_board()
            .without_constraint("power-area")
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
            .with_constraint(region(target="group:pair", id="gather"))
        )
        findings = [v for v in run_drc(board) if v.type is ViolationType.REGION_UNMET]
        assert [v.offending_refs[1] for v in findings] == [
            "placement:R1",
            "placement:R2",
        ]

    def test_placing_order_changes_nothing(self):
        a = (
            placed_region_board()
            .without_constraint("power-area")
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
            .with_constraint(region(target="group:pair", id="gather"))
        )
        b = (
            tiny_board()
            .with_placement("R2", 25_500_000, 15_000_000, 0, "F.Cu")
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
            .with_group(Group(id="pair", refs=("R2", "R1")))
            .with_constraint(region(target="group:pair", id="gather"))
        )
        assert run_drc(a) == run_drc(b)

    def test_preference_produces_no_violation(self):
        board = (
            tiny_board()
            .with_constraint(region(kind=Preference(), id="wish"))
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_unplaced_member_is_pending(self):
        # neither placed
        board = (
            tiny_board()
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(region(target="group:pair"))
        )
        assert run_drc(board) == ()
        # one placed inside, the other unplaced — the placed one is clean
        board = board.with_placement("R1", 15_000_000, 15_000_000, 0, "F.Cu")
        assert run_drc(board) == ()

    def test_padless_courtyardless_member_measures_by_pad_bbox_and_says_so(self):
        # NOYARD pad box is ±800k around the anchor: at (0, 10M) it pokes
        # 800_000 past the region's min-x face — and the description notes
        # the fallback (ADR-0011, extended to EdgeMount by #42)
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOYARD"),), nets=()
            ),
            footprint_irs={
                "NOYARD": FootprintIR(
                    entry_name="NOYARD", pads=TINY_PAD, courtyard=None
                )
            },
        )
        board = (
            build_board(spec)
            .with_constraint(region(target="placement:X1", id="tight"))
            .with_placement("X1", 4_200_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "Region unmet: placement:X1 (constraint:tight)"
            " — measured by pad bounding box"
        )
        # tangent at the face: the pad box reaches exactly to the rect edge
        board = (
            build_board(spec)
            .with_constraint(region(target="placement:X1", id="tight"))
            .with_placement("X1", 5_800_000, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_padless_footprint_with_no_pads_at_all_is_skipped(self):
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOTHING"),), nets=()
            ),
            footprint_irs={
                "NOTHING": FootprintIR(entry_name="NOTHING", pads=(), courtyard=None)
            },
        )
        board = (
            build_board(spec)
            .with_constraint(region(target="placement:X1", id="tight"))
            .with_placement("X1", 0, 0, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_coexists_with_proximity_in_the_sorted_tuple(self):
        board = (
            tiny_board()
            .with_constraint(proximity(bound=1, id="cap"))
            .with_constraint(region(id="area"))
            .with_placement("R1", 2_000_000, 15_000_000, 0, "F.Cu")
            .with_placement("R2", 27_000_000, 15_000_000, 0, "F.Cu")
        )
        assert [v.type.value for v in run_drc(board)] == [
            "proximity_unmet",
            "region_unmet",
        ]


def mount(target="placement:R1", edge=Edge.LEFT, bound=None, kind=None, id=None):
    return Constraint(
        id=id or f"m-{target.split(':', 1)[1]}-{edge.value}",
        kind=kind or Requirement(),
        relation=EdgeMount(target=target, edge=edge, max_overhang_nm=bound),
    )


class TestEdgeMountUnmet:
    def test_face_flush_on_the_edge_is_satisfied(self):
        # yard min.x exactly on the edge line: offset 0, no bound stated
        board = (
            tiny_board()
            .with_constraint(mount())
            .with_placement("R1", 500_000, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_overhang_within_the_bound_is_satisfied(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=1_000_000))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_exact_at_the_bound_is_satisfied(self):
        # the ecc83 posture: every stated bound equals the measured overhang
        board = (
            tiny_board()
            .with_constraint(mount(bound=500_000))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_over_bound_overhang_is_unmet(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=499_999, id="snug"))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.EDGE_MOUNT_UNMET
        assert violation.severity is Severity.ERROR

    def test_interior_face_is_unmet_even_flush(self):
        # the face must lie ON the edge: 9.5 mm short of it is unmet however
        # generous the bound — the band is [edge, edge + overhang]
        board = (
            tiny_board()
            .with_constraint(mount(bound=10_000_000, id="off"))
            .with_placement("R1", 10_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.EDGE_MOUNT_UNMET

    def test_finding_names_constraint_and_offender_at_its_position(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=1, id="off"))
            .with_placement("R1", 10_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == ("constraint:off", "placement:R1")
        assert violation.locations == (Point(10_000_000, 15_000_000),)

    def test_description_is_pinned(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=1, id="off"))
            .with_placement("R1", 10_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "EdgeMount unmet: placement:R1 (constraint:off)"
        )

    def test_over_bound_overhang_is_the_stated_edges_one_finding(self):
        # ADR-0018's single-reporting rule, completed by #42: the stated
        # edge silences containment regardless of the bound, and the
        # over-bound overhang is EDGE_MOUNT_UNMET's alone to report
        board = (
            fresh_board()
            .with_placement("R1", 1_000_000, 10_000_000, 0, "F.Cu")  # 50 µm overhang
            .with_constraint(
                Constraint(
                    id="R1-left",
                    kind=Requirement(),
                    relation=EdgeMount(
                        target="placement:R1", edge=Edge.LEFT, max_overhang_nm=1
                    ),
                )
            )
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.EDGE_MOUNT_UNMET

    def test_group_target_offends_per_member(self):
        board = (
            tiny_board()
            .with_group(Group(id="edge", refs=("R1", "R2")))
            .with_constraint(mount(target="group:edge", bound=1_000_000, id="g"))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")  # 500k overhang: in band
            .with_placement("R2", 10_000_000, 15_000_000, 0, "F.Cu")  # interior
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == ("constraint:g", "placement:R2")

    def test_preference_produces_no_violation(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=1, kind=Preference(), id="wish"))
            .with_placement("R1", 10_000_000, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_unplaced_member_is_pending(self):
        board = tiny_board().with_constraint(mount())
        assert run_drc(board) == ()

    def test_padless_courtyardless_member_measures_by_pad_bbox_and_says_so(self):
        # NOYARD pad box is ±800k: at x=0 its face sits 800k past the edge —
        # within a 1M bound, so satisfied; at bound 799_999 unmet, noted
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOYARD"),), nets=()
            ),
            footprint_irs={
                "NOYARD": FootprintIR(
                    entry_name="NOYARD", pads=TINY_PAD, courtyard=None
                )
            },
        )
        board = (
            build_board(spec)
            .with_constraint(
                mount(target="placement:X1", bound=1_000_000, id="soft-edge")
            )
            .with_placement("X1", 0, 15_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()
        board = (
            build_board(spec)
            .with_constraint(mount(target="placement:X1", bound=799_999, id="snug"))
            .with_placement("X1", 0, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "EdgeMount unmet: placement:X1 (constraint:snug)"
            " — measured by pad bounding box"
        )

    def test_coexists_with_proximity_in_the_sorted_tuple(self):
        board = (
            tiny_board()
            .with_constraint(mount(bound=1, id="off"))
            .with_constraint(proximity(bound=1, id="cap"))
            .with_placement("R1", 10_000_000, 15_000_000, 0, "F.Cu")
            .with_placement("R2", 28_000_000, 15_000_000, 0, "F.Cu")
        )
        assert [v.type.value for v in run_drc(board)] == [
            "edge_mount_unmet",
            "proximity_unmet",
        ]


def keep_together(
    target="group:pair",
    width=7_000_000,
    height=7_000_000,
    kind=None,
    id="cluster",
):
    return Constraint(
        id=id,
        kind=kind or Requirement(),
        relation=KeepTogether(target=target, max_width_nm=width, max_height_nm=height),
    )


def clustered_board():
    """R1 and R2 5 mm apart horizontally: union extent 6 mm × 1 mm."""
    return (
        tiny_board()
        .with_group(Group(id="pair", refs=("R1", "R2")))
        .with_constraint(keep_together())
        .with_placement("R1", 10_000_000, 10_000_000, 0, "F.Cu")
        .with_placement("R2", 15_000_000, 10_000_000, 0, "F.Cu")
    )


class TestKeepTogetherUnmet:
    def test_extent_within_the_bound_is_satisfied(self):
        assert run_drc(clustered_board()) == ()

    def test_exact_at_the_bound_is_satisfied(self):
        # union is exactly 6 mm wide: the floating bound is inclusive
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=6_000_000, height=1_000_000))
        )
        assert run_drc(board) == ()

    def test_one_nm_over_on_width_is_unmet(self):
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=5_999_999, height=1_000_000))
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.KEEP_TOGETHER_UNMET
        assert violation.severity is Severity.ERROR

    def test_height_axis_bounds_too(self):
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_placement("R2", 10_000_000, 15_000_000, 0, "F.Cu")
            .with_constraint(keep_together(width=1_000_000, height=5_999_999))
        )
        (violation,) = run_drc(board)
        assert violation.type is ViolationType.KEEP_TOGETHER_UNMET

    def test_the_finding_names_the_whole_group(self):
        # the extent is collective: every member is named, so any member's
        # move is attributable to the finding (the Locality axiom, #42)
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=1_000_000, height=1_000_000))
        )
        (violation,) = run_drc(board)
        assert violation.offending_refs == (
            "constraint:cluster",
            "placement:R1",
            "placement:R2",
        )
        assert violation.locations == (
            Point(10_000_000, 10_000_000),
            Point(15_000_000, 10_000_000),
        )

    def test_description_is_pinned(self):
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=1_000_000, height=1_000_000))
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "KeepTogether unmet: placement:R1 and placement:R2 (constraint:cluster)"
        )

    def test_any_unplaced_member_is_pending(self):
        # the extent cannot be measured short a member: silent until placed
        board = (
            tiny_board()
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(keep_together())
            .with_placement("R1", 10_000_000, 10_000_000, 0, "F.Cu")
        )
        assert run_drc(board) == ()

    def test_preference_produces_no_violation(self):
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(
                keep_together(
                    width=1_000_000, height=1_000_000, kind=Preference(), id="wish"
                )
            )
        )
        assert run_drc(board) == ()

    def test_placing_order_changes_nothing(self):
        a = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=1_000_000, height=1_000_000))
        )
        b = (
            tiny_board()
            .with_placement("R2", 15_000_000, 10_000_000, 0, "F.Cu")
            .with_placement("R1", 10_000_000, 10_000_000, 0, "F.Cu")
            .with_group(Group(id="pair", refs=("R2", "R1")))
            .with_constraint(keep_together(width=1_000_000, height=1_000_000))
        )
        assert run_drc(a) == run_drc(b)

    def test_padless_courtyardless_member_measures_by_pad_bbox_and_says_so(self):
        # a lone NOYARD member is a 1.6 mm pad box: 1 nm over the width
        spec = make_spec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="X1", entry_name="NOYARD"),), nets=()
            ),
            footprint_irs={
                "NOYARD": FootprintIR(
                    entry_name="NOYARD", pads=TINY_PAD, courtyard=None
                )
            },
        )
        board = (
            build_board(spec)
            .with_group(Group(id="solo", refs=("X1",)))
            .with_constraint(
                keep_together(target="group:solo", width=1_599_999, height=1_599_999)
            )
            .with_placement("X1", 20_000_000, 15_000_000, 0, "F.Cu")
        )
        (violation,) = run_drc(board)
        assert violation.description == (
            "KeepTogether unmet: placement:X1 (constraint:cluster)"
            " — measured by pad bounding box"
        )


import net2board.drc as drc_module
from net2board.drc import CHECKS, COMPANIONS


def refiltered(check_output, ref):
    return tuple(v for v in check_output if f"placement:{ref}" in v.offending_refs)


class TestLocalityCompanions:
    """Each constraint check declares its locality companion (ADR-0016):
    ``companion(board, ref)`` returns exactly the findings of the full
    check whose ``offending_refs`` contain the moved ref (CONTEXT.md's
    Locality axiom) — here, for a single moved placement."""

    def test_every_check_with_a_local_companion_declares_one(self):
        # containment (ADR-0018) plus the four constraint checks; the
        # all-pairs overlap check has no companion — slower, never wrong
        declaring = {check for check in CHECKS if check in COMPANIONS}
        assert declaring == {
            drc_module._courtyard_outside_outline,
            drc_module._proximity_unmet,
            drc_module._region_unmet,
            drc_module._edge_mount_unmet,
            drc_module._keep_together_unmet,
        }

    def test_proximity_companion_recomputes_its_own_pair(self):
        board = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=1, id="tight"))
            .with_constraint(proximity(ref_a="R2", ref_b="R1", bound=1, id="back"))
        )
        check = drc_module._proximity_unmet
        companion = COMPANIONS[check]
        assert check(board) != ()
        # both constraints name both refs: either ref's companion is the
        # same two findings; R3 — on no Proximity — is empty
        assert companion(board, "R1") == refiltered(check(board), "R1")
        assert companion(board, "R2") == refiltered(check(board), "R2")
        assert companion(board, "R1") == check(board)
        assert companion(board, "NO SUCH") == ()

    def test_proximity_companion_clears_after_the_move(self):
        # bound 7_620_002 is unmet at 7_620_000; moving R2 one nm closer
        # satisfies it — R2's companion empties with the full check
        moved = (
            decoupled_board()
            .without_constraint("cap-near")
            .with_constraint(proximity(bound=7_620_002, id="tight"))
            .with_placement("R2", X + 7_620_001, Y, 0, "F.Cu")
        )
        check = drc_module._proximity_unmet
        assert check(moved) == ()
        assert COMPANIONS[check](moved, "R2") == ()

    def test_region_companion_recomputes_only_the_moved_member(self):
        board = (
            placed_region_board()
            .without_constraint("power-area")
            .with_placement("R1", 4_000_000, 15_000_000, 0, "F.Cu")
            .with_constraint(region(target="group:pair", id="gather"))
        )
        check = drc_module._region_unmet
        companion = COMPANIONS[check]
        assert check(board) != ()
        assert companion(board, "R1") == refiltered(check(board), "R1")
        assert companion(board, "R2") == refiltered(check(board), "R2")
        assert companion(board, "NO SUCH") == ()

    def test_region_companion_flips_with_a_single_moved_placement(self):
        # moving R1 inside empties R1's finding; R2's is untouched
        moved = placed_region_board()
        check = drc_module._region_unmet
        companion = COMPANIONS[check]
        assert companion(moved, "R1") == ()
        assert companion(moved, "R2") == refiltered(check(moved), "R2")

    def test_edge_mount_companion_recomputes_only_the_moved_member(self):
        board = (
            tiny_board()
            .with_group(Group(id="edge", refs=("R1", "R2")))
            .with_constraint(mount(target="group:edge", bound=1_000_000, id="g"))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")  # flush-ish: in band
            .with_placement("R2", 10_000_000, 15_000_000, 0, "F.Cu")  # interior
        )
        check = drc_module._edge_mount_unmet
        companion = COMPANIONS[check]
        assert check(board) != ()
        assert companion(board, "R1") == ()
        assert companion(board, "R2") == refiltered(check(board), "R2")

    def test_keep_together_companion_returns_the_whole_group_finding(self):
        # union is exactly 6 mm wide: unmet at bound 5_999_999
        board = (
            clustered_board()
            .without_constraint("cluster")
            .with_constraint(keep_together(width=5_999_999, height=1_000_000))
        )
        check = drc_module._keep_together_unmet
        companion = COMPANIONS[check]
        (finding,) = check(board)
        # the extent is collective: both members' companions name it
        assert companion(board, "R1") == (finding,)
        assert companion(board, "R2") == (finding,)
        assert companion(board, "NO SUCH") == ()
        # moving R1 0.5 mm left narrows the union to 5.5 mm: satisfied
        moved = board.with_placement("R1", 10_500_000, 10_000_000, 0, "F.Cu")
        assert check(moved) == ()
        assert companion(moved, "R1") == ()

    def test_companions_match_the_full_check_for_every_ref(self):
        # one board carrying all four relations at once, two refs each:
        # the companion/filtered-full identity holds for every ref
        board = (
            tiny_board()
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(proximity(bound=1, id="cap"))
            .with_constraint(region(target="group:pair", id="area"))
            .with_constraint(mount(target="group:pair", bound=1, id="g"))
            .with_constraint(keep_together(width=1_000_000, height=1_000_000))
            .with_placement("R1", 0, 15_000_000, 0, "F.Cu")
            .with_placement("R2", 3_000_000, 15_000_000, 0, "F.Cu")
        )
        checks = (
            drc_module._proximity_unmet,
            drc_module._region_unmet,
            drc_module._edge_mount_unmet,
            drc_module._keep_together_unmet,
        )
        for check in checks:
            full = check(board)
            assert full != ()
            for ref in ("R1", "R2"):
                assert COMPANIONS[check](board, ref) == refiltered(full, ref)
