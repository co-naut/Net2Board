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
tests is a few millimetres, and every bound breach is unambiguous. The
copper world adds CHIP (two rect pads at ±0.4 mm, the copper box and yard
±0.65 mm — two chips on a row can sit with clear courtyards and a
violating pad gap), SSOP (pads that touch: an intra-footprint pair),
THT/SMD (a ``*.Cu`` barrel vs a back-side pad), JP (a custom pad whose
copper extent is its hull), and TIE (a net-tie group exempting its own
overlap). Default rules: clearance 0.2 mm, edge clearance 0.5 mm.
"""

from enum import Enum

import pytest

from conftest import (
    SWIVEL_WORLD,
    load_ecc83_spec,
    make_spec,
)
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import Severity, Violation, ViolationType, run_drc
from net2board.errors import Net2BoardError
from net2board.examples.ecc83_placements import beat_overlap_board
from net2board.geometry import Point, Rectangle, pad_world_center
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, NodeIR, PadIR
from net2board.model import (
    Board,
    Constraint,
    DesignRules,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Preference,
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

ROOMY_PAD = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(500_000, 500_000),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
ROOMY_YARD = Rectangle(Point(-mm(1), -mm(1)), Point(mm(1), mm(1)))
ROOMY = {"ROOMY": FootprintIR(entry_name="ROOMY", pads=ROOMY_PAD, courtyard=ROOMY_YARD)}


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


def roomy_board(*refs: str) -> Board:
    """An unplaced Board over the given ROOMY comps, no nets — pads small
    next to the courtyard, so a courtyard repair leaves the copper clear
    and a legal board reads clean (the #96 world)."""
    return build_board(
        make_spec(
            netlist_ir=NetlistIR(
                comps=tuple(CompIR(ref=ref, entry_name="ROOMY") for ref in refs),
                nets=(),
            ),
            footprint_irs=ROOMY,
        )
    )


def _rect_pad(number: str, x: int, w: int = 500_000) -> PadIR:
    return PadIR(
        number=number,
        local_pos=Point(x, 0),
        size=(w, w),
        drill=None,
        layers=("F.Cu",),
        shape_enum="rect",
    )


CHIP = {
    "CHIP": FootprintIR(
        entry_name="CHIP",
        pads=(_rect_pad("1", -400_000), _rect_pad("2", 400_000)),
        courtyard=Rectangle(Point(-650_000, -650_000), Point(650_000, 650_000)),
    )
}
"""A two-pad 0402-ish chip: the copper box is the yard, ±0.65 mm — two
chips on one row sit with clear courtyards and a violating pad gap."""

SSOP = {
    "SSOP": FootprintIR(
        entry_name="SSOP",
        pads=(_rect_pad("1", -100_000, 200_000), _rect_pad("2", 100_000, 200_000)),
        courtyard=Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000)),
    )
}
"""A two-pad part whose pads touch — the inner faces meet at x=0 — so its
own intra-footprint pair is a 0-gap clearance: placement-invariant."""

SSOP_WIDE = {
    "SSOP": FootprintIR(
        entry_name="SSOP",
        pads=(_rect_pad("1", -225_000, 200_000), _rect_pad("2", 225_000, 200_000)),
        courtyard=Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000)),
    )
}
"""The same part with the pads 0.25 mm apart — clean at the 0.2 mm rule,
a self-pair only once the board's rule rises past the gap (the spec's
TSSOP-at-raised-clearance world)."""

THT = {
    "THT": FootprintIR(
        entry_name="THT",
        pads=(
            PadIR(
                number="1",
                local_pos=Point(0, 0),
                size=(1_000_000, 1_000_000),
                drill=600_000,
                layers=("*.Cu",),
                shape_enum="circle",
            ),
        ),
        courtyard=Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000)),
    )
}
SMD = {
    "SMD": FootprintIR(
        entry_name="SMD",
        pads=(
            PadIR(
                number="1",
                local_pos=Point(0, 0),
                size=(500_000, 500_000),
                drill=None,
                layers=("F.Cu",),
                shape_enum="circle",
            ),
        ),
        courtyard=Rectangle(Point(-300_000, -300_000), Point(300_000, 300_000)),
    )
}
"""The shared-layer pair: a ``*.Cu`` barrel (every stackup layer) and a
front-defined SMD pad that lands on B.Cu once its part sits on the back
(KiCad's flip)."""

JP = {
    "JP": FootprintIR(
        entry_name="JP",
        pads=(
            PadIR(
                number="1",
                local_pos=Point(0, 0),
                size=(2_000_000, 1_000_000),
                drill=None,
                layers=("F.Cu",),
                shape_enum="custom",
                hull=Rectangle(Point(-1_000_000, -500_000), Point(1_000_000, 500_000)),
            ),
        ),
        courtyard=Rectangle(Point(-1_050_000, -400_000), Point(1_050_000, 400_000)),
    )
}
"""A custom-pad connector (the JP family): the copper extent is the hull
box, 2 × 1 mm — wider than the yard is tall, so two JPs stack with clear
courtyards and overlapping copper, and the push-out must size itself on
the full extent."""

TIE = {
    "TIE": FootprintIR(
        entry_name="TIE",
        pads=(_rect_pad("1", -100_000, 300_000), _rect_pad("2", 100_000, 300_000)),
        courtyard=Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000)),
        net_tie_groups=(frozenset({"1", "2"}),),
    )
}
"""The net-tie part: two overlapping pads sharing one net-tie group — the
pair exempts itself (ADR-0022's second exemption)."""


def copper_board(*refs: str, nets=(), footprint_irs=CHIP, rules=None) -> Board:
    """An unplaced Board over same-footprint comps with optional nets and
    design rules — the copper world's base board."""
    return build_board(
        make_spec(
            netlist_ir=NetlistIR(
                comps=tuple(
                    CompIR(ref=ref, entry_name=next(iter(footprint_irs)))
                    for ref in refs
                ),
                nets=nets,
            ),
            footprint_irs=footprint_irs,
            design_rules=rules,
        )
    )


def placed(
    board: Board, ref: str, x: float, y: float, locked: bool = False, side: str = "F.Cu"
) -> Board:
    # the copper world's fractional placements round to exact nm —
    # one- and two-decimal millimetres are exact at this scale
    return board.with_placement(ref, round(mm(x)), round(mm(y)), 0, side, locked)


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

    def test_an_unrepairable_finding_does_not_undo_other_repairs(self):
        # A's own two pads touch: an intra-footprint clearance —
        # placement-invariant, no translation repairs it (ADR-0012 as
        # amended by #83). B overlaps A's courtyard: a repairable
        # courtyards_overlap. The repair must clear the overlap without
        # letting the unrepairable finding undo it — A stays moved, the
        # self-pair is the honest residual naming A alone.
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(
                    comps=(
                        CompIR(ref="A", entry_name="SSOP"),
                        CompIR(ref="B", entry_name="TINY"),
                    ),
                    nets=(),
                ),
                footprint_irs={**SSOP, **TINY},
            )
        )
        board = board.with_placement("A", mm(10), mm(10), 0, "F.Cu")
        board = board.with_placement("B", 10_900_000, mm(10), 0, "F.Cu")
        result = legalize(board)
        (finding,) = error_findings(result.board)
        assert finding.type is ViolationType.CLEARANCE
        assert finding.offending_refs == ("placement:A", "placement:A")
        assert result.unlegalized_refs == ("A",)
        # the overlap repair survived: a 0.1 mm penetration pushes the
        # face 0.101 mm, and the self-pair could not undo it
        assert result.displacements == {"A": 101_000}
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["A"].pos == Point(9_899_000, mm(10))

    def test_a_cleared_offender_is_no_longer_named(self):
        # #96: A's Region cage pulls the part back into B after the
        # overlap push-out; the cycle burns A's attempt cap, A is
        # restored and parked, and B's own move then clears the pair.
        # The board comes back legal, so nothing may be named: the
        # report reads the final board, not the repairer's failure
        # history.
        cage = Rectangle(Point(mm(9), mm(9)), Point(mm(11), mm(11)))
        board = (
            roomy_board("A", "B")
            .with_constraint(
                Constraint(
                    id="cage",
                    kind=Requirement(),
                    relation=Region(target="placement:A", rect=cage),
                )
            )
            .with_placement("A", mm(10), mm(10), 0, "F.Cu")
            .with_placement("B", mm(11), mm(10), 0, "F.Cu")
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        assert set(result.displacements) == {"B"}

    def test_the_report_reads_the_final_board(self):
        # The pin behind #96's fix, over this file's worlds: the named
        # refs are exactly the Offenders of the result board's
        # residual error findings — success stays sound and naming
        # stays honest whatever the repairer's failure history was.
        # The cage is the ticket's own shape, so the constraint types
        # are pinned too, not just the courtyard families: the near
        # cage exercises the cleared direction, and the far cage under
        # a bound pins the surviving direction — a Region finding that
        # survives must name its Offender.
        cage = Rectangle(Point(mm(9), mm(9)), Point(mm(11), mm(11)))
        caged = (
            roomy_board("A", "B")
            .with_constraint(
                Constraint(
                    id="cage",
                    kind=Requirement(),
                    relation=Region(target="placement:A", rect=cage),
                )
            )
            .with_placement("A", mm(10), mm(10), 0, "F.Cu")
            .with_placement("B", mm(11), mm(10), 0, "F.Cu")
        )
        far_cage = Rectangle(Point(mm(20), mm(20)), Point(mm(22), mm(22)))
        caged_far = (
            roomy_board("A", "B")
            .with_constraint(
                Constraint(
                    id="far",
                    kind=Requirement(),
                    relation=Region(target="placement:A", rect=far_cage),
                )
            )
            .with_placement("A", mm(10), mm(10), 0, "F.Cu")
            .with_placement("B", mm(15), mm(15), 0, "F.Cu")
        )
        cases = [
            (overlapping_board(), {}),
            (placed(tiny_board("R1"), "R1", -1, 10), {}),
            (
                placed(placed(tiny_board("R1", "R2"), "R1", 10, 10), "R2", 10, 10),
                {},
            ),
            (legalize(overlapping_board(), max_displacement=1_000).board, {}),
            (
                placed(
                    placed(tiny_board("A", "B"), "A", 10, 10, locked=True),
                    "B",
                    10,
                    10,
                ),
                {},
            ),
            (caged, {}),
            (caged_far, {"max_displacement": mm(2)}),
        ]
        for board, kwargs in cases:
            result = legalize(board, **kwargs)
            residual = {
                ref.removeprefix("placement:")
                for finding in error_findings(result.board)
                for ref in finding.offending_refs
                if ref.startswith("placement:")
            }
            assert set(result.unlegalized_refs) == residual

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
        # the yard push separates the coincident pads by a hair — a
        # clearance the pad-pair move then finishes: the board ends
        # fully clean (1.001 mm of yard, then 0.2 mm of pad clearance)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        # the canonical first offender is the mover
        assert result.displacements == {"R1": 1_201_000}

    def test_containment_becomes_legal(self):
        board = placed(tiny_board("R1"), "R1", -1, 10)  # 1.5 mm off the left
        result = legalize(board)
        # containment clamps flush (1.5 mm), then the copper-edge repair
        # pulls the pad inside the shrunk outline (0.501 mm) — clean
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        assert result.displacements == {"R1": 2_001_000}

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
        assert result.unlegalized_refs == ()
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
        assert result.unlegalized_refs == ()
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
        # The crowded valve socket: the repair's first strictly-better
        # move trades the overlap-plus-two-shorts cluster for a single
        # new overlap one part over — and from there every escape from
        # U1's overlap lands back on R2 (the closed forms are
        # per-finding; no candidate walks the diagonal away from both).
        # The bounce is courtyard-to-courtyard, so #95's pad moves
        # never get their turn: the attempt caps end it, both parts are
        # restored and named, and the residual shorts are the honest
        # report (ADR-0012).
        board = beat_overlap_board(build_board(load_ecc83_spec()))
        result = legalize(board)
        assert set(result.unlegalized_refs) == {"R1", "R2"}
        assert result.displacements == {}
        residual = {finding.type for finding in error_findings(result.board)}
        assert residual == {
            ViolationType.COURTYARDS_OVERLAP,
            ViolationType.SHORTING_ITEMS,
        }


class TestCopperRepairs:
    """The pad-copper repair vocabulary (#95, ADR-0012 as amended by
    #83): pad pairs push out along one axis to ``clearance_nm +
    _MARGIN_NM`` — past full clearance, so a short cannot be "cleared"
    into an equal-count ``clearance`` finding — the whole copper box
    clamps inside the shrunk outline, intra-footprint pairs park with
    the part movable, and the pairs come from the checker's own records
    (``Evaluator.pad_pairs``), never a second predicate."""

    def test_a_pad_gap_pushes_out_to_clearance_plus_margin(self):
        board = placed(placed(copper_board("R1", "R2"), "R1", 10, 10), "R2", 11.4, 10)
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        # one move: the missing 0.1 mm plus the margin
        assert result.displacements == {"R1": 101_000}

    def test_overlapping_pads_end_fully_clean(self):
        # a short, not a gap: the push-out aims past full clearance, so
        # the repair cannot settle into an equal-count clearance trade.
        # Two -y moves: the touching outer pads' clearance (0.201 mm),
        # then the yard push (1.1 mm) — the final pad-box gap is 0.801
        # mm, and nothing the probe could bounce on remains
        board = placed(placed(copper_board("R1", "R2"), "R1", 10, 10), "R2", 10.5, 10)
        types = {finding.type for finding in error_findings(board)}
        # the short, the touching outer pads' 0-gap clearance, the yards
        assert types == {
            ViolationType.SHORTING_ITEMS,
            ViolationType.CLEARANCE,
            ViolationType.COURTYARDS_OVERLAP,
        }
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        assert result.displacements == {"R1": 1_301_000}

    def test_same_net_pairs_take_no_move(self):
        nets = (NetIR(name="n1", nodes=(NodeIR("R1", "2"), NodeIR("R2", "1"))),)
        board = placed(
            placed(copper_board("R1", "R2", nets=nets), "R1", 10, 10), "R2", 11.4, 10
        )
        assert error_findings(board) == ()  # the same pair, one net — exempt
        result = legalize(board)
        assert result.board is board

    def test_net_tie_groups_exempt_their_own_pair(self):
        board = placed(copper_board("A", footprint_irs=TIE), "A", 10, 10)
        assert error_findings(board) == ()  # the pads overlap; the group is silent
        result = legalize(board)
        assert result.board is board

    def test_near_edge_pad_pulled_inside_the_shrunk_outline(self):
        board = placed(copper_board("R1"), "R1", 0.95, 10)
        # pad 1's face sits 0.3 mm from the left edge line
        (finding,) = error_findings(board)
        assert finding.type is ViolationType.COPPER_EDGE_CLEARANCE
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.displacements == {"R1": 201_000}

    def test_requirement_edge_mount_stated_edge_takes_no_move(self):
        board = copper_board("R1").with_constraint(
            Constraint(
                id="west",
                kind=Requirement(),
                relation=EdgeMount(target="placement:R1", edge=Edge.LEFT),
            )
        )
        board = placed(board, "R1", 0.65, 10)
        assert error_findings(board) == ()  # flush; the stated edge is exempt
        result = legalize(board)
        assert result.board is board
        (r1,) = result.board.placements
        assert r1.pos.x == 650_000

    def test_preference_edge_mount_exempts_nothing_gets_pulled_in(self):
        board = copper_board("R1").with_constraint(
            Constraint(
                id="west",
                kind=Preference(),
                relation=EdgeMount(target="placement:R1", edge=Edge.LEFT),
            )
        )
        board = placed(board, "R1", 0.65, 10)
        (finding,) = error_findings(board)
        assert finding.type is ViolationType.COPPER_EDGE_CLEARANCE
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.displacements == {"R1": 501_000}

    def test_intra_footprint_pair_alone_returns_the_same_board(self):
        # the self-pair alone: placement-invariant, so the repairer has
        # nothing to do and identity holds — the same object back, the
        # ref named (ADR-0012 as amended by #83). The movable-part half
        # — the self-pair not stranding the part's other repairs — is
        # TestContract's unrepairable-finding pin.
        board = placed(copper_board("A", footprint_irs=SSOP), "A", 10, 10)
        (finding,) = error_findings(board)
        assert finding.type is ViolationType.CLEARANCE
        assert finding.offending_refs == ("placement:A", "placement:A")
        result = legalize(board)
        assert result.board is board
        assert result.unlegalized_refs == ("A",)
        assert result.displacements == {}

    def test_a_raised_clearance_names_the_self_pair_too(self):
        # the spec's TSSOP world: pads comfortably clean at the 0.2 mm
        # rule, self-pairing only once the board's rule rises past their
        # 0.25 mm gap — placement-invariant either way
        board = placed(copper_board("A", footprint_irs=SSOP_WIDE), "A", 10, 10)
        assert error_findings(board) == ()  # clean at the default rule
        raised_board = placed(
            copper_board(
                "A", footprint_irs=SSOP_WIDE, rules=DesignRules(clearance_nm=300_000)
            ),
            "A",
            10,
            10,
        )
        (finding,) = error_findings(raised_board)
        assert finding.type is ViolationType.CLEARANCE
        assert finding.offending_refs == ("placement:A", "placement:A")
        result = legalize(raised_board)
        assert result.board is raised_board
        assert result.unlegalized_refs == ("A",)
        assert result.displacements == {}

    def test_a_bound_smaller_than_the_move_restores_exactly(self):
        board = placed(placed(copper_board("R1", "R2"), "R1", 10, 10), "R2", 11.4, 10)
        result = legalize(board, max_displacement=1_000)
        assert result.displacements == {}
        assert result.unlegalized_refs == ("R1", "R2")
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["R1"].pos == Point(mm(10), mm(10))
        assert placed_map["R2"].pos == Point(mm(11.4), mm(10))

    def test_a_locked_offender_lets_its_partner_move(self):
        board = placed(
            placed(copper_board("R1", "R2"), "R1", 10, 10, locked=True), "R2", 11.4, 10
        )
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.displacements == {"R2": 101_000}
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["R1"].pos == Point(mm(10), mm(10))
        assert placed_map["R1"].locked is True

    def test_a_back_side_pad_prices_against_a_cu_barrel(self):
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(
                    comps=(
                        CompIR(ref="T1", entry_name="THT"),
                        CompIR(ref="S1", entry_name="SMD"),
                    ),
                    nets=(),
                ),
                footprint_irs={**THT, **SMD},
            )
        )
        board = placed(board, "T1", 10, 10)
        board = placed(board, "S1", 10.3, 10, side="B.Cu")
        # cross-side courtyards never overlap; the barrel's *.Cu shares
        # the back pad's B.Cu — the pair prices and shorts
        (finding,) = error_findings(board)
        assert finding.type is ViolationType.SHORTING_ITEMS
        result = legalize(board)
        assert error_findings(result.board) == ()
        assert result.displacements == {"S1": 651_000}

    def test_custom_pad_push_out_sizes_on_the_full_copper_box(self):
        board = placed(
            placed(copper_board("R1", "R2", footprint_irs=JP), "R1", 10, 10),
            "R2",
            10,
            10.85,
        )
        # the hull boxes overlap 0.15 mm while the courtyards stay clear
        (finding,) = error_findings(board)
        assert finding.type is ViolationType.SHORTING_ITEMS
        result = legalize(board)
        assert error_findings(result.board) == ()
        # the −y push carries the full 2 × 1 mm extents 0.201 mm apart
        assert result.displacements == {"R1": 351_000}


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
        # A's locked position stays; B escapes and then finishes the pad
        # clearance — the board ends fully clean
        assert error_findings(result.board) == ()
        assert result.unlegalized_refs == ()
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["A"].pos == Point(mm(10), mm(10))
        assert placed_map["A"].locked is True
        assert set(result.displacements) == {"B"}

    def test_locked_illegal_part_is_a_named_failure(self):
        board = placed(tiny_board("A"), "A", -1, 10, locked=True)
        result = legalize(board)
        # the 1 mm pad sits wholly beyond the left edge line — the edge
        # check stays silent (the ADR-0018 probe's pinned reading), so
        # containment alone names the locked part
        (finding,) = error_findings(result.board)
        assert finding.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
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
        # A's containment stays — it is locked, the honest named failure;
        # B escaped the overlap and the pad clearance finished clean
        (finding,) = error_findings(result.board)
        assert finding.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        assert finding.offending_refs == ("placement:A",)
        assert result.unlegalized_refs == ("A",)
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
        # patched through the drc module alone: the Evaluator reads the
        # registry at call time, so no second patch site exists (#66)

        board = placed(tiny_board("R1"), "R1", 10, 10)
        result = legalize(board)
        assert result.unlegalized_refs == ("R1",)
        assert result.displacements == {}
        assert result.board.placements[0].pos == Point(mm(10), mm(10))


class TestRotationIsUntouched:
    """Rotation never changes in legalization (ADR-0012, narrowed by
    ADR-0021): rotation is the search's decision, not a repair — a 90°
    turn has no measure in integer nanometres under the Euclidean
    displacement bound, so counting one as zero movement would quietly
    weaken the small-movement promise. ``legalize`` neither reads nor
    writes rotation: a part repairable only by turning fails by name.

    The world adds the SWIVEL part — pads ((0, 0) and (2 mm, 0), unit
    circles) under the yard that covers them ((-1, -1) to (3, 1) mm, so
    4 × 2 mm at rotation 0 and 2 × 4 mm at 90°) — beside this file's
    TINY parts (1 × 1 mm yards).
    """

    @classmethod
    def swivel_board(cls, *refs: str) -> Board:
        """An unplaced Board over the given comps — SWIVEL for ``P*``."""
        return build_board(
            make_spec(
                netlist_ir=NetlistIR(
                    comps=tuple(
                        CompIR(
                            ref=ref,
                            entry_name="SWIVEL" if ref.startswith("P") else "TINY",
                        )
                        for ref in refs
                    ),
                    nets=(),
                ),
                footprint_irs=SWIVEL_WORLD,
            )
        )

    @classmethod
    def boxed_board(cls) -> Board:
        """P1 on a board too narrow for its current rotation: the SWIVEL
        yard is 4 × 2 mm at rotation 0 on a 2.5 × 4.5 mm outline, so
        every rot-0 position pokes past the right edge (4 mm of yard
        cannot fit 2.5 mm of board) — while the 90° turn fits with
        margin. The one containment candidate slides P1 past the left
        edge instead; no translation clears, and legalize never tries
        the turn. No walls: the outline is the cage, airtight by
        arithmetic."""
        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="P1", entry_name="SWIVEL"),),
                nets=(),
            ),
            footprint_irs=SWIVEL_WORLD,
            outline=Rectangle(Point(0, 0), Point(mm(2) + 500_000, mm(4) + 500_000)),
            stackup=("F.Cu", "B.Cu"),
        )
        board = build_board(spec)
        return board.with_placement("P1", mm(1) + 250_000, mm(3), 0, "F.Cu")

    def test_repair_slides_but_never_turns(self):
        """A rotated part with a repairable overlap: legalize slides the
        mover in translation and the board's rotations come back exactly
        as they went in."""
        board = self.swivel_board("P1", "T1")
        board = board.with_placement("P1", mm(10), mm(10), 270, "F.Cu")
        board = board.with_placement("T1", mm(10) + 500_000, mm(10), 0, "F.Cu")
        assert error_findings(board)  # the overlap the repair clears
        result = legalize(board)
        assert result.unlegalized_refs == ()
        assert error_findings(result.board) == ()
        assert [
            (placement.ref, placement.rotation) for placement in result.board.placements
        ] == [("P1", 270), ("T1", 0)]

    def test_a_part_repairable_only_by_turning_fails_by_name(self):
        """The too-narrow board: P1's containment finding has exactly one
        closed-form candidate, and the probe rejects it (the slide pokes
        past the left edge instead); the attempts run out, and P1 ends
        exactly where it was, rotation 0, named in
        ``unlegalized_refs``. The counterfactual is asserted beside it:
        the same position at rotation 90 is a legal board, so the named
        failure is the turn's absence, not the board's."""
        board = self.boxed_board()
        assert error_findings(board)  # the containment finding exists
        turned = board.with_placement("P1", mm(1) + 250_000, mm(3), 90, "F.Cu")
        assert error_findings(turned) == ()  # ...and only the turn clears
        result = legalize(board)
        assert result.unlegalized_refs == ("P1",)
        placement = result.board.placement("P1")
        assert (placement.pos.x, placement.pos.y, placement.rotation) == (
            mm(1) + 250_000,
            mm(3),
            0,
        )
        assert result.displacements == {}  # nothing moved at all

    def test_displacement_bound_measures_translation_only(self):
        """A 90° turn is zero translation — so if the bound ever read a
        turn as free, a part could rotate its way out of a too-narrow
        board. It cannot: the candidates are translations and P1's fail
        on the outline at any bound; the pin is that a turned pose never
        appears."""
        result = legalize(self.boxed_board(), max_displacement=mm(100))
        placement = result.board.placement("P1")
        assert placement.rotation == 0
        assert result.unlegalized_refs == ("P1",)
