"""The private incremental Evaluator — bit-for-bit with full recompute (#44, ADR-0016).

Whitebox by construction: the Evaluator is solver-private (never exported,
absent from every public signature), so these tests import
``net2board.solver._evaluator`` directly. The contract under test:

- after **every probe and every commit**, the incremental Violations and
  Objective equal a full ``run_drc`` / ``evaluate`` on the equivalent Board
  — the equivalent Board being the input with the same ``with_placement``
  applied (the public construction path);
- the work per probe is bounded by the moved placement's locality,
  asserted in operation counts (``shapes_overlap`` tests and NetSpan
  recomputes), scaling with n and never n² — never wall clock (the fast
  tier has no timer in it, per ADR-0016).

The local world (``dense_board``, deliberately not a real circuit): eight
TINY parts (one pad, ±0.5 mm rectangle courtyard), a WIDE part (two pads,
the RES-like rectangle courtyard), a NOYARD part (the pad-bounding-box
fallback), a BARE part (no pads, no courtyard — nothing to measure), and a
ROUND part (circle courtyard, on ``B.Cu`` — the cross-side case). Nets
cover multi-pin spans, a single-pin net, and two PARTIAL nets held PARTIAL
by unplaced ``T7``/``T8``. Constraints cover every relation in both kinds
plus Criticality (two stated on one net — last wins, ADR-0014). ``T3`` is
authored onto ``W1`` (one courtyard overlap), ``T4`` hangs 1.5 mm off the
left edge under a 0.5 mm Requirement EdgeMount (unmet mount, silenced
containment — ADR-0018's interplay), and one group is spread past its
KeepTogether bound. The seeded walk then moves, places, and merely probes
its way through 200 candidates and must never drift.
"""

import math
import random

import pytest

from conftest import OUTLINE, load_ecc83_spec, make_spec
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import Severity, ViolationType, run_drc
from net2board.examples.ecc83_placements import EDGE_MOUNTS, PLACEMENTS
from net2board.geometry import Circle, Point, Rectangle
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
from net2board.objective import TermKind, evaluate
from net2board.solver._evaluator import Evaluator

CONSTRAINT_TYPES = (
    ViolationType.PROXIMITY_UNMET,
    ViolationType.REGION_UNMET,
    ViolationType.EDGE_MOUNT_UNMET,
    ViolationType.KEEP_TOGETHER_UNMET,
)


def mm(value: int) -> int:
    return value * 1_000_000


UNIT_PAD = (
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

WIDE_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(mm(2), mm(2)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(mm(8), 0),
        size=(mm(2), mm(2)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
WIDE_YARD = Rectangle(Point(-mm(1), -mm(2)), Point(mm(9), mm(2)))

ROUND_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(mm(2), mm(2)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(mm(4), 0),
        size=(mm(2), mm(2)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
ROUND_YARD = Circle(center=Point(mm(2), 0), radius_sq=mm(2) ** 2, end=Point(mm(4), 0))

DENSE_FOOTPRINTS = {
    "TINY": FootprintIR(entry_name="TINY", pads=UNIT_PAD, courtyard=TINY_YARD),
    "WIDE": FootprintIR(entry_name="WIDE", pads=WIDE_PADS, courtyard=WIDE_YARD),
    "NOYARD": FootprintIR(entry_name="NOYARD", pads=UNIT_PAD, courtyard=None),
    "BARE": FootprintIR(entry_name="BARE", pads=(), courtyard=None),
    "ROUND": FootprintIR(entry_name="ROUND", pads=ROUND_PADS, courtyard=ROUND_YARD),
}

DENSE_COMPS = (
    CompIR(ref="T1", entry_name="TINY"),
    CompIR(ref="T2", entry_name="TINY"),
    CompIR(ref="T3", entry_name="TINY"),
    CompIR(ref="T4", entry_name="TINY"),
    CompIR(ref="T5", entry_name="TINY"),
    CompIR(ref="T6", entry_name="TINY"),
    CompIR(ref="T7", entry_name="TINY"),
    CompIR(ref="T8", entry_name="TINY"),
    CompIR(ref="W1", entry_name="WIDE"),
    CompIR(ref="N1", entry_name="NOYARD"),
    CompIR(ref="B1", entry_name="BARE"),
    CompIR(ref="C1", entry_name="ROUND"),
)

DENSE_NETS = (
    NetIR(name="BUS", nodes=(("T1", "1"), ("W1", "1"), ("T7", "1"))),
    NetIR(name="GND", nodes=(("W1", "2"), ("T8", "1"))),
    NetIR(name="LONE", nodes=(("T2", "1"),)),
    NetIR(name="SPAN", nodes=(("T3", "1"), ("T4", "1"))),
)


def _dense_constraint(id: str, kind, relation, weight: int = 1) -> Constraint:
    return Constraint(id=id, kind=kind, relation=relation, weight=weight)


DENSE_CONSTRAINTS = (
    _dense_constraint(
        "prox-req",
        Requirement(),
        Proximity(ref_a="T1", pad_a="1", ref_b="W1", pad_b="1", bound_nm=mm(3)),
    ),
    _dense_constraint(
        "prox-ss",
        Requirement(),
        Proximity(
            ref_a="C1",
            pad_a="1",
            ref_b="T1",
            pad_b="1",
            bound_nm=mm(20),
            same_side=True,
        ),
    ),
    _dense_constraint(
        "prox-wish",
        Preference(),
        Proximity(ref_a="T2", pad_a="1", ref_b="N1", pad_b="1", bound_nm=mm(2)),
        weight=3,
    ),
    _dense_constraint(
        "region-req",
        Requirement(),
        Region(
            target="placement:T3",
            rect=Rectangle(Point(mm(10), mm(8)), Point(mm(25), mm(12))),
        ),
    ),
    _dense_constraint(
        "edge-req",
        Requirement(),
        EdgeMount(target="placement:T4", edge=Edge.LEFT, max_overhang_nm=500_000),
    ),
    _dense_constraint(
        "edge-wish",
        Preference(),
        EdgeMount(target="placement:C1", edge=Edge.RIGHT),
        weight=2,
    ),
    _dense_constraint("crit-bus-1", Preference(), Criticality(net="BUS"), weight=5),
    _dense_constraint("crit-bus-2", Preference(), Criticality(net="BUS"), weight=2),
    _dense_constraint("crit-gnd", Preference(), Criticality(net="GND"), weight=0),
)


def dense_board():
    """The dense hand-built world: T3 on W1, T4 overhanging left, T7/T8
    unplaced, every relation stated in both kinds."""
    board = build_board(
        make_spec(
            netlist_ir=NetlistIR(comps=DENSE_COMPS, nets=DENSE_NETS),
            footprint_irs=DENSE_FOOTPRINTS,
        )
    )
    board = board.with_group(Group(id="g-duo", refs=("T5", "T6")))
    board = board.with_group(Group(id="g-trio", refs=("T4", "N1", "C1")))
    board = board.with_constraint(
        _dense_constraint(
            "region-wish",
            Preference(),
            Region(
                target="group:g-duo",
                rect=Rectangle(Point(mm(5), mm(5)), Point(mm(15), mm(15))),
            ),
        )
    )
    board = board.with_constraint(
        _dense_constraint(
            "kt-req",
            Requirement(),
            KeepTogether(target="group:g-duo", max_width_nm=mm(3), max_height_nm=mm(3)),
        )
    )
    board = board.with_constraint(
        _dense_constraint(
            "kt-wish",
            Preference(),
            KeepTogether(
                target="group:g-trio", max_width_nm=mm(5), max_height_nm=mm(5)
            ),
            weight=2,
        )
    )
    for constraint in DENSE_CONSTRAINTS:
        board = board.with_constraint(constraint)
    rows = (
        ("T1", 10, 10, 0, "F.Cu"),
        ("T2", 12, 10, 0, "F.Cu"),
        ("T3", 18, 10, 0, "F.Cu"),
        ("T4", -1, 4, 0, "F.Cu"),
        ("T5", 20, 20, 0, "F.Cu"),
        ("T6", 30, 24, 0, "F.Cu"),
        ("W1", 18, 10, 0, "F.Cu"),
        ("N1", 12, 13, 0, "F.Cu"),
        ("B1", 2, 2, 0, "F.Cu"),
        ("C1", 34, 6, 0, "B.Cu"),
    )
    for ref, x, y, rotation, side in rows:
        board = board.with_placement(ref, mm(x), mm(y), rotation, side)
    return board


def ecc83_walk_board():
    """The ecc83 fixture under its acceptance table plus extra intent: a
    KeepTogether Requirement, Proximity/Region Preferences, and two
    Criticality statements — the real-footprint walk."""
    board = build_board(load_ecc83_spec())
    for row in PLACEMENTS:
        board = board.with_placement(*row)
    for constraint in EDGE_MOUNTS:
        board = board.with_constraint(constraint)
    board = board.with_group(Group(id="clk", refs=("R1", "R2")))
    board = board.with_constraint(
        Constraint(
            id="kt-clk",
            kind=Requirement(),
            relation=KeepTogether(
                target="group:clk", max_width_nm=mm(6), max_height_nm=mm(6)
            ),
        )
    )
    board = board.with_constraint(
        Constraint(
            id="wish-near",
            kind=Preference(),
            relation=Proximity(
                ref_a="R1", pad_a="1", ref_b="R2", pad_b="1", bound_nm=mm(4)
            ),
            weight=3,
        )
    )
    board = board.with_constraint(
        Constraint(
            id="wish-region",
            kind=Preference(),
            relation=Region(
                target="placement:U1",
                rect=Rectangle(Point(mm(5), mm(5)), Point(mm(35), mm(25))),
            ),
            weight=2,
        )
    )
    board = board.with_constraint(
        Constraint(
            id="hot-net",
            kind=Preference(),
            relation=Criticality(net=board.nets[0].name),
            weight=4,
        )
    )
    return board


def placement_of(board, ref):
    return {p.ref: p for p in board.placements}.get(ref)


def error_count(violations) -> int:
    """The ranking key's legality half — the counted verdict's own
    definition, read off a full verdict tuple."""
    return sum(1 for violation in violations if violation.severity is Severity.ERROR)


def assert_matches_full(evaluator, board):
    """The bit-for-bit contract against the full recompute path."""
    assert evaluator.violations() == run_drc(board)
    assert evaluator.objective() == evaluate(board)


def seeded_walk(board, seed, moves=200, commit_probability=0.75, require_placed=True):
    """A seeded random walk: after every probe and commit, incremental ==
    full on the equivalent Board. Returns the seen-finding counters — the
    walk must actually bite (ADR-0016: a sparse walk passes while testing
    nothing)."""
    rng = random.Random(seed)
    evaluator = Evaluator(board)
    current = board
    refs = tuple(comp.ref for comp in board.components)
    seen = {
        "violation": 0,
        "overlap": 0,
        "constraint": 0,
        "containment": 0,
        "placed": 0,
        "rejected": 0,
    }
    for _ in range(moves):
        ref = rng.choice(refs)
        placement = placement_of(current, ref)
        if placement is not None:
            rotation, side = placement.rotation, placement.side
        else:
            rotation, side = 0, "F.Cu"
        if placement is not None and rng.random() < 0.7:
            pos = Point(
                placement.pos.x + rng.randint(-mm(2), mm(2)),
                placement.pos.y + rng.randint(-mm(2), mm(2)),
            )
        else:
            pos = Point(
                rng.randint(-mm(2), OUTLINE.max.x + mm(2)),
                rng.randint(-mm(2), OUTLINE.max.y + mm(2)),
            )
        probe = evaluator.probe(ref, pos)
        counted = evaluator.probe_key(ref, pos)
        assert counted == (
            error_count(probe.violations),
            probe.objective.total_nm,
        )
        candidate = current.with_placement(ref, pos.x, pos.y, rotation, side)
        expected_violations = run_drc(candidate)
        expected_objective = evaluate(candidate)
        base_objective = evaluate(current)
        assert probe.violations == expected_violations
        assert probe.objective == expected_objective
        assert probe.delta_nm == (expected_objective.total_nm - base_objective.total_nm)
        seen["violation"] += bool(probe.violations)
        seen["overlap"] += any(
            v.type is ViolationType.COURTYARDS_OVERLAP for v in probe.violations
        )
        seen["constraint"] += any(v.type in CONSTRAINT_TYPES for v in probe.violations)
        seen["containment"] += any(
            v.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE for v in probe.violations
        )
        if placement is None:
            seen["placed"] += 1
        if rng.random() < commit_probability:
            evaluator.commit(ref, pos)
            current = candidate
            # the criterion's letter: after every commit, too
            assert_matches_full(evaluator, current)
        else:
            seen["rejected"] += 1
    assert seen["overlap"] > 0, "walk never produced an overlap finding"
    assert seen["constraint"] > 0, "walk never produced a constraint finding"
    assert seen["containment"] > 0, "walk never produced a containment finding"
    if require_placed:
        assert seen["placed"] > 0, "walk never placed an unplaced ref"
    assert_matches_full(evaluator, current)
    return seen


class TestInitialState:
    def test_dense_board_matches_full(self):
        board = dense_board()
        evaluator = Evaluator(board)
        assert_matches_full(evaluator, board)
        # the world is authored to violate: the walk below has something
        # to be local about from move one
        assert evaluator.violations()
        net_span = [t for t in evaluator.objective().terms if "net:" in t.ref]
        assert any(t.status.value == "partial" for t in net_span)

    def test_unplaced_board_matches_full(self):
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(comps=DENSE_COMPS, nets=DENSE_NETS),
                footprint_irs=DENSE_FOOTPRINTS,
            )
        )
        board = board.with_constraint(DENSE_CONSTRAINTS[0])
        assert_matches_full(Evaluator(board), board)
        assert Evaluator(board).violations() == ()  # all anchors pending

    def test_ecc83_board_matches_full(self):
        board = ecc83_walk_board()
        assert_matches_full(Evaluator(board), board)


class TestUnplacedAnchorWording:
    """A probe naming a not-yet-committed anchor still renders the pinned
    wording. The finding builders read descriptions off the committed
    Board — where a solve's initial-placement candidate has no placement
    yet — so the pad-bounding-box fallback must fall back one step further,
    to the netlist component that carries the same footprint. Parity
    contract, ADR-0016: the probe equals the full recompute on the
    equivalent Board."""

    @staticmethod
    def all_unplaced_board(constraint):
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(comps=DENSE_COMPS, nets=DENSE_NETS),
                footprint_irs=DENSE_FOOTPRINTS,
            )
        )
        return board.with_constraint(constraint)

    def test_edge_mount_probe_of_an_unplaced_anchor_matches_full(self):
        board = self.all_unplaced_board(
            DENSE_CONSTRAINTS[4]
        )  # edge-req: T4 LEFT, ≤0.5 mm
        evaluator = Evaluator(board)
        probe = evaluator.probe("T4", Point(mm(-1), mm(4)))  # 1.5 mm overhang: unmet
        candidate = board.with_placement("T4", mm(-1), mm(4), 0, "F.Cu")
        expected = run_drc(candidate)
        assert expected
        assert any(
            violation.type is ViolationType.EDGE_MOUNT_UNMET for violation in expected
        )
        assert probe.violations == expected

    def test_region_probe_of_an_unplaced_anchor_matches_full(self):
        board = self.all_unplaced_board(
            DENSE_CONSTRAINTS[3]
        )  # region-req: T3 in (10,8)..(25,12) mm
        evaluator = Evaluator(board)
        probe = evaluator.probe("T3", Point(mm(1), mm(1)))  # well outside the rect
        candidate = board.with_placement("T3", mm(1), mm(1), 0, "F.Cu")
        expected = run_drc(candidate)
        assert expected
        assert any(
            violation.type is ViolationType.REGION_UNMET for violation in expected
        )
        assert probe.violations == expected


class TestSeededWalk:
    def test_dense_board_walk(self):
        seen = seeded_walk(dense_board(), seed=2026)
        assert seen["rejected"] > 0  # probes without commits leave no trace

    def test_ecc83_walk(self):
        seeded_walk(ecc83_walk_board(), seed=9, require_placed=False)


class TestProbeContract:
    def test_probe_does_not_mutate(self):
        board = dense_board()
        evaluator = Evaluator(board)
        before_violations = evaluator.violations()
        before_objective = evaluator.objective()
        evaluator.probe("T1", Point(mm(11), mm(11)))
        assert evaluator.violations() == before_violations
        assert evaluator.objective() == before_objective
        assert_matches_full(evaluator, board)

    def test_same_position_probe_is_zero_delta(self):
        board = dense_board()
        evaluator = Evaluator(board)
        placement = placement_of(board, "T1")
        probe = evaluator.probe("T1", placement.pos)
        assert probe.delta_nm == 0
        assert probe.violations == run_drc(board)

    def test_commit_is_the_writer(self):
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(15), mm(15))
        evaluator.commit("T1", pos)
        assert_matches_full(
            evaluator, board.with_placement("T1", pos.x, pos.y, 0, "F.Cu")
        )

    def test_unknown_ref_is_rejected(self):
        evaluator = Evaluator(dense_board())
        with pytest.raises(ValueError, match="unknown ref"):
            evaluator.probe("XX", Point(0, 0))
        with pytest.raises(ValueError, match="unknown ref"):
            evaluator.commit("XX", Point(0, 0))

    def test_net_span_recompute_never_reads_a_stale_center(self):
        # W1 is on BUS and GND; moving it must flip both spans exactly.
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(24), mm(10))
        evaluator.commit("W1", pos)
        equivalent = board.with_placement("W1", pos.x, pos.y, 0, "F.Cu")
        assert_matches_full(evaluator, equivalent)


class TestCountedVerdict:
    """The search-internal probe contract (#64, ADR-0016's amendment): a
    counted verdict — the error count and total the full probe reports,
    computed from the same merged findings and term deltas, but never
    materialized: no sorted findings tuple, no term-store copy, no
    Objective assembly. The same legality notion, unmaterialized — never a
    second, weaker check (ADR-0012); ``run_drc`` stays the authority and
    ``legalize`` stays full-cost."""

    def test_probe_key_matches_the_full_probe(self):
        board = dense_board()
        evaluator = Evaluator(board)
        for ref in ("T1", "W1", "T7", "C1"):
            for pos in (
                Point(mm(11), mm(11)),
                Point(mm(-3), mm(-3)),
                Point(mm(30), mm(25)),
            ):
                probe = evaluator.probe(ref, pos)
                assert evaluator.probe_key(ref, pos) == (
                    error_count(probe.violations),
                    probe.objective.total_nm,
                )

    def test_probe_key_does_not_mutate(self):
        evaluator = Evaluator(dense_board())
        before = evaluator.current_key()
        evaluator.probe_key("T1", Point(mm(11), mm(11)))
        assert evaluator.current_key() == before

    def test_current_key_matches_the_authorities_after_a_commit(self):
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(15), mm(15))
        evaluator.commit("T1", pos)
        equivalent = board.with_placement("T1", pos.x, pos.y, 0, "F.Cu")
        assert evaluator.current_key() == (
            error_count(run_drc(equivalent)),
            evaluate(equivalent).total_nm,
        )


class TestLocalityComplexity:
    @staticmethod
    def grid_board(count: int):
        """Non-overlapping grid world: count TINY parts in a chain of
        two-pin nets (the middle ref has degree 2 at any size), every
        courtyard clear of its neighbours."""
        spacing = mm(3)
        columns = math.isqrt(count - 1) + 1
        width = columns * spacing + spacing
        rows_count = -(-count // columns)
        height = rows_count * spacing + spacing
        comps = tuple(
            CompIR(ref=f"T{i}", entry_name="TINY") for i in range(1, count + 1)
        )
        nets = tuple(
            NetIR(name=f"N{i}", nodes=((f"T{i}", "1"), (f"T{i + 1}", "1")))
            for i in range(1, count)
        )
        spec = BoardSpec(
            netlist_ir=NetlistIR(comps=comps, nets=nets),
            footprint_irs={
                "TINY": FootprintIR(
                    entry_name="TINY", pads=UNIT_PAD, courtyard=TINY_YARD
                )
            },
            outline=Rectangle(Point(0, 0), Point(width, height)),
            stackup=("F.Cu", "B.Cu"),
        )
        board = build_board(spec)
        for i in range(1, count + 1):
            index = i - 1
            x = (index % columns) * spacing + spacing // 2
            y = (index // columns) * spacing + spacing // 2
            board = board.with_placement(f"T{i}", x, y, 0, "F.Cu")
        return board

    def test_probe_work_is_density_bounded(self):
        """The courtyard grid gathers the neighbourhood, not the field
        (#64): doubling the board does not double the probe's exact
        ``shapes_overlap`` tests, and the count sits strictly under the
        linear scan the pre-index O(n) loop cost (n-1 tests). The findings
        themselves are pinned bit-for-bit by the seeded walks — the grid
        must be observationally invisible."""
        small = self.grid_board(40)
        large = self.grid_board(80)
        probe_small = Evaluator(small).probe("T20", Point(mm(1), mm(1)))
        probe_large = Evaluator(large).probe("T40", Point(mm(1), mm(1)))
        assert probe_small.span_recomputes == probe_large.span_recomputes == 2
        assert probe_large.overlap_tests <= probe_small.overlap_tests
        assert probe_small.overlap_tests < 39

    def test_touching_yards_across_a_cell_boundary_stay_clean(self):
        """The strict-interior rule survives the grid's coarse pass: two
        1 mm yards whose edges meet exactly at a cell boundary line are
        gathered into the same cells, rejected by the strict AABB pass,
        and never overlap."""
        board = dense_board()
        evaluator = Evaluator(board)
        probe = evaluator.probe("T2", Point(mm(11), mm(10)))
        equivalent = board.with_placement("T2", mm(11), mm(10), 0, "F.Cu")
        assert probe.violations == run_drc(equivalent)
        assert not any(
            violation.type is ViolationType.COURTYARDS_OVERLAP
            and set(violation.offending_refs) == {"placement:T1", "placement:T2"}
            for violation in probe.violations
        )

    def test_commit_moves_a_slot_between_cells(self):
        """The invalidation story: commit is the only writer, and each hop
        re-buckets the moved slot — remove the old cells, insert the new —
        so the grid never drifts from the committed positions."""
        board = dense_board()
        evaluator = Evaluator(board)
        for pos in (
            Point(mm(12), mm(10)),
            Point(mm(24), mm(10)),
            Point(mm(-2), mm(-2)),
            Point(mm(10), mm(10)),
        ):
            evaluator.commit("T2", pos)
            equivalent = board.with_placement("T2", pos.x, pos.y, 0, "F.Cu")
            assert_matches_full(evaluator, equivalent)

    def test_unknown_term_kind_is_refused_loudly(self, monkeypatch):
        from enum import Enum

        import net2board.solver._evaluator as evaluator_module

        class Congestion(Enum):
            CONGESTION = "congestion"

        monkeypatch.setattr(
            evaluator_module,
            "TERMS",
            (TermKind.NET_SPAN, Congestion.CONGESTION),
        )
        with pytest.raises(NotImplementedError, match="TERMS"):
            Evaluator(dense_board())


class TestUnregisteredChecks:
    """A check the Evaluator does not implement natively must still hold
    the contract — via its COMPANIONS entry, or (absent one) a full
    recompute of itself: absence is slow, never wrong (ADR-0016)."""

    @staticmethod
    def _patched_walk(monkeypatch, companion: bool):
        import net2board.drc as drc_module
        import net2board.solver._evaluator as evaluator_module

        def extra_check(board):
            findings = []
            for placement in board.placements:
                if placement.pos.x < 0:
                    findings.append(
                        drc_module.Violation(
                            type=ViolationType.COURTYARD_OUTSIDE_OUTLINE,
                            severity=drc_module.Severity.WARNING,
                            description=f"west of the board: {placement.ref}",
                            offending_refs=(f"placement:{placement.ref}",),
                            locations=(placement.pos,),
                        )
                    )
            return tuple(findings)

        extras = (extra_check,)
        companions = dict(drc_module.COMPANIONS)
        if companion:

            def extra_companion(board, ref):
                return tuple(
                    finding
                    for finding in extra_check(board)
                    if f"placement:{ref}" in finding.offending_refs
                )

            companions[extra_check] = extra_companion
        patched = drc_module.CHECKS + extras
        monkeypatch.setattr(drc_module, "CHECKS", patched, raising=False)
        monkeypatch.setattr(evaluator_module, "CHECKS", patched, raising=False)
        monkeypatch.setattr(drc_module, "COMPANIONS", companions)
        monkeypatch.setattr(evaluator_module, "COMPANIONS", companions)
        seeded_walk(dense_board(), seed=11, moves=60)

    def test_extra_check_with_companion_stays_bit_for_bit(self, monkeypatch):
        self._patched_walk(monkeypatch, companion=True)

    def test_extra_check_without_companion_stays_bit_for_bit(self, monkeypatch):
        self._patched_walk(monkeypatch, companion=False)


class TestPrivacy:
    def test_solver_exports_ops_but_never_the_evaluator(self):
        from net2board import solver

        assert set(solver.__all__) == {
            "LegalizationError",
            "LegalizationResult",
            "SolveError",
            "SolveRefError",
            "SolveResult",
            "StopReason",
            "legalize",
            "solve",
        }
        assert "Evaluator" not in vars(solver)
        assert all("Evaluator" not in name for name in solver.__all__)
