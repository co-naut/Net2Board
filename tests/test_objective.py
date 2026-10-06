"""net2board.objective — the pure integer-nm ranking (ADR-0014, #43).

``evaluate(board)`` is the full-recompute reference implementation the
incremental Evaluator (#44, ADR-0016) must match bit for bit. Here it is
pinned against hand-built boards with known spans and shortfalls:

- **NetSpan** — the numeraire: half-perimeter of the AABB of a net's
  placed pad centers, one term per net (degree ≤ 1 still reports),
  PARTIAL when a pin is unplaced, weighted by Criticality.
- **Hinged Preferences** — one term per Preference constraint, zero when
  satisfied, ``weight × shortfall`` past the bound, reading the same
  geometry measures the Requirement checks read (#42's single
  definition): a board that fires ``PROXIMITY_UNMET`` measures a
  positive hinge, and a Preferences-only board produces no Violation.
- **Criticality** — scales its net's span term; weight 0 keeps the term
  measured but contributes nothing; two ids on one net resolve last-stated.
- **Shape** — frozen, integer-only, canonically ``(kind.value, ref)``
  sorted, deterministic down to ``asdict``, and no *cost* anywhere.

The local world: ``TINY`` is a one-pad footprint with a ±0.5 mm rectangle
courtyard (the #42 fixture — every hinge the constraint's alone);
``VOID`` has neither pads nor courtyard (the unmeasurable member);
``wide_board()`` carries a three-pin net for a known multi-pin span.
"""

import dataclasses
import json

import pytest

from conftest import fresh_board, load_ecc83_spec, make_spec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.geometry import Point, Rectangle
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import (
    Constraint,
    Criticality,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Preference,
    Proximity,
    Region,
    Requirement,
)
from net2board.objective import (
    TERMS,
    Objective,
    ObjectiveTerm,
    TermKind,
    TermStatus,
    evaluate,
)

X = 10_000_000
Y = 10_000_000

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


def wide_board():
    """Three TINY comps on one three-pin net WIDE, no placements."""
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="R1", entry_name="TINY"),
                CompIR(ref="R2", entry_name="TINY"),
                CompIR(ref="R3", entry_name="TINY"),
            ),
            nets=(NetIR(name="WIDE", nodes=(("R1", "1"), ("R2", "1"), ("R3", "1"))),),
        ),
        footprint_irs={
            "TINY": FootprintIR(
                entry_name="TINY", pads=TINY_PAD, courtyard=TINY_COURTYARD
            )
        },
    )
    return build_board(spec)


def void_board():
    """R1, R2 measurable TINY comps; R3 has neither courtyard nor pads."""
    spec = make_spec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="R1", entry_name="TINY"),
                CompIR(ref="R2", entry_name="TINY"),
                CompIR(ref="R3", entry_name="VOID"),
            ),
            nets=(),
        ),
        footprint_irs={
            "TINY": FootprintIR(
                entry_name="TINY", pads=TINY_PAD, courtyard=TINY_COURTYARD
            ),
            "VOID": FootprintIR(entry_name="VOID", pads=(), courtyard=None),
        },
    )
    return build_board(spec)


def proximity(bound=7_620_000, same_side=False, kind=None, id="cap-near", weight=1):
    return Constraint(
        id=id,
        kind=kind or Preference(),
        relation=Proximity(
            ref_a="R1",
            pad_a="1",
            ref_b="R2",
            pad_b="1",
            bound_nm=bound,
            same_side=same_side,
        ),
        weight=weight,
    )


def region(rect, target="placement:R1", kind=None, id="in-region", weight=1):
    return Constraint(
        id=id,
        kind=kind or Preference(),
        relation=Region(target=target, rect=rect),
        weight=weight,
    )


def edge_mount(
    edge=Edge.RIGHT,
    overhang=None,
    target="placement:R1",
    kind=None,
    id="row-mount",
    weight=1,
):
    return Constraint(
        id=id,
        kind=kind or Preference(),
        relation=EdgeMount(target=target, edge=edge, max_overhang_nm=overhang),
        weight=weight,
    )


def keep_together(
    width, height, target="group:pair", kind=None, id="pair-fit", weight=1
):
    return Constraint(
        id=id,
        kind=kind or Preference(),
        relation=KeepTogether(target=target, max_width_nm=width, max_height_nm=height),
        weight=weight,
    )


def criticality(net="GND", weight=1, id="gnd-pull"):
    return Constraint(
        id=id,
        kind=Preference(),
        relation=Criticality(net=net),
        weight=weight,
    )


def term(objective, ref):
    return next(t for t in objective.terms if t.ref == ref)


def gnd_pair_board():
    """R1 (10M, 10M), R2 (10M, 20M) — GND pad 2s 10 mm apart vertically."""
    return (
        fresh_board()
        .with_placement("R1", X, Y, 0, "F.Cu")
        .with_placement("R2", X, Y + 10_000_000, 0, "F.Cu")
    )


class TestNetSpan:
    def test_unplaced_nets_report_zero_partially(self):
        objective = evaluate(fresh_board())
        gnd = term(objective, "net:GND")
        assert (gnd.raw_nm, gnd.weighted_nm, gnd.status) == (0, 0, TermStatus.PARTIAL)
        n1 = term(objective, "net:N1")
        assert (n1.raw_nm, n1.weighted_nm, n1.status) == (0, 0, TermStatus.PARTIAL)
        assert gnd.weight == 1
        assert objective.total_nm == 0

    def test_span_is_the_half_perimeter_of_placed_pad_centers(self):
        objective = evaluate(gnd_pair_board())
        gnd = term(objective, "net:GND")
        # pad 2 sits 7.62 mm right of each anchor: centers (17.62M, 10M) and (17.62M, 20M)
        assert (gnd.raw_nm, gnd.weighted_nm) == (10_000_000, 10_000_000)
        assert gnd.status is TermStatus.ACTIVE

    def test_rotation_enters_through_the_pad_center(self):
        # R2 rot 90: pad 2's local (7.62M, 0) becomes (0, −7.62M) — the center
        # lands 17.38 mm below R1's, though the anchors are 15 mm apart
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 17_620_000, 25_000_000, 90, "F.Cu")
        )
        gnd = term(evaluate(board), "net:GND")
        assert gnd.raw_nm == 7_380_000
        assert gnd.status is TermStatus.ACTIVE

    def test_back_side_mirror_enters_through_the_pad_center(self):
        # R2 on B.Cu mirrors local x: pad 2 sits 7.62 mm left of its anchor
        board = (
            fresh_board()
            .with_placement("R1", 12_000_000, Y, 0, "F.Cu")
            .with_placement("R2", 12_000_000, 20_000_000, 0, "B.Cu")
        )
        gnd = term(evaluate(board), "net:GND")
        # centers (19.62M, 10M) and (4.38M, 20M): 15.24M + 10M
        assert gnd.raw_nm == 25_240_000

    def test_single_pin_net_is_a_zero_span_term(self):
        objective = evaluate(gnd_pair_board())
        n1 = term(objective, "net:N1")
        assert (n1.raw_nm, n1.weighted_nm, n1.status) == (0, 0, TermStatus.ACTIVE)

    def test_unplaced_pin_measures_over_placed_pins_partially(self):
        board = fresh_board().with_placement("R1", X, Y, 0, "F.Cu")
        gnd = term(evaluate(board), "net:GND")
        # one placed center: the span degenerates to 0, but the term is honest
        assert (gnd.raw_nm, gnd.status) == (0, TermStatus.PARTIAL)

    def test_multi_pin_span_is_known_arithmetic(self):
        board = (
            wide_board()
            .with_placement("R1", 0, 0, 0, "F.Cu")
            .with_placement("R2", 3_000_000, 0, 0, "F.Cu")
            .with_placement("R3", 0, 4_000_000, 0, "F.Cu")
        )
        wide = term(evaluate(board), "net:WIDE")
        # centers (0,0), (3M, 0), (0, 4M): width 3M + height 4M
        assert (wide.raw_nm, wide.status) == (7_000_000, TermStatus.ACTIVE)

    def test_multi_pin_partial_measures_the_placed_subset(self):
        board = (
            wide_board()
            .with_placement("R1", 0, 0, 0, "F.Cu")
            .with_placement("R2", 3_000_000, 0, 0, "F.Cu")
        )
        wide = term(evaluate(board), "net:WIDE")
        assert (wide.raw_nm, wide.status) == (3_000_000, TermStatus.PARTIAL)


class TestCriticality:
    def test_criticality_scales_the_net_span_term(self):
        weighted = evaluate(gnd_pair_board().with_constraint(criticality(weight=5)))
        plain = evaluate(gnd_pair_board())
        assert term(weighted, "net:GND").weight == 5
        assert term(weighted, "net:GND").weighted_nm == 50_000_000
        assert weighted.total_nm == 50_000_000
        # the ranking consequence: same geometry, heavier pull ranks worse
        assert weighted.total_nm > plain.total_nm

    def test_weight_zero_keeps_the_term_measured(self):
        objective = evaluate(gnd_pair_board().with_constraint(criticality(weight=0)))
        gnd = term(objective, "net:GND")
        assert (gnd.raw_nm, gnd.weighted_nm, gnd.status) == (
            10_000_000,
            0,
            TermStatus.ACTIVE,
        )
        assert objective.total_nm == 0

    def test_last_stated_criticality_wins_for_a_net(self):
        board = (
            gnd_pair_board()
            .with_constraint(criticality(weight=2, id="pull-a"))
            .with_constraint(criticality(weight=7, id="pull-b"))
        )
        assert term(evaluate(board), "net:GND").weight == 7

    def test_criticality_is_never_a_hinge_term(self):
        objective = evaluate(gnd_pair_board().with_constraint(criticality(weight=5)))
        assert [t.ref for t in objective.terms] == ["net:GND", "net:N1"]
        assert all(t.kind is TermKind.NET_SPAN for t in objective.terms)


class TestProximityHinge:
    def test_satisfied_contributes_zero(self):
        board = (
            tiny_board()
            .with_constraint(proximity())
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 7_620_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:cap-near")
        # pad centers exactly at the inclusive bound
        assert (hinge.raw_nm, hinge.weighted_nm, hinge.status) == (
            0,
            0,
            TermStatus.ACTIVE,
        )

    def test_grows_linearly_past_the_bound(self):
        board = (
            tiny_board()
            .with_constraint(proximity(weight=3))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 9_620_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:cap-near")
        assert (hinge.raw_nm, hinge.weighted_nm) == (2_000_000, 6_000_000)
        assert hinge.kind is TermKind.PREFERENCE

    def test_unplaced_endpoint_is_pending(self):
        board = (
            tiny_board()
            .with_constraint(proximity())
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:cap-near")
        assert (hinge.raw_nm, hinge.weighted_nm, hinge.status) == (
            0,
            0,
            TermStatus.PENDING,
        )

    def test_same_side_hinge_is_side_insensitive(self):
        # a cross-side pair under same_side measures its distance like any
        # other: 2 mm apart, bound 7.62 mm — the side predicate is the
        # Requirement check's alone (pinned decision, ADR-0014's formula)
        board = (
            tiny_board()
            .with_constraint(proximity(same_side=True))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 2_000_000, Y, 0, "B.Cu")
        )
        hinge = term(evaluate(board), "constraint:cap-near")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.ACTIVE)


class TestRegionHinge:
    def test_satisfied_contributes_zero(self):
        rect = Rectangle(
            min=Point(9_000_000, 9_000_000), max=Point(11_000_000, 11_000_000)
        )
        board = (
            tiny_board()
            .with_constraint(region(rect))
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:in-region")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.ACTIVE)

    def test_overhang_sums_axis_pokes(self):
        rect = Rectangle(
            min=Point(9_800_000, 9_800_000), max=Point(10_200_000, 10_200_000)
        )
        board = (
            tiny_board()
            .with_constraint(region(rect, weight=2))
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:in-region")
        # the ±0.5 mm courtyard box pokes 0.3 mm past each of the four faces
        assert (hinge.raw_nm, hinge.weighted_nm) == (1_200_000, 2_400_000)

    def test_group_target_sums_members(self):
        rect = Rectangle(
            min=Point(9_000_000, 9_000_000), max=Point(11_000_000, 11_000_000)
        )
        board = (
            tiny_board()
            .with_group(Group(id="g", refs=("R1", "R2")))
            .with_constraint(region(rect, target="group:g"))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 12_000_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:in-region")
        # R1 inside; R2's box (11.5M..12.5M) pokes 1.5 mm past the right face
        assert (hinge.raw_nm, hinge.status) == (1_500_000, TermStatus.ACTIVE)

    def test_partially_placed_group_is_partial(self):
        rect = Rectangle(
            min=Point(9_000_000, 9_000_000), max=Point(11_000_000, 11_000_000)
        )
        board = (
            tiny_board()
            .with_group(Group(id="g", refs=("R1", "R2")))
            .with_constraint(region(rect, target="group:g"))
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:in-region")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PARTIAL)

    def test_no_member_placed_is_pending(self):
        rect = Rectangle(
            min=Point(9_000_000, 9_000_000), max=Point(11_000_000, 11_000_000)
        )
        board = (
            tiny_board()
            .with_group(Group(id="g", refs=("R1", "R2")))
            .with_constraint(region(rect, target="group:g"))
        )
        hinge = term(evaluate(board), "constraint:in-region")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PENDING)

    def test_placed_unmeasurable_member_leaves_the_term_partial(self):
        rect = Rectangle(
            min=Point(9_000_000, 9_000_000), max=Point(11_000_000, 11_000_000)
        )
        board = (
            void_board()
            .with_group(Group(id="g", refs=("R1", "R3")))
            .with_constraint(region(rect, target="group:g"))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R3", 20_000_000, 20_000_000, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:in-region")
        # R1 measures 0; R3 has nothing to measure — measured over a strict
        # subset of the anchors, so PARTIAL flags the skipped member
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PARTIAL)


class TestEdgeMountHinge:
    def test_flush_face_contributes_zero(self):
        board = (
            tiny_board()
            .with_constraint(edge_mount())
            .with_placement("R1", 39_500_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        # the courtyard's right face lands exactly on the 40 mm outline edge
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.ACTIVE)

    def test_short_of_the_edge_is_debt(self):
        board = (
            tiny_board()
            .with_constraint(edge_mount())
            .with_placement("R1", 30_000_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        # the face sits 9.5 mm inside the edge line: the whole distance is debt
        assert hinge.raw_nm == 9_500_000

    def test_past_the_band_is_the_excess(self):
        board = (
            tiny_board()
            .with_constraint(edge_mount(overhang=1_000_000))
            .with_placement("R1", 40_900_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        # the face is 1.4 mm past the edge; 1 mm is sanctioned, 0.4 mm is debt
        assert hinge.raw_nm == 400_000

    def test_within_the_band_contributes_zero(self):
        board = (
            tiny_board()
            .with_constraint(edge_mount(overhang=1_000_000))
            .with_placement("R1", 40_400_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.ACTIVE)

    def test_none_overhang_is_a_band_of_exactly_zero(self):
        board = (
            tiny_board()
            .with_constraint(edge_mount(overhang=None))
            .with_placement("R1", 40_400_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        assert hinge.raw_nm == 900_000

    def test_group_target_sums_member_debt(self):
        board = (
            tiny_board()
            .with_group(Group(id="g", refs=("R1", "R2")))
            .with_constraint(edge_mount(target="group:g"))
            .with_placement("R1", 39_500_000, Y, 0, "F.Cu")
            .with_placement("R2", 38_000_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        # R1 flush; R2's face 1.5 mm short of the edge
        assert (hinge.raw_nm, hinge.status) == (1_500_000, TermStatus.ACTIVE)

    def test_unplaced_target_is_pending(self):
        board = tiny_board().with_constraint(edge_mount())
        hinge = term(evaluate(board), "constraint:row-mount")
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PENDING)

    def test_placed_unmeasurable_member_leaves_the_term_partial(self):
        board = (
            void_board()
            .with_group(Group(id="g", refs=("R1", "R3")))
            .with_constraint(edge_mount(target="group:g"))
            .with_placement("R1", 39_500_000, Y, 0, "F.Cu")
            .with_placement("R3", 20_000_000, 20_000_000, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:row-mount")
        # R1 flush (0); R3 has neither courtyard nor pads — a strict subset
        # of the anchors measured
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PARTIAL)


class TestKeepTogetherHinge:
    def pair(self, constraint):
        return (
            tiny_board()
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(constraint)
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 12_000_000, Y, 0, "F.Cu")
        )

    def test_fitting_extent_contributes_zero(self):
        hinge = term(
            evaluate(self.pair(keep_together(4_000_000, 2_000_000))),
            "constraint:pair-fit",
        )
        # union box (9.5M..12.5M) × (9.5M..10.5M): 3 mm × 1 mm
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.ACTIVE)

    def test_exact_fit_contributes_zero(self):
        hinge = term(
            evaluate(self.pair(keep_together(3_000_000, 1_000_000))),
            "constraint:pair-fit",
        )
        assert hinge.raw_nm == 0

    def test_width_over_is_the_excess(self):
        hinge = term(
            evaluate(self.pair(keep_together(2_000_000, 5_000_000))),
            "constraint:pair-fit",
        )
        assert (hinge.raw_nm, hinge.weighted_nm) == (1_000_000, 1_000_000)

    def test_height_over_is_the_excess(self):
        hinge = term(
            evaluate(self.pair(keep_together(5_000_000, 500_000))),
            "constraint:pair-fit",
        )
        assert hinge.raw_nm == 500_000

    def test_both_axes_over_sum(self):
        hinge = term(
            evaluate(self.pair(keep_together(2_000_000, 500_000, weight=2))),
            "constraint:pair-fit",
        )
        assert (hinge.raw_nm, hinge.weighted_nm) == (1_500_000, 3_000_000)

    def test_unplaced_member_pending_the_whole_extent(self):
        board = (
            tiny_board()
            .with_group(Group(id="pair", refs=("R1", "R2")))
            .with_constraint(keep_together(2_000_000, 5_000_000))
            .with_placement("R1", X, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:pair-fit")
        # an extent cannot be measured short a member — never a partial guess
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PENDING)

    def test_unmeasurable_member_pending_the_whole_extent(self):
        board = (
            void_board()
            .with_group(Group(id="pair", refs=("R1", "R2", "R3")))
            .with_constraint(keep_together(20_000_000, 20_000_000, target="group:pair"))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", 12_000_000, Y, 0, "F.Cu")
            .with_placement("R3", 20_000_000, 20_000_000, 0, "F.Cu")
        )
        hinge = term(evaluate(board), "constraint:pair-fit")
        # R3 has neither courtyard nor pads: nothing to measure, void — not a guess
        assert (hinge.raw_nm, hinge.status) == (0, TermStatus.PENDING)


class TestTheHingeReusesTheChecksMeasure:
    def test_a_fired_requirement_is_a_positive_hinge(self):
        requirement_board = (
            tiny_board()
            .with_constraint(proximity(kind=Requirement()))
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 9_620_000, Y, 0, "F.Cu")
        )
        findings = [
            v
            for v in run_drc(requirement_board)
            if v.type is ViolationType.PROXIMITY_UNMET
        ]
        assert len(findings) == 1

        preference_board = (
            tiny_board()
            .with_constraint(proximity())
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X + 9_620_000, Y, 0, "F.Cu")
        )
        hinge = term(evaluate(preference_board), "constraint:cap-near")
        assert hinge.raw_nm == 2_000_000
        # and Preferences never produce Violations (ADR-0011)
        assert run_drc(preference_board) == ()


class TestShape:
    def test_terms_come_from_the_ordered_registry(self):
        assert TERMS == (TermKind.NET_SPAN, TermKind.PREFERENCE)
        objective = evaluate(gnd_pair_board())
        assert all(term.kind in TERMS for term in objective.terms)

    def test_terms_are_canonically_sorted(self):
        board = (
            fresh_board()
            .with_constraint(proximity(id="z-near"))
            .with_constraint(proximity(id="a-near"))
        )
        assert [t.ref for t in evaluate(board).terms] == [
            "net:GND",
            "net:N1",
            "constraint:a-near",
            "constraint:z-near",
        ]

    def test_total_is_the_sum_of_weighted_terms(self):
        board = (
            gnd_pair_board()
            .with_constraint(criticality(weight=5))
            .with_constraint(proximity(weight=3))
        )
        objective = evaluate(
            board.with_placement("R1", X, Y, 0, "F.Cu").with_placement(
                "R2", X + 9_620_000, Y, 0, "F.Cu"
            )
        )
        # GND 5×9.62M (pad 2s 9.62 mm apart) + hinge 3×2M + N1 0
        assert objective.total_nm == 5 * 9_620_000 + 3 * 2_000_000

    def test_same_board_same_objective(self):
        board = gnd_pair_board().with_constraint(criticality(weight=5))
        first = evaluate(board)
        second = evaluate(board)
        assert first == second
        assert dataclasses.asdict(first) == dataclasses.asdict(second)
        render = lambda o: json.dumps(
            dataclasses.asdict(o), indent=2, sort_keys=True, default=lambda v: v.value
        )
        assert render(first) == render(second)

    def test_the_api_is_frozen_and_integer_only(self):
        objective = evaluate(gnd_pair_board().with_constraint(criticality(weight=5)))
        with pytest.raises(dataclasses.FrozenInstanceError):
            objective.total_nm = 1
        with pytest.raises(dataclasses.FrozenInstanceError):
            objective.terms[0].weight = 2
        assert isinstance(objective.total_nm, int) and not isinstance(
            objective.total_nm, bool
        )
        for t in objective.terms:
            for value in (t.weight, t.raw_nm, t.weighted_nm):
                assert isinstance(value, int) and not isinstance(value, bool)

    def test_nothing_names_cost(self):
        objective = evaluate(gnd_pair_board().with_constraint(criticality(weight=5)))
        fields = set(dataclasses.asdict(objective)) | set(
            dataclasses.asdict(objective.terms[0])
        )
        assert "cost" not in {key.lower() for key in fields}

    def test_a_board_without_intent_collapses_to_empty(self):
        objective = evaluate(tiny_board())
        assert objective == Objective(total_nm=0, terms=())

    def test_ecc83_unplaced_is_all_partial_at_zero(self):
        board = build_board(load_ecc83_spec())
        objective = evaluate(board)
        assert len(objective.terms) == len(board.nets)
        assert all(
            t.kind is TermKind.NET_SPAN
            and t.status is TermStatus.PARTIAL
            and t.raw_nm == 0
            for t in objective.terms
        )
        assert objective.total_nm == 0


def test_objective_term_is_a_documented_shape():
    """The ADR-0014 field order: kind, ref, weight, raw_nm, weighted_nm, status."""
    fields = [f.name for f in dataclasses.fields(ObjectiveTerm)]
    assert fields == ["kind", "ref", "weight", "raw_nm", "weighted_nm", "status"]
    assert [f.name for f in dataclasses.fields(Objective)] == ["total_nm", "terms"]
