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

import dataclasses
import math
import random
from collections.abc import Mapping

import pytest

from conftest import OUTLINE, fresh_board, load_ecc83_spec, make_spec
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import Severity, ViolationType, run_drc
from net2board.examples.ecc83_placements import EDGE_MOUNTS, PLACEMENTS
from net2board.geometry import Circle, Point, Rectangle
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import (
    ROTATIONS,
    Board,
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
from net2board.solver._evaluator import (
    Evaluator,
    Pose,
    _build_span_record,
    _MoveView,
)

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


def fmm(value: float) -> int:
    """Exact nanometres for a fractional-millimetre fixture constant."""
    return round(value * 1_000_000)


PAD_YARD = Rectangle(Point(-mm(2), -mm(2)), Point(mm(6), mm(2)))

# the left face sits 0.8 mm back of the pad centre: placed at x = 0.8 mm
# the yard face lands flush on the outline edge line — the EdgeMount
# band's zero point (a face short of the line is as unmet as one past
# the overhang)
EDGE_YARD = Rectangle(Point(-fmm(0.8), -mm(1)), Point(fmm(4.6), mm(1)))


def _pad(number, x, layers=("F.Cu",)):
    return PadIR(
        number=number,
        local_pos=Point(x, 0),
        size=(mm(1), mm(1)),
        drill=None,
        layers=layers,
        shape_enum="rect",
    )


PAD_FOOTPRINTS = {
    # a through-hole part: *.Cu pads span every stackup copper layer, so
    # the slot enters both grids and pairs with either side's copper
    "THT": FootprintIR(
        entry_name="THT",
        pads=(_pad("1", 0, ("*.Cu",)), _pad("2", mm(2), ("*.Cu",))),
        courtyard=PAD_YARD,
    ),
    # placed on B.Cu, its F.Cu token pads resolve to B.Cu — the cross-side
    # case the layer mask, never a side test, decides (ADR-0022)
    "BSMD": FootprintIR(
        entry_name="BSMD",
        pads=(_pad("1", 0), _pad("2", mm(2))),
        courtyard=PAD_YARD,
    ),
    "TIE": FootprintIR(
        entry_name="TIE",
        pads=(_pad("1", 0), _pad("2", 500_000)),
        courtyard=PAD_YARD,
        net_tie_groups=(frozenset({"1", "2"}),),
    ),
    # duplicate pad numbers (both "1" pads share a net) plus pad "3"
    # stacked on the first "1" — different nets, one location: the pair
    # finding's key needs the pad indices, locations alone collide (#92)
    "DUP": FootprintIR(
        entry_name="DUP",
        pads=(
            _pad("1", 0),
            _pad("1", mm(2)),
            _pad("3", 0),
            _pad("2", mm(4)),
        ),
        courtyard=PAD_YARD,
    ),
    # copper with no courtyard: the courtyard grid skips it, the copper
    # grid must not — covered by construction (ADR-0016, #82)
    "NOYARD": FootprintIR(entry_name="NOYARD", pads=(_pad("1", 0),), courtyard=None),
    "EDGEFP": FootprintIR(
        entry_name="EDGEFP", pads=(_pad("1", 0),), courtyard=EDGE_YARD
    ),
    "SAMEFP": FootprintIR(
        entry_name="SAMEFP",
        pads=(_pad("1", 0), _pad("2", 700_000)),
        courtyard=PAD_YARD,
    ),
    "INTRA": FootprintIR(
        entry_name="INTRA",
        pads=(_pad("1", 0), _pad("2", 300_000)),
        courtyard=PAD_YARD,
    ),
}

PAD_COMPS = tuple(
    CompIR(ref=ref, entry_name=entry)
    for ref, entry in (
        ("THT1", "THT"),
        ("BSMD1", "BSMD"),
        ("TIE1", "TIE"),
        ("DUP1", "DUP"),
        ("NOYARD1", "NOYARD"),
        ("EDGE1", "EDGEFP"),
        ("EDGEX1", "EDGEFP"),
        ("SAME1", "SAMEFP"),
        ("INTRA1", "INTRA"),
    )
)

PAD_NETS = tuple(
    NetIR(name=name, nodes=nodes)
    for name, nodes in (
        ("nTHTa", (("THT1", "1"),)),
        ("nTHTb", (("THT1", "2"),)),
        ("nBSa", (("BSMD1", "1"),)),
        ("nBSb", (("BSMD1", "2"),)),
        ("nTIEa", (("TIE1", "1"),)),
        ("nTIEb", (("TIE1", "2"),)),
        ("nDUPa", (("DUP1", "1"),)),
        ("nDUPb", (("DUP1", "3"),)),
        ("nDUPc", (("DUP1", "2"),)),
        ("nNYa", (("NOYARD1", "1"),)),
        ("nEDa", (("EDGE1", "1"),)),
        ("nEXa", (("EDGEX1", "1"),)),
        ("SAME", (("SAME1", "1"), ("SAME1", "2"))),
        ("nINa", (("INTRA1", "1"),)),
        ("nINb", (("INTRA1", "2"),)),
    )
)

PAD_ROWS = (
    ("THT1", 10, 10, 0, "F.Cu"),
    # 0.8 mm from THT1's pad 2, both resolving onto B.Cu: an entry short
    ("BSMD1", 11.2, 10, 0, "B.Cu"),
    ("TIE1", 20, 10, 0, "F.Cu"),
    # pad "2" sits centred on the right outline edge line: an entry edge
    # finding; the stacked "1"×"3" pair: an entry intra short
    ("DUP1", 36, 12, 0, "F.Cu"),
    # courtyard-less copper 0.2 mm from SAME1's pad 2: an entry short the
    # copper grid — only — can gather
    ("NOYARD1", 20.9, 20, 0, "F.Cu"),
    ("EDGE1", 0.8, 18, 0, "F.Cu"),
    ("EDGEX1", 0.8, 24, 0, "F.Cu"),
    ("SAME1", 20, 20, 0, "F.Cu"),
    ("INTRA1", 30, 20, 0, "F.Cu"),
)


def pad_board():
    """#92's dense synthetic pad world: a THT part across sides from a
    back-side SMD part, a net-tie group, duplicate pad numbers with one
    stacked pair, a courtyard-less part, same-net pairs, near-edge pads
    with one EdgeMount exemption, and intra-footprint shorts — the board
    is authored to violate from move one, so the pad checks have locality
    to be local about."""
    board = build_board(
        make_spec(
            netlist_ir=NetlistIR(comps=PAD_COMPS, nets=PAD_NETS),
            footprint_irs=PAD_FOOTPRINTS,
        )
    )
    board = board.with_constraint(
        Constraint(
            id="edge-exempt",
            kind=Requirement(),
            relation=EdgeMount(
                target="placement:EDGEX1", edge=Edge.LEFT, max_overhang_nm=500_000
            ),
        )
    )
    for ref, x, y, rotation, side in PAD_ROWS:
        board = board.with_placement(
            ref,
            fmm(x) if isinstance(x, float) else mm(x),
            fmm(y) if isinstance(y, float) else mm(y),
            rotation,
            side,
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


class TestProbeFaceReads:
    """The Evaluator owns the live state, so consumers read it there
    (#67): ``placement``/``position`` answer for the committed state —
    never a mirror a second writer can forget."""

    @staticmethod
    def _evaluator():
        return Evaluator(
            fresh_board()
            .with_placement("R1", 1_000_000, 2_000_000, 0, "F.Cu")
            .with_placement("R2", 5_000_000, 0, 90, "B.Cu")
        )

    def test_placement_reads_the_committed_state(self):
        evaluator = self._evaluator()
        assert evaluator.placement("R1") is not None
        assert evaluator.placement("R1").pos == Point(1_000_000, 2_000_000)
        assert evaluator.position("R1") == Point(1_000_000, 2_000_000)

    def test_an_unplaced_or_unknown_ref_reads_none(self):
        evaluator = self._evaluator()
        assert evaluator.placement("R3") is None
        assert evaluator.position("R3") is None
        assert evaluator.placement("ZZ") is None
        assert evaluator.position("ZZ") is None

    def test_the_read_is_live_after_commit(self):
        board = (
            fresh_board()
            .with_placement("R1", 1_000_000, 2_000_000, 0, "F.Cu")
            .with_placement("R2", 5_000_000, 0, 90, "B.Cu")
        )
        evaluator = Evaluator(board)
        evaluator.commit("R1", Pose(9_000_000, 9_000_000, 0))
        assert evaluator.position("R1") == Point(9_000_000, 9_000_000)
        assert evaluator.placement("R1").pos == Point(9_000_000, 9_000_000)
        # the read answers for the live state, not the input snapshot
        assert evaluator.placement("R1") != board.placement("R1")


class TestMoveViewIsAMapping:
    """The widened builder signatures promise ``Mapping`` (#66) — the
    Evaluator's own view must keep that promise, not merely duck it."""

    @staticmethod
    def _evaluator_placements():
        board = (
            fresh_board()
            .with_placement("R1", 0, 0, 0, "F.Cu")
            .with_placement("R2", 10_000_000, 0, 0, "F.Cu")
        )
        evaluator = Evaluator(board)
        return evaluator._placements

    @classmethod
    def _view(cls, moved="R1"):
        base = cls._evaluator_placements()
        candidate = dataclasses.replace(base[moved], pos=Point(5, 5))
        return _MoveView(base, moved, candidate)

    def test_the_view_satisfies_the_declared_protocol(self):
        assert isinstance(self._view(), Mapping)

    def test_reads_the_candidate_for_the_moved_ref(self):
        view = self._view()
        assert view.get("R1").pos == Point(5, 5)
        assert view["R1"].pos == Point(5, 5)

    def test_reads_the_base_for_every_other_ref(self):
        view = self._view()
        assert view.get("R2") is view._base["R2"]
        assert "R2" in view

    def test_a_missing_ref_defaults_and_counts(self):
        view = self._view()
        sentinel = object()
        assert view.get("ZZ", sentinel) is sentinel
        assert view.get("ZZ") is None
        with pytest.raises(KeyError):
            view["ZZ"]
        assert "ZZ" not in view
        # the moved ref replaces its base slot: the count never grows
        assert len(view) == len(view._base)
        assert set(view) == set(view._base)

    def test_a_candidate_for_an_unplotted_ref_extends_the_view(self):
        base = self._evaluator_placements()
        candidate = dataclasses.replace(base["R1"], ref="R9", pos=Point(7, 7))
        view = _MoveView(base, "R9", candidate)
        assert view.get("R9").pos == Point(7, 7)
        assert len(view) == len(base) + 1
        assert set(view) == set(base) | {"R9"}


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
        "pad_short": 0,
        "pad_clearance": 0,
        "pad_edge": 0,
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
        pose = Pose(pos.x, pos.y, rotation)
        probe = evaluator.probe(ref, pose)
        counted = evaluator.probe_key(ref, pose)
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
        seen["pad_short"] += any(
            v.type is ViolationType.SHORTING_ITEMS for v in probe.violations
        )
        seen["pad_clearance"] += any(
            v.type is ViolationType.CLEARANCE for v in probe.violations
        )
        seen["pad_edge"] += any(
            v.type is ViolationType.COPPER_EDGE_CLEARANCE for v in probe.violations
        )
        if placement is None:
            seen["placed"] += 1
        if rng.random() < commit_probability:
            evaluator.commit(ref, pose)
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
        probe = evaluator.probe("T4", Pose(mm(-1), mm(4), 0))  # 1.5 mm overhang: unmet
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
        probe = evaluator.probe("T3", Pose(mm(1), mm(1), 0))  # well outside the rect
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


SPAN_TRI_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(mm(1), mm(1)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(mm(2), 0),
        size=(mm(1), mm(1)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
    PadIR(
        number="3",
        local_pos=Point(0, mm(2)),
        size=(mm(1), mm(1)),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
SPAN_TRI_YARD = Rectangle(Point(-mm(1), -mm(1)), Point(mm(3), mm(3)))

SPAN_FOOTPRINTS = {
    "TRI": FootprintIR(entry_name="TRI", pads=SPAN_TRI_PADS, courtyard=SPAN_TRI_YARD),
    "TINY": DENSE_FOOTPRINTS["TINY"],
}

SPAN_COMPS = (
    CompIR(ref="U1", entry_name="TRI"),
    CompIR(ref="U2", entry_name="TRI"),  # stays unplaced until a walk places it
    CompIR(ref="R1", entry_name="TINY"),
    CompIR(ref="R2", entry_name="TINY"),
    CompIR(ref="R3", entry_name="TINY"),
)

SPAN_NETS = (
    # U1 holds two of BIG's five pins: one slot can own both top extremes
    NetIR(
        name="BIG",
        nodes=(("U1", "1"), ("U1", "2"), ("R1", "1"), ("R2", "1"), ("U2", "1")),
    ),
    NetIR(name="PAIR", nodes=(("U1", "3"), ("R3", "1"))),
    NetIR(name="SOLO", nodes=(("U2", "2"),)),  # single pin, on the unplaced part
)


def span_board():
    """The maintained-span world (#113): one part holding two pins of one
    net, a tie partner, an unplaced node keeping BIG PARTIAL, and a
    single-pin net on the unplaced part — every state a per-net span
    record can get wrong."""
    board = build_board(
        make_spec(
            netlist_ir=NetlistIR(comps=SPAN_COMPS, nets=SPAN_NETS),
            footprint_irs=SPAN_FOOTPRINTS,
        )
    )
    board = board.with_constraint(
        Constraint(
            id="crit-big", kind=Preference(), relation=Criticality(net="BIG"), weight=4
        )
    )
    rows = (
        ("U1", 0, 0, 0, "F.Cu"),
        ("R1", 20, 0, 0, "F.Cu"),
        ("R2", 30, 5, 0, "F.Cu"),
        ("R3", 40, 0, 0, "F.Cu"),
    )
    for ref, x, y, rotation, side in rows:
        board = board.with_placement(ref, mm(x), mm(y), rotation, side)
    return board


class TestMaintainedNetSpan:
    """#113: the net-span term reads a per-net record — per direction the
    best value, the slot holding it, and the best value from any other
    slot — so a probe costs O(1) per incident net, not O(net degree).
    The record must be observationally invisible (the probe contract):
    every test here is the bit-for-bit contract on exactly the states the
    record can get wrong — the moved slot owning both top values, a tie
    across slots, the extreme holder moving inward, the ≤1-point boundary,
    unplaced pins flipping PARTIAL — and ``commit`` stays the record's
    only writer (ADR-0016, amended)."""

    def test_the_record_is_maintained_by_commit_alone(self):
        board = span_board()
        evaluator = Evaluator(board)
        fresh = Evaluator(board)._span_records
        assert evaluator._span_records == fresh
        evaluator.probe("R1", Pose(mm(21), mm(1), 0))
        assert evaluator._span_records == fresh  # a probe writes nothing
        evaluator.commit("R1", Pose(mm(21), mm(1), 0))
        moved = board.with_placement("R1", mm(21), mm(1), 0, "F.Cu")
        assert evaluator._span_records == Evaluator(moved)._span_records

    def test_the_moved_slot_holding_both_top_extremes(self):
        board = span_board()
        # entry: U1 owns BIG's min-x twice over (0 and 2 mm) — the
        # excluded slot's runner-up must not survive the probe
        pose = Pose(mm(10), mm(10), 0)
        evaluator = Evaluator(board)
        candidate = board.with_placement("U1", mm(10), mm(10), 0, "F.Cu")
        probe = evaluator.probe("U1", pose)
        assert probe.violations == run_drc(candidate)
        assert probe.objective == evaluate(candidate)
        evaluator.commit("U1", pose)
        assert_matches_full(evaluator, candidate)

    def test_the_extreme_holder_moving_inward_shrinks_the_span(self):
        board = span_board()
        pose = Pose(mm(6), mm(0), 0)
        evaluator = Evaluator(board)
        candidate = board.with_placement("U1", mm(6), mm(0), 0, "F.Cu")
        probe = evaluator.probe("U1", pose)
        assert probe.objective == evaluate(candidate)
        evaluator.commit("U1", pose)
        assert_matches_full(evaluator, candidate)

    def test_a_tie_between_two_slots_hands_the_extreme_over(self):
        board = span_board()
        evaluator = Evaluator(board)
        # R1 lands exactly on U1.2's 2 mm min-x: two slots tie on a value
        evaluator.commit("R1", Pose(mm(2), 0, 0))
        tied = board.with_placement("R1", mm(2), 0, 0, "F.Cu")
        assert_matches_full(evaluator, tied)
        # the co-holder leaves: the surviving holder's value wins, not a
        # stale copy of the departed one
        evaluator.commit("U1", Pose(mm(8), mm(6), 0))
        assert_matches_full(
            evaluator, tied.with_placement("U1", mm(8), mm(6), 0, "F.Cu")
        )

    def test_the_second_placement_crosses_the_one_point_boundary(self):
        bare = build_board(
            make_spec(
                netlist_ir=NetlistIR(comps=SPAN_COMPS, nets=SPAN_NETS),
                footprint_irs=SPAN_FOOTPRINTS,
            )
        )
        bare = bare.with_constraint(
            Constraint(
                id="crit-big",
                kind=Preference(),
                relation=Criticality(net="BIG"),
                weight=4,
            )
        )
        bare = bare.with_placement("U1", 0, 0, 0, "F.Cu")
        evaluator = Evaluator(bare)
        assert_matches_full(evaluator, bare)  # BIG measures 0 over one pin
        # R1's placement is BIG's second slot: the span wakes up
        pose = Pose(mm(5), 0, 0)
        candidate = bare.with_placement("R1", mm(5), 0, 0, "F.Cu")
        probe = evaluator.probe("R1", pose)
        assert probe.objective == evaluate(candidate)
        evaluator.commit("R1", pose)
        assert_matches_full(evaluator, candidate)

    def test_placing_the_unplaced_part_flips_partial(self):
        board = span_board()
        evaluator = Evaluator(board)
        terms = {term.ref: term for term in evaluator.objective().terms}
        assert terms["net:BIG"].status.value == "partial"
        assert terms["net:SOLO"].status.value == "partial"
        evaluator.commit("U2", Pose(mm(25), mm(1), 0))
        equivalent = board.with_placement("U2", mm(25), mm(1), 0, "F.Cu")
        assert_matches_full(evaluator, equivalent)
        terms = {term.ref: term for term in evaluator.objective().terms}
        assert terms["net:BIG"].status.value == "active"
        assert terms["net:SOLO"].status.value == "active"

    def test_span_board_walk(self):
        """The seeded walk over the span world: moves, turns, and U2's
        placement — after every probe and commit, incremental == full."""
        rng = random.Random(113)
        board = span_board()
        evaluator = Evaluator(board)
        current = board
        for _ in range(150):
            ref = rng.choice(("U1", "U2", "R1", "R2", "R3"))
            placement = placement_of(current, ref)
            if placement is None:
                rotation, side = 0, "F.Cu"
            else:
                rotation, side = placement.rotation, placement.side
                if rng.random() < 0.3:
                    rotation = rng.choice(ROTATIONS)
            if placement is not None and rng.random() < 0.7:
                pos = Point(
                    placement.pos.x + rng.randint(-mm(3), mm(3)),
                    placement.pos.y + rng.randint(-mm(3), mm(3)),
                )
            else:
                pos = Point(rng.randint(-mm(2), mm(45)), rng.randint(-mm(2), mm(10)))
            pose = Pose(pos.x, pos.y, rotation)
            probe = evaluator.probe(ref, pose)
            candidate = current.with_placement(ref, pos.x, pos.y, rotation, side)
            assert probe.violations == run_drc(candidate)
            assert probe.objective == evaluate(candidate)
            assert evaluator.probe_key(ref, pose) == (
                error_count(probe.violations),
                probe.objective.total_nm,
            )
            if rng.random() < 0.75:
                evaluator.commit(ref, pose)
                current = candidate
                assert_matches_full(evaluator, current)
        assert_matches_full(evaluator, current)
        assert placement_of(current, "U2") is not None  # the walk placed it


class TestSpanRecordBuilder:
    """The record builder against an independent oracle (the #113 retro):
    hand-built pins and centers, every field checked against plain
    min/max over the same points and a counted pad list. The seeded walks
    cannot see a corrupted record whose duplicates leave integer min/max
    untouched — the builder's own contract is the only place the counts
    and pad lists are visible."""

    def test_the_record_matches_a_plain_min_max_walk(self):
        centers = [
            (Point(0, 0), Point(mm(2), 0), Point(0, mm(2))),
            (Point(mm(20), 0),),
            None,  # an unplaced slot: its pads stay on the record
            (Point(mm(30), mm(5)),),
        ]
        pins = (
            (0, 0),
            (0, 1),  # slot 0 holds two pins: both top min-x values
            (1, 0),
            (3, 0),
            (2, 0),  # the unplaced slot's pin
            (None, -1),  # a ref outside the netlist: permanently unplaced
        )
        record = _build_span_record(pins, centers)
        assert record.placed == 4
        assert record.unplaced == 2
        # min-x: slot 0 holds both top values; the runner-up is the best
        # value from any OTHER slot
        assert record.min_x.value == 0
        assert record.min_x.slot == 0
        assert record.min_x.other == mm(20)
        assert record.min_x.other_slot == 1
        assert record.max_x.value == mm(30)
        assert record.max_x.slot == 3
        assert record.max_x.other == mm(20)
        assert record.min_y.value == 0
        assert record.min_y.slot == 0
        assert record.max_y.value == mm(5)
        assert record.max_y.slot == 3
        assert record.max_y.other == 0
        # every resolved slot's pads, exactly once each, placed or not
        assert record.slot_pads == {0: (0, 1), 1: (0,), 2: (0,), 3: (0,)}

    def test_a_cross_slot_tie_keeps_both_values(self):
        centers = [(Point(mm(4), 0),), (Point(mm(4), mm(2)),)]
        record = _build_span_record(((0, 0), (1, 0)), centers)
        assert record.min_x.value == mm(4)
        assert record.min_x.slot == 0
        assert record.min_x.other == mm(4)
        assert record.min_x.other_slot == 1
        assert record.max_x.value == mm(4)
        assert record.max_x.slot == 0
        assert record.max_y.value == mm(2)
        assert record.max_y.slot == 1
        assert record.max_y.other == 0


def rotation_seeded_walk(
    board, seed, moves=200, commit_probability=0.75, require_placed=True
):
    """The rotation walk (ADR-0021/0025's falsification): the seeded
    walk's bit-for-bit contract — after every probe and commit,
    incremental == full on the equivalent Board — exercised across
    rotation commits. Each move draws one of the three classes:
    position-only (the standing rotation), rotation-only (the standing
    position), or both at once. Own seed, own draws — the existing
    walks' random sequences are load-bearing and stay untouched."""
    rng = random.Random(seed)
    evaluator = Evaluator(board)
    current = board
    refs = tuple(comp.ref for comp in board.components)
    seen = {
        "violation": 0,
        "overlap": 0,
        "constraint": 0,
        "containment": 0,
        "pad_short": 0,
        "pad_clearance": 0,
        "pad_edge": 0,
        "placed": 0,
        "rejected": 0,
        "rotation_commits": 0,
    }
    for _ in range(moves):
        ref = rng.choice(refs)
        placement = placement_of(current, ref)
        if placement is not None:
            entry_rotation, side = placement.rotation, placement.side
        else:
            entry_rotation, side = 0, "F.Cu"
        move_class = rng.choice(("position", "rotation", "both"))
        if move_class != "rotation":
            # a fresh position: near the current one, or anywhere
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
        else:
            pos = placement.pos if placement is not None else Point(0, 0)
        if move_class != "position":
            rotation = rng.choice(ROTATIONS)
        else:
            rotation = entry_rotation
        pose = Pose(pos.x, pos.y, rotation)
        probe = evaluator.probe(ref, pose)
        counted = evaluator.probe_key(ref, pose)
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
        seen["pad_short"] += any(
            v.type is ViolationType.SHORTING_ITEMS for v in probe.violations
        )
        seen["pad_clearance"] += any(
            v.type is ViolationType.CLEARANCE for v in probe.violations
        )
        seen["pad_edge"] += any(
            v.type is ViolationType.COPPER_EDGE_CLEARANCE for v in probe.violations
        )
        if placement is None:
            seen["placed"] += 1
        if rng.random() < commit_probability:
            evaluator.commit(ref, pose)
            current = candidate
            # the criterion's letter: after every commit, too
            assert_matches_full(evaluator, current)
            if rotation != entry_rotation:
                seen["rotation_commits"] += 1
        else:
            seen["rejected"] += 1
    assert seen["rotation_commits"] > 0, "walk never committed a rotation change"
    assert seen["overlap"] > 0, "walk never produced an overlap finding"
    assert seen["constraint"] > 0, "walk never produced a constraint finding"
    assert seen["containment"] > 0, "walk never produced a containment finding"
    if require_placed:
        assert seen["placed"] > 0, "walk never placed an unplaced ref"
    assert_matches_full(evaluator, current)
    return seen


class TestRotationSeededWalk:
    """The rotation walks (ADR-0025's falsification): new seeds on the
    walk boards, mixing position-only, rotation-only, and both-at-once
    moves, asserting incremental == full bit-for-bit after every probe
    and commit across rotation commits. The existing walks above keep
    their exact draws — these are additions, never edits."""

    def test_dense_board_rotation_walk(self):
        seen = rotation_seeded_walk(dense_board(), seed=4242)
        assert seen["rejected"] > 0
        assert seen["rotation_commits"] >= 10

    def test_ecc83_board_rotation_walk(self):
        seen = rotation_seeded_walk(ecc83_walk_board(), seed=77, require_placed=False)
        assert seen["rotation_commits"] >= 10


class TestPadBoardWalks:
    """#92's walks: the seeded-walk contract extended onto the dense
    synthetic pad board — the pad checks' verdicts join the
    incremental == full assertion after every probe and commit, and the
    walk must actually bite: shorts, clearances, and edge findings all
    seen, across turns (#89's rotation walk)."""

    def test_pad_board_walk(self):
        seen = seeded_walk(pad_board(), seed=5150, require_placed=False)
        assert seen["rejected"] > 0
        assert seen["pad_short"] > 0, "walk never produced a pad short"
        assert seen["pad_clearance"] > 0, "walk never produced a pad clearance"
        assert seen["pad_edge"] > 0, "walk never produced a pad edge finding"

    def test_pad_board_rotation_walk(self):
        seen = rotation_seeded_walk(
            pad_board(), seed=6251, moves=240, require_placed=False
        )
        assert seen["rotation_commits"] > 0
        assert seen["pad_short"] > 0, "turns never produced a pad short"
        assert seen["pad_clearance"] > 0, "turns never produced a pad clearance"
        assert seen["pad_edge"] > 0, "turns never produced a pad edge finding"


class TestRotationProbeCost:
    def test_a_rotation_probe_costs_a_position_probe(self):
        """ADR-0025: a rotation probe costs a position probe — both run
        the same ``_plan`` (courtyard recompute, pad-centre transforms,
        the same merged deltas), so the knob's cost is probe *count*,
        not per-probe time. The counted operations agree, and the probe
        writes nothing (the fast tier counts its work, never wall
        clocks it)."""
        board = dense_board()
        evaluator = Evaluator(board)
        placement = placement_of(board, "W1")
        rotation_probe = evaluator.probe(
            "W1", Pose(placement.pos.x, placement.pos.y, 90)
        )
        position_probe = evaluator.probe(
            "W1",
            Pose(placement.pos.x + mm(1), placement.pos.y, placement.rotation),
        )
        assert rotation_probe.span_recomputes == position_probe.span_recomputes
        assert rotation_probe.overlap_tests > 0
        assert position_probe.overlap_tests > 0
        assert_matches_full(evaluator, board)


class TestProbeContract:
    def test_probe_does_not_mutate(self):
        board = dense_board()
        evaluator = Evaluator(board)
        before_violations = evaluator.violations()
        before_objective = evaluator.objective()
        evaluator.probe("T1", Pose(mm(11), mm(11), 0))
        assert evaluator.violations() == before_violations
        assert evaluator.objective() == before_objective
        assert_matches_full(evaluator, board)

    def test_same_position_probe_is_zero_delta(self):
        board = dense_board()
        evaluator = Evaluator(board)
        placement = placement_of(board, "T1")
        probe = evaluator.probe("T1", Pose.of(placement))
        assert probe.delta_nm == 0
        assert probe.violations == run_drc(board)

    def test_commit_is_the_writer(self):
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(15), mm(15))
        evaluator.commit("T1", Pose(pos.x, pos.y, 0))
        assert_matches_full(
            evaluator, board.with_placement("T1", pos.x, pos.y, 0, "F.Cu")
        )

    def test_unknown_ref_is_rejected(self):
        evaluator = Evaluator(dense_board())
        with pytest.raises(ValueError, match="unknown ref"):
            evaluator.probe("XX", Pose(0, 0, 0))
        with pytest.raises(ValueError, match="unknown ref"):
            evaluator.commit("XX", Pose(0, 0, 0))

    def test_net_span_recompute_never_reads_a_stale_center(self):
        # W1 is on BUS and GND; moving it must flip both spans exactly.
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(24), mm(10))
        evaluator.commit("W1", Pose(pos.x, pos.y, 0))
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
                probe = evaluator.probe(ref, Pose(pos.x, pos.y, 0))
                assert evaluator.probe_key(ref, Pose(pos.x, pos.y, 0)) == (
                    error_count(probe.violations),
                    probe.objective.total_nm,
                )

    def test_probe_key_does_not_mutate(self):
        evaluator = Evaluator(dense_board())
        before = evaluator.current_key()
        evaluator.probe_key("T1", Pose(mm(11), mm(11), 0))
        assert evaluator.current_key() == before

    def test_current_key_matches_the_authorities_after_a_commit(self):
        board = dense_board()
        evaluator = Evaluator(board)
        pos = Point(mm(15), mm(15))
        evaluator.commit("T1", Pose(pos.x, pos.y, 0))
        equivalent = board.with_placement("T1", pos.x, pos.y, 0, "F.Cu")
        assert evaluator.current_key() == (
            error_count(run_drc(equivalent)),
            evaluate(equivalent).total_nm,
        )


class TestNativePadChecks:
    """#92's fixture list as exact-parity scenarios on the dense pad
    board: the THT-across-sides short, the intra-footprint rewrite under
    turns, duplicate pad numbers with a stacked pair, the courtyard-less
    neighbour, the EdgeMount exemption, and same-net pairs — every probe
    bit-for-bit with the full recompute on the equivalent Board."""

    def test_cross_side_short_flips_to_clearance(self):
        board = pad_board()
        evaluator = Evaluator(board)
        # entry: the B-side mirror trails BSMD1's pads to 9.2 and 11.2 mm,
        # 0.8 mm from THT1's at 10 and 12 — all four pairs resolve onto
        # B.Cu and two overlap. The side never enters it, the layer mask
        # decides
        pair = {"placement:THT1", "placement:BSMD1"}

        def kind_count(violations, kind):
            return sum(
                1
                for v in violations
                if v.type is kind and pair <= set(v.offending_refs)
            )

        assert kind_count(evaluator.violations(), ViolationType.SHORTING_ITEMS) == 2
        # one move right: every shared-layer pair touches at a zero gap
        pose = Pose(mm(13), mm(10), 0)
        probe = evaluator.probe("BSMD1", pose)
        candidate = board.with_placement("BSMD1", pose.x, pose.y, 0, "B.Cu")
        assert probe.violations == run_drc(candidate)
        assert kind_count(probe.violations, ViolationType.SHORTING_ITEMS) == 0
        assert kind_count(probe.violations, ViolationType.CLEARANCE) == 3

    def test_intra_short_locations_rewrite_on_a_turn(self):
        board = pad_board()
        evaluator = Evaluator(board)
        pose = Pose(mm(30), mm(20), 90)  # INTRA1 turns in place
        probe = evaluator.probe("INTRA1", pose)
        candidate = board.with_placement("INTRA1", pose.x, pose.y, 90, "F.Cu")
        assert probe.violations == run_drc(candidate)
        intra = [
            v
            for v in probe.violations
            if v.type is ViolationType.SHORTING_ITEMS
            and v.offending_refs == ("placement:INTRA1", "placement:INTRA1")
        ]
        assert len(intra) == 1
        # the verdict is placement-invariant; the locations turned with it
        assert intra[0].locations[1].y == intra[0].locations[0].y - 300_000
        evaluator.commit("INTRA1", pose)
        assert_matches_full(evaluator, candidate)

    def test_stacked_duplicate_numbers_stay_one_finding(self):
        board = pad_board()
        evaluator = Evaluator(board)
        pose = Pose(mm(34), mm(16), 180)  # DUP1 turns, pads still stacked
        probe = evaluator.probe("DUP1", pose)
        candidate = board.with_placement("DUP1", pose.x, pose.y, 180, "F.Cu")
        assert probe.violations == run_drc(candidate)
        stacked = [
            v
            for v in probe.violations
            if v.type is ViolationType.SHORTING_ITEMS
            and v.offending_refs == ("placement:DUP1", "placement:DUP1")
            and v.locations[0] == v.locations[1]
        ]
        # one stacked pair, one finding — two pads, one location, the
        # pad indices in the key keeping it from colliding (#92)
        assert len(stacked) == 1
        evaluator.commit("DUP1", pose)
        assert_matches_full(evaluator, candidate)

    def test_courtyardless_part_is_a_grid_neighbour(self):
        board = pad_board()
        evaluator = Evaluator(board)
        pose = Pose(fmm(21.6), mm(10), 0)  # NOYARD1 moves beside TIE1
        probe = evaluator.probe("NOYARD1", pose)
        candidate = board.with_placement("NOYARD1", pose.x, pose.y, 0, "F.Cu")
        assert probe.violations == run_drc(candidate)
        # TIE1's second pad sits 0.1 mm away: one clearance finding,
        # gathered with no courtyard anywhere in the pair
        clearances = [
            v
            for v in probe.violations
            if v.type is ViolationType.CLEARANCE
            and "placement:TIE1" in v.offending_refs
        ]
        assert len(clearances) == 1

    def test_edge_mount_exemption_holds_under_probes(self):
        board = pad_board()
        evaluator = Evaluator(board)
        # EDGEX1's stated LEFT edge exempts its pads wherever they go;
        # EDGE1 — the same footprint, no statement — finds at entry
        assert any(
            v.type is ViolationType.COPPER_EDGE_CLEARANCE
            and "placement:EDGE1" in v.offending_refs
            for v in evaluator.violations()
        )
        for pose in (Pose(fmm(0.8), mm(24), 0), Pose(fmm(0.3), mm(24), 90)):
            probe = evaluator.probe("EDGEX1", pose)
            candidate = board.with_placement(
                "EDGEX1", pose.x, pose.y, pose.rotation, "F.Cu"
            )
            assert probe.violations == run_drc(candidate)
            assert not any(
                v.type is ViolationType.COPPER_EDGE_CLEARANCE
                and "placement:EDGEX1" in v.offending_refs
                for v in probe.violations
            )
        evaluator.commit("EDGEX1", Pose(fmm(0.3), mm(24), 90))
        assert_matches_full(
            evaluator, board.with_placement("EDGEX1", fmm(0.3), mm(24), 90, "F.Cu")
        )

    def test_same_net_pairs_stay_exempt_under_moves(self):
        board = pad_board()
        evaluator = Evaluator(board)
        pose = Pose(fmm(20.9), mm(20), 0)  # SAME1 moves back onto NOYARD1's spot
        probe = evaluator.probe("SAME1", pose)
        candidate = board.with_placement("SAME1", pose.x, pose.y, 0, "F.Cu")
        assert probe.violations == run_drc(candidate)
        # its own two pads overlap: same net, exempt — one net-tie-free
        # exemption the narrow phase must honour
        assert not any(
            v.type is ViolationType.SHORTING_ITEMS
            and v.offending_refs == ("placement:SAME1", "placement:SAME1")
            for v in probe.violations
        )
        evaluator.commit("SAME1", pose)
        assert_matches_full(evaluator, candidate)


class TestPadPairsRead:
    """#95's repair seam: ``Evaluator.pad_pairs`` reads the violating pad
    pairs back off the stored findings — the shared builder's own
    records, pad indices aligned to the refs as passed, never a
    re-price."""

    def test_pairs_read_off_the_stored_findings(self):
        board = pad_board()
        evaluator = Evaluator(board)
        findings = [
            v
            for v in evaluator.violations()
            if v.type in (ViolationType.SHORTING_ITEMS, ViolationType.CLEARANCE)
            and set(v.offending_refs) == {"placement:THT1", "placement:BSMD1"}
        ]
        assert findings  # the cross-side world violates from move one
        pairs = evaluator.pad_pairs("THT1", "BSMD1")
        assert len(pairs) == len(findings)
        # the argument order decides the alignment — mirrored, swapped
        mirrored = evaluator.pad_pairs("BSMD1", "THT1")
        assert all(
            (index_b, index_a) == (mirrored_b, mirrored_a)
            for (index_a, index_b), (mirrored_b, mirrored_a) in zip(pairs, mirrored)
        )
        # the indices answer the keys the findings are stored under
        from net2board.solver._evaluator import _pad_key

        canonical = tuple(sorted(("placement:THT1", "placement:BSMD1")))
        keys = {_pad_key(canonical, (index_a, index_b)) for index_a, index_b in pairs}
        stored = {
            key
            for index in (evaluator._shorting_index, evaluator._clearance_index)
            for key, violation in evaluator._findings[index].items()
            if violation is not None and set(key[1]) == {"THT1", "BSMD1"}
        }
        assert keys == stored

    def test_a_committed_move_empties_the_read(self):
        board = pad_board()
        evaluator = Evaluator(board)
        assert evaluator.pad_pairs("THT1", "BSMD1")
        far = Pose(mm(30), mm(10), 0)
        evaluator.commit("BSMD1", far)
        assert evaluator.pad_pairs("THT1", "BSMD1") == ()
        assert evaluator.pad_pairs("BSMD1", "THT1") == ()


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
        pre-index O(n) loop's linear cost (n-1 tests). The findings
        themselves are pinned bit-for-bit by the seeded walks — the grid
        must be observationally invisible."""
        small = self.grid_board(40)
        large = self.grid_board(80)
        probe_small = Evaluator(small).probe("T20", Pose(mm(1), mm(1), 0))
        probe_large = Evaluator(large).probe("T40", Pose(mm(1), mm(1), 0))
        assert probe_small.span_recomputes == probe_large.span_recomputes == 2
        assert probe_large.overlap_tests <= probe_small.overlap_tests
        assert probe_small.overlap_tests < 39

    def test_pad_pair_work_is_density_bounded(self):
        """#92: the pad checks' exact narrow-phase tests join the counted
        work — bounded by the copper grid's density, not by n: doubling
        the board does not double the count, and a probe onto another
        part's copper pays at least one exact test."""
        small = self.grid_board(40)
        large = self.grid_board(80)
        probe_small = Evaluator(small).probe("T20", Pose(mm(1), mm(1), 0))
        probe_large = Evaluator(large).probe("T40", Pose(mm(1), mm(1), 0))
        assert probe_small.pad_pair_tests > 0
        assert probe_large.pad_pair_tests <= probe_small.pad_pair_tests

    @staticmethod
    def connector_board(field_parts: int):
        """A J4-shaped world for the #92 cull pins: an 80-pad two-row
        connector parked at a fixed spot, one long two-pad neighbour
        (W0) whose grown box spans the connector's row — without the
        per-capsule cull every pad in that span exact-prices both of
        W0's pads — and a chained TINY field whose rows sit at fixed
        absolute positions, so extra parts only ever append beyond the
        connector's grown box (the 5 mm-gap rows keep the second row
        provably clean of every edge line in the edge-count pin)."""
        long_pads = (
            PadIR(
                number="1",
                local_pos=Point(0, 0),
                size=(mm(1), mm(1)),
                drill=None,
                layers=("F.Cu",),
                shape_enum="circle",
            ),
            PadIR(
                number="2",
                local_pos=Point(mm(30), 0),
                size=(mm(1), mm(1)),
                drill=None,
                layers=("F.Cu",),
                shape_enum="circle",
            ),
        )
        comps = (
            CompIR(ref="B1", entry_name="CONN"),
            CompIR(ref="W0", entry_name="LONG"),
        ) + tuple(
            CompIR(ref=f"T{i}", entry_name="TINY") for i in range(1, field_parts + 1)
        )
        nets = tuple(
            NetIR(name=f"N{i}", nodes=((f"T{i}", "1"), (f"T{i + 1}", "1")))
            for i in range(1, field_parts)
        )
        pads = tuple(
            PadIR(
                number=str(row * 40 + col + 1),
                local_pos=Point(mm(col), mm(row * 5)),
                size=(500_000, 500_000),  # 0.5 mm rects — radius-0 capsules
                drill=None,
                layers=("F.Cu",),
                shape_enum="rect",
            )
            for row in (0, 1)
            for col in range(40)
        )
        spec = BoardSpec(
            netlist_ir=NetlistIR(comps=comps, nets=nets),
            footprint_irs={
                "CONN": FootprintIR(entry_name="CONN", pads=pads, courtyard=None),
                "LONG": FootprintIR(entry_name="LONG", pads=long_pads, courtyard=None),
                "TINY": FootprintIR(
                    entry_name="TINY", pads=UNIT_PAD, courtyard=TINY_YARD
                ),
            },
            outline=Rectangle(Point(0, 0), Point(mm(90), mm(30))),
            stackup=("F.Cu", "B.Cu"),
        )
        board = build_board(spec)
        board = board.with_placement("B1", mm(3), mm(5), 0, "F.Cu")
        board = board.with_placement("W0", mm(12), mm(5), 0, "F.Cu")
        for i in range(1, field_parts + 1):
            index = i - 1
            board = board.with_placement(
                f"T{i}", mm(47 + 3 * (index % 10)), mm(3 + 3 * (index // 10)), 0, "F.Cu"
            )
        return board

    def test_big_part_probe_work_is_density_bounded(self):
        """#92's J4 row: the culls keep a big part's exact pad-pair tests
        bounded by the neighbourhood, not by n — doubling the distant
        field leaves the count unchanged (the gathered set is cells'
        work, the culls price grown-box overlap alone), and the long
        neighbour's grown-box span covers 32 of the mover's row pads
        (64 pairs without the per-capsule cull) yet exact-prices only
        the pairs near its two pads."""
        probe_small = Evaluator(self.connector_board(40)).probe(
            "B1", Pose(mm(3) + 200_000, mm(5), 0)
        )
        probe_large = Evaluator(self.connector_board(80)).probe(
            "B1", Pose(mm(3) + 200_000, mm(5), 0)
        )
        assert probe_small.pad_pair_tests > 0
        assert probe_large.pad_pair_tests == probe_small.pad_pair_tests
        assert probe_small.pad_pair_tests < 64

    def test_edge_records_price_only_the_rule_band(self, monkeypatch):
        """#92's edge cull: the box skip passes a straddling mover to the
        per-capsule cull, which prices only the pads not provably clean
        of every edge line — the row inside the rule band (40 of the 80
        pads), one finding each — and a mover whole inside the shrunk
        outline prices nothing. Correctness against exhaustive pricing is
        the seeded differential's (#98); this pins the work."""
        import net2board.solver._evaluator as evaluator_module

        evaluator = Evaluator(self.connector_board(40))
        calls = []
        original = evaluator_module._pad_edge_record

        def counting(*args):
            calls.append(args)
            return original(*args)

        monkeypatch.setattr(evaluator_module, "_pad_edge_record", counting)
        edge = ViolationType.COPPER_EDGE_CLEARANCE
        near = evaluator.probe("B1", Pose(mm(5), 300_000, 0))
        assert len(calls) == 40
        findings = [
            violation for violation in near.violations if violation.type is edge
        ]
        assert len(findings) == 40
        assert len({violation.locations for violation in findings}) == 40
        calls.clear()
        far = evaluator.probe("B1", Pose(mm(5), mm(10), 0))
        assert calls == []
        assert not any(violation.type is edge for violation in far.violations)

    def test_pad_wording_does_not_walk_the_placement_view(self, monkeypatch):
        """#111: wording a pad finding resolves its ``placement:`` refs in
        O(1) — ``Board.placement``'s index on a real Board, the same call
        answered from the ``_MoveView`` on the Evaluator's ``_ProbeBoard``
        — never by rebuilding the ref → placement map over all n
        placements. The spy counts every item iterated out of a
        ``_MoveView`` while the probe runs: the pre-#111 wording walked
        all of them per finding (2n for one SHORTING pair), so a single
        item here is the O(n) term back. Zero is the pinned bound — a
        future bounded walk relaxes it deliberately, never silently."""
        walked = 0
        real_iter = _MoveView.__iter__

        def counting_iter(view):
            nonlocal walked
            for item in real_iter(view):
                walked += 1
                yield item

        monkeypatch.setattr(_MoveView, "__iter__", counting_iter)
        probe_small = Evaluator(self.grid_board(40)).probe("T20", Pose(mm(1), mm(1), 0))
        probe_large = Evaluator(self.grid_board(80)).probe("T40", Pose(mm(1), mm(1), 0))
        assert any(
            violation.type is ViolationType.SHORTING_ITEMS
            for violation in probe_small.violations
        )
        assert any(
            violation.type is ViolationType.SHORTING_ITEMS
            for violation in probe_large.violations
        )
        assert walked == 0

    def test_touching_yards_across_a_cell_boundary_stay_clean(self):
        """The strict-interior rule survives the grid's coarse pass: two
        1 mm yards whose edges meet exactly at a cell boundary line are
        gathered into the same cells, rejected by the strict AABB pass,
        and never overlap."""
        board = dense_board()
        evaluator = Evaluator(board)
        probe = evaluator.probe("T2", Pose(mm(11), mm(10), 0))
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
            evaluator.commit("T2", Pose(pos.x, pos.y, 0))
            equivalent = board.with_placement("T2", pos.x, pos.y, 0, "F.Cu")
            assert_matches_full(evaluator, equivalent)

    def test_commit_rebuckets_a_rotation_across_cells(self):
        """The invalidation invariant's rotation clause (ADR-0025): slots
        never unplace and never change side, and a rotation change
        re-buckets like a move. The WIDE yard is 10×4 mm — 1 mm cells, so
        the 0° slot spans a row of cells and the 90° slot a column: after
        the turn a probe over the new column must find the overlap the
        stale row never covered, and a probe over the abandoned row must
        find none."""
        board = dense_board()
        evaluator = Evaluator(board)
        evaluator.commit("W1", Pose(mm(18), mm(10), 90))
        # the Pose is the operation: the committed rotation took
        assert evaluator.placement("W1").rotation == 90
        # (18, 5) is inside the 90° column (16..20, 1..11), outside the
        # 0° row (17..27, 8..12) — only fresh buckets gather it
        probe = evaluator.probe("T5", Pose(mm(18), mm(5), 0))
        assert any(
            violation.type is ViolationType.COURTYARDS_OVERLAP
            and set(violation.offending_refs) == {"placement:T5", "placement:W1"}
            for violation in probe.violations
        )
        # (25, 10) is inside the 0° row's old reach, outside the 90°
        # column — only stale buckets would gather it
        probe = evaluator.probe("T5", Pose(mm(25), mm(10), 0))
        assert not any(
            violation.type is ViolationType.COURTYARDS_OVERLAP
            and set(violation.offending_refs) == {"placement:T5", "placement:W1"}
            for violation in probe.violations
        )
        assert_matches_full(
            evaluator, board.with_placement("W1", mm(18), mm(10), 90, "F.Cu")
        )

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


class TestRegisteredChecksAreNative:
    """Every check registered in ``CHECKS`` stays off the Evaluator's
    slow fallback: one probe must never materialise a candidate Board
    through ``with_placement`` (#92). The fallback itself is contract,
    not bug (``TestUnregisteredChecks`` — absence is slow, never wrong),
    but a *registered* check riding it makes every search probe pay the
    full-board cost: how #91's pad checks grew the fast tier from 14 s
    to 179 s (90% of ``run_session()`` inside ``_foreign_changed``).
    Counted, never timed (ADR-0016: no clock in the fast tier). The
    board is built before the patch — construction prices the input
    Board through the public path by contract; it is the probe that must
    never materialise."""

    def test_a_probe_materialises_no_board(self, monkeypatch):
        board = dense_board()
        calls = 0
        original = Board.with_placement

        def counting(board, *args, **kwargs):
            nonlocal calls
            calls += 1
            return original(board, *args, **kwargs)

        monkeypatch.setattr(Board, "with_placement", counting)
        evaluator = Evaluator(board)
        assert calls == 0
        evaluator.probe("T2", Pose(mm(13), mm(11), 90))
        evaluator.probe("W1", Pose(mm(5), mm(9), 180))
        evaluator.probe("C1", Pose(mm(20), mm(20), 0))
        evaluator.probe("T7", Pose(mm(2), mm(2), 0))
        assert calls == 0


class TestUnregisteredChecks:
    """A check the Evaluator does not implement natively must still hold
    the contract — via its COMPANIONS entry, or (absent one) a full
    recompute of itself: absence is slow, never wrong (ADR-0016)."""

    @staticmethod
    def _patched_walk(monkeypatch, companion: bool):
        import net2board.drc as drc_module

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
        monkeypatch.setattr(drc_module, "COMPANIONS", companions)
        # patched through the drc module alone: the Evaluator reads the
        # registry at call time, so no second patch site exists (#66)
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
