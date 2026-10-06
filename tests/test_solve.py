"""The solve seam — budgeted, deterministic, Board-in/Board-out (#46, ADR-0013).

The contract under test:

- ``solve(board, refs=None, *, budget)`` returns a frozen ``SolveResult(board,
  unmet_constraint_ids, stop_reason)`` — no success boolean, no carried
  Violations, no cost field; failure is a value the orchestrator branches on;
- the budget is counted in candidates evaluated (one Evaluator probe each),
  never wall clock, and is never exceeded; ``budget=0`` is a valid no-op;
- input errors raise: a malformed budget, or a ``refs`` entry naming no
  component or a locked placement, raise under ``SolveError``
  (``Net2BoardError``), the ref cases through the ref-carrying
  ``SolveRefError``;
- ``refs`` scopes the call — bare placement refs only, matching
  ``with_placement``'s surface; locked placements never move and out-of-scope
  placements are never touched;
- an unsatisfiable Requirement is reported in ``unmet_constraint_ids``, never
  raised; the final phase is ``legalize``, so the result board passes legalize
  identity by construction;
- determinism is unconditional: same ``(board, refs, budget)``, byte-identical
  result, and identity on no-op — a solved board re-solved returns itself.

The local world: one-pad TINY parts (±0.5 mm rectangle courtyard) plus a
two-pad PAIRED part, on the shared 40 × 30 mm outline, with small nets so the
NetSpan numeraire has something to rank — every move in these tests is a few
millimetres and every improvement unambiguous.
"""

from math import isqrt

import pytest

from conftest import load_ecc83_spec, make_spec
from net2board.build import build_board
from net2board.drc import Severity, run_drc
from net2board.errors import Net2BoardError
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_pcb
from net2board.geometry import Point, Rectangle, pad_world_center
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import (
    Board,
    Constraint,
    Proximity,
    Requirement,
)
from net2board.objective import evaluate
from net2board.solver import (
    SolveError,
    SolveRefError,
    SolveResult,
    StopReason,
    legalize,
    solve,
)
from net2board.solver._solve import (
    _GRID_INSET_NM,
    _GRID_PITCH_NM,
    _PLACEMENT_MARGIN_NM,
    _half_diagonal,
    _visit_order,
)


def mm(value: int) -> int:
    return value * 1_000_000


def pad_gap_nm(a, b, pad: str = "1") -> int:
    """The Euclidean pad-to-pad distance between two placements, in nm."""
    pa = pad_world_center(a, pad)
    pb = pad_world_center(b, pad)
    return isqrt((pb.x - pa.x) ** 2 + (pb.y - pa.y) ** 2)


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
WIDE_YARD = Rectangle(Point(-6_000_000, -1_000_000), Point(6_000_000, 1_000_000))

PAIRED_PADS = (
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
)

WORLD = {
    "TINY": FootprintIR(entry_name="TINY", pads=UNIT_PAD, courtyard=TINY_YARD),
    "PAIRED": FootprintIR(entry_name="PAIRED", pads=PAIRED_PADS, courtyard=TINY_YARD),
    "WIDE": FootprintIR(entry_name="WIDE", pads=UNIT_PAD, courtyard=WIDE_YARD),
}


def net_board(*refs: str, nets=()) -> Board:
    """An unplaced Board over the given TINY comps (PAIRED for ``P*`` refs),
    with the given nets as ``((ref, pad), ...)`` pin tuples."""
    comps = tuple(
        CompIR(ref=ref, entry_name="PAIRED" if ref.startswith("P") else "TINY")
        for ref in refs
    )
    net_irs = tuple(NetIR(name=name, nodes=tuple(pins)) for name, pins in nets)
    return build_board(
        make_spec(netlist_ir=NetlistIR(comps=comps, nets=net_irs), footprint_irs=WORLD)
    )


def mixed_board(*refs: str) -> Board:
    """An unplaced Board over the given comps — WIDE (a 12 × 2 mm courtyard)
    for refs starting ``Z``, TINY (1 × 1 mm) otherwise: the visit-order
    tests' world, same unit pads at two different reaches."""
    comps = tuple(
        CompIR(ref=ref, entry_name="WIDE" if ref.startswith("Z") else "TINY")
        for ref in refs
    )
    return build_board(
        make_spec(netlist_ir=NetlistIR(comps=comps, nets=()), footprint_irs=WORLD)
    )


def placed(board: Board, ref: str, x: int, y: int, locked: bool = False) -> Board:
    return board.with_placement(ref, mm(x), mm(y), 0, "F.Cu", locked)


def error_findings(board: Board):
    return tuple(v for v in run_drc(board) if v.severity is Severity.ERROR)


class TestSurface:
    def test_result_is_frozen_with_the_three_fields(self):
        board = placed(net_board("R1"), "R1", 10, 10)
        result = solve(board, budget=0)
        assert isinstance(result, SolveResult)
        assert result.board is board
        assert result.unmet_constraint_ids == ()
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
        with pytest.raises(AttributeError):
            result.board = board

    def test_stop_reason_names_both_members(self):
        assert StopReason.NO_FURTHER_IMPROVEMENT.value == "no_further_improvement"
        assert StopReason.BUDGET_EXHAUSTED.value == "budget_exhausted"

    def test_errors_live_under_net2boarderror(self):
        board = placed(net_board("R1", "R2"), "R1", 10, 10)
        for bad_budget in (-1, True, 1.5, "5", None):
            with pytest.raises(Net2BoardError) as excinfo:
                solve(board, budget=bad_budget)
            assert isinstance(excinfo.value, SolveError)
        with pytest.raises(Net2BoardError) as excinfo:
            solve(board, refs=("R1", "R9"), budget=10)
        assert isinstance(excinfo.value, SolveRefError)
        assert isinstance(excinfo.value, SolveError)
        locked = placed(
            placed(net_board("R1", "R2"), "R1", 10, 10, locked=True), "R2", 20, 10
        )
        with pytest.raises(Net2BoardError) as excinfo:
            solve(locked, refs=("R1",), budget=10)
        assert isinstance(excinfo.value, SolveRefError)

    def test_ref_error_carries_the_ref(self):
        board = placed(net_board("R1", "R2"), "R1", 10, 10)
        with pytest.raises(SolveRefError) as excinfo:
            solve(board, refs=("R9",), budget=10)
        assert excinfo.value.ref == "R9"
        locked = placed(net_board("R1"), "R1", 10, 10, locked=True)
        with pytest.raises(SolveRefError) as excinfo:
            solve(locked, refs=("R1",), budget=10)
        assert excinfo.value.ref == "R1"

    def test_refs_rejects_a_bare_string(self):
        board = placed(net_board("R1", "R2"), "R1", 10, 10)
        with pytest.raises(SolveError):
            solve(board, refs="R1", budget=10)

    def test_budget_zero_is_a_valid_no_op(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                10,
                10,
            ),
            "R2",
            30,
            10,
        )
        result = solve(board, budget=0)
        assert result.board is board
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert result.unmet_constraint_ids == ()


class TestScope:
    def test_empty_scope_commits_nothing(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, refs=(), budget=10_000)
        assert result.board is board
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_scope_tolerates_duplicates_and_any_order(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, refs=("R2", "R2", "R1"), budget=10_000)
        assert isinstance(result, SolveResult)


class TestVisitOrder:
    """The canonical visit order (#52): board-derived reach descending —
    each in-scope ref's half-diagonal as its placement stands, unplaced
    refs measured at the origin anchor — ties on the lexicographic ref.
    The strand fix's seam: big parts attempt before the field converges
    around them."""

    def test_big_parts_attempt_before_small_ones(self):
        board = mixed_board("A1", "A2", "Z9")
        assert _visit_order(board, ("A1", "A2", "Z9")) == ("Z9", "A1", "A2")

    def test_equal_reach_ties_break_on_the_ref(self):
        board = mixed_board("R2", "R1")
        assert _visit_order(board, ("R2", "R1")) == ("R1", "R2")

    def test_placed_and_unplaced_tie_on_footprint_reach(self):
        # the half-diagonal reads the courtyard shape, not where the
        # placement stands: a WIDE part at 90° measures as its unplaced twin
        board = mixed_board("Z9", "Z8")
        board = board.with_placement("Z8", mm(10), mm(10), 90, "F.Cu")
        assert _visit_order(board, ("Z8", "Z9")) == ("Z8", "Z9")

    def test_the_order_is_deterministic(self):
        # the guard the total order buys: no dict/set iteration order
        # leaks through the sort key's reach computation
        board = mixed_board("A1", "Z9", "R2")
        assert _visit_order(board, ("A1", "Z9", "R2")) == _visit_order(
            board, ("A1", "Z9", "R2")
        )

    def test_empty_scope_stays_empty(self):
        board = mixed_board("A1")
        assert _visit_order(board, ()) == ()


class TestPlacement:
    def test_unplaced_ref_places_near_its_net_neighbours(self):
        board = placed(
            placed(
                net_board(
                    "R1",
                    "R2",
                    "R3",
                    nets=(
                        ("N1", (("R1", "1"), ("R2", "1"))),
                        ("N2", (("R2", "1"), ("R3", "1"))),
                    ),
                ),
                "R1",
                5,
                5,
            ),
            "R2",
            30,
            10,
        )
        result = solve(board, budget=10_000)
        assert result.board.unplaced_refs() == ()
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        distance = (
            pad_world_center(placed_map["R2"], "1").x
            - pad_world_center(placed_map["R3"], "1").x
        ) ** 2 + (
            pad_world_center(placed_map["R2"], "1").y
            - pad_world_center(placed_map["R3"], "1").y
        ) ** 2
        assert distance <= mm(6) ** 2
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_unplaced_ref_without_anchors_still_lands_legal(self):
        board = placed(net_board("R1", "R4"), "R1", 10, 10)
        result = solve(board, budget=10_000)
        assert result.board.unplaced_refs() == ()
        assert error_findings(result.board) == ()

    def test_unplaced_out_of_scope_stays_unplaced(self):
        board = placed(net_board("R1", "R4"), "R1", 10, 10)
        result = solve(board, refs=("R1",), budget=10_000)
        assert result.board.unplaced_refs() == ("R4",)


class TestLivePlacements:
    """_commit maintains the live placements map (#63): a netmate placed
    during the call anchors every later candidate family, so the place
    phase lands pad-aligned on it — never falling through to the coarse
    first-fit grid for want of an anchor. An all-unplaced entry is the
    regression face: _placements is empty at entry, so under the stale
    map every family stayed empty for the whole run."""

    def test_unplaced_pair_lands_pad_aligned_not_grid_aligned(self):
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        result = solve(board, budget=10_000)
        assert result.board.unplaced_refs() == ()
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        span = (
            _half_diagonal(placed_map["R1"])
            + _half_diagonal(placed_map["R2"])
            + _PLACEMENT_MARGIN_NM
        )
        r2_gap = pad_gap_nm(placed_map["R1"], placed_map["R2"])
        assert r2_gap == span
        r2 = pad_world_center(placed_map["R2"], "1")
        assert not (
            r2.x % _GRID_PITCH_NM == _GRID_INSET_NM % _GRID_PITCH_NM
            and r2.y % _GRID_PITCH_NM == _GRID_INSET_NM % _GRID_PITCH_NM
        )

    def test_netmate_placed_during_the_call_anchors_the_next_family(self):
        # R3's only netmate is R2, placed mid-call: the chain lands
        # pad-aligned end to end only if R2's commit is visible to R3's
        # family — the stale map sent R3 to the fallback grid
        board = net_board(
            "R1",
            "R2",
            "R3",
            nets=(
                ("N1", (("R1", "1"), ("R2", "1"))),
                ("N2", (("R2", "1"), ("R3", "1"))),
            ),
        )
        result = solve(board, budget=10_000)
        assert result.board.unplaced_refs() == ()
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        gap_12 = (
            _half_diagonal(placed_map["R1"])
            + _half_diagonal(placed_map["R2"])
            + _PLACEMENT_MARGIN_NM
        )
        gap_23 = (
            _half_diagonal(placed_map["R2"])
            + _half_diagonal(placed_map["R3"])
            + _PLACEMENT_MARGIN_NM
        )
        assert pad_gap_nm(placed_map["R1"], placed_map["R2"]) == gap_12
        assert pad_gap_nm(placed_map["R2"], placed_map["R3"]) == gap_23

    def test_the_searched_board_is_a_fixed_point(self):
        # the searched board is not a fixed point of solve when the map
        # is stale: the re-solve's families (populated at entry) find
        # what the first run's never could
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        result = solve(board, budget=10_000)
        again = solve(result.board, budget=10_000)
        assert again.board is result.board
        assert again.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT


class TestDeterminism:
    def test_same_input_same_result_and_same_bytes(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        first = solve(board, budget=10_000)
        second = solve(board, budget=10_000)
        assert first == second
        assert export_pcb(first.board) == export_pcb(second.board)

    def test_resolving_a_solved_board_returns_it(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, budget=10_000)
        again = solve(result.board, budget=10_000)
        assert again.board is result.board
        assert again.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert again.unmet_constraint_ids == ()


class TestFailureAsValue:
    def test_unsatisfiable_requirement_is_reported_not_raised(self):
        # a cross-side pair under same_side: no translation satisfies it,
        # and rotation/side are caller-fixed in M2 — unmet, never an error
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        board = board.with_constraint(
            Constraint(
                id="near",
                kind=Requirement(),
                relation=Proximity(
                    ref_a="R1",
                    pad_a="1",
                    ref_b="R2",
                    pad_b="1",
                    bound_nm=mm(2),
                    same_side=True,
                ),
            )
        )
        board = placed(board, "R1", 10, 10)
        board = board.with_placement("R2", mm(30), mm(10), 0, "B.Cu")
        result = solve(board, budget=50_000)
        assert result.unmet_constraint_ids == ("near",)
        findings = error_findings(result.board)
        assert any(finding.type.value == "proximity_unmet" for finding in findings)

    def test_result_board_passes_legalize_identity(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, budget=10_000)
        assert legalize(result.board).board is result.board

    def test_illegal_result_also_passes_legalize_identity(self):
        # the unsatisfiable pair leaves the board honestly illegal; the
        # final legalize phase has already run to its named-failure fixpoint
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        board = board.with_constraint(
            Constraint(
                id="near",
                kind=Requirement(),
                relation=Proximity(
                    ref_a="R1",
                    pad_a="1",
                    ref_b="R2",
                    pad_b="1",
                    bound_nm=mm(2),
                    same_side=True,
                ),
            )
        )
        board = placed(board, "R1", 10, 10)
        board = board.with_placement("R2", mm(30), mm(10), 0, "B.Cu")
        result = solve(board, budget=50_000)
        assert legalize(result.board).board is result.board


class TestEcc83:
    """The fixture face: solve improves the Objective over the hand-tuned
    acceptance table and stays legal — the EdgeMounts included."""

    def test_improves_the_hand_table_within_the_budget(self):
        board = apply_placements(build_board(load_ecc83_spec()))
        before = evaluate(board).total_nm
        result = solve(board, budget=50_000)
        assert evaluate(result.board).total_nm < before
        assert error_findings(result.board) == ()
        assert result.unmet_constraint_ids == ()
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_deterministic_and_identity_on_resolve(self):
        board = apply_placements(build_board(load_ecc83_spec()))
        first = solve(board, budget=50_000)
        second = solve(board, budget=50_000)
        assert first == second
        assert export_pcb(first.board) == export_pcb(second.board)
        again = solve(first.board, budget=50_000)
        assert again.board is first.board


class TestImprovement:
    def test_objective_improves_and_board_stays_legal(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        before = evaluate(board).total_nm
        result = solve(board, budget=100_000)
        assert evaluate(result.board).total_nm < before
        assert error_findings(result.board) == ()
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_only_in_scope_refs_move(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, refs=("R1",), budget=100_000)
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["R2"].pos == Point(mm(35), mm(25))
        assert placed_map["R1"].pos != Point(mm(5), mm(5))

    def test_locked_never_moves_even_in_default_scope(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
                locked=True,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, budget=100_000)
        placed_map = {p.ref: p for p in result.board.placements}
        assert placed_map["R1"].pos == Point(mm(5), mm(5))
        assert placed_map["R1"].locked is True
        assert placed_map["R2"].pos != Point(mm(35), mm(25))

    def test_never_exceeds_the_budget(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        for budget in (0, 1, 7, 50, 5_000):
            probes = self._counted_solve(board, budget)
            assert probes <= budget
            if budget:
                assert probes > 0

    def test_tiny_budget_stops_exhausted(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, budget=1)
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED

    def test_generous_budget_reaches_no_further_improvement(self):
        board = placed(
            placed(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                "R1",
                5,
                5,
            ),
            "R2",
            35,
            25,
        )
        result = solve(board, budget=100_000)
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_rotated_mover_aligns_its_net_pad(self):
        # P1 sits rotated 90°; the pad-alignment candidates must compose
        # that rotation exactly once, so its net pad ("2") — not the pad
        # one slot over — ends up nearest R1's pad
        board = placed(
            placed(
                net_board("R1", "P1", nets=(("N1", (("R1", "1"), ("P1", "2"))),)),
                "R1",
                5,
                5,
            ),
            "P1",
            35,
            25,
        )
        board = board.with_placement("P1", mm(35), mm(25), 90, "F.Cu")
        result = solve(board, budget=100_000)
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        r1 = pad_world_center(placed_map["R1"], "1")
        p1_pad1 = pad_world_center(placed_map["P1"], "1")
        p1_pad2 = pad_world_center(placed_map["P1"], "2")
        near = (p1_pad2.x - r1.x) ** 2 + (p1_pad2.y - r1.y) ** 2
        far = (p1_pad1.x - r1.x) ** 2 + (p1_pad1.y - r1.y) ** 2
        assert near < far

    @staticmethod
    def _counted_solve(board: Board, budget: int) -> int:
        """solve with a counting wrapper on the Evaluator's counted
        verdict — the candidates-evaluated ledger (one candidate = one
        ``probe_key`` read, ADR-0016 as amended by #64). Patches the seam
        the search actually reads: a wrapper on any other method counts
        zero forever, and ``probes <= budget`` passes vacuously."""
        import net2board.solver._solve as solve_module

        real_probe_key = solve_module.Evaluator.probe_key
        count = 0

        def counting_probe_key(self, ref, pos):
            nonlocal count
            count += 1
            return real_probe_key(self, ref, pos)

        solve_module.Evaluator.probe_key = counting_probe_key
        try:
            solve(board, budget=budget)
        finally:
            solve_module.Evaluator.probe_key = real_probe_key
        return count
