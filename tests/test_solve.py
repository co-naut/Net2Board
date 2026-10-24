"""The solve seam — budgeted, deterministic, Board-in/Board-out (#46, ADR-0013).

The contract under test:

- ``solve(board, refs=None, *, budget)`` returns a frozen ``SolveResult(board,
  unmet_constraint_ids, stop_reason, candidates_evaluated)`` — no success
  boolean, no carried Violations, no cost reading; failure is a value the
  orchestrator branches on;
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

from conftest import SWIVEL_WORLD, counted_probes, load_ecc83_spec, make_spec
from net2board.build import build_board
from net2board.drc import Severity, run_drc
from net2board.errors import Net2BoardError
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_pcb
from net2board.geometry import Point, Rectangle, measure_box, pad_world_center
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import (
    ROTATIONS,
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


def mm(value: int) -> int:
    return value * 1_000_000


def pad_gap_nm(a, b, pad: str = "1") -> int:
    """The Euclidean pad-to-pad distance between two placements, in nm."""
    pa = pad_world_center(a, pad)
    pb = pad_world_center(b, pad)
    return isqrt((pb.x - pa.x) ** 2 + (pb.y - pa.y) ** 2)


def half_diagonal(placement) -> int:
    """Half the measure box's diagonal, from the public geometry — the
    reach the placement's courtyards clear in every direction."""
    box = measure_box(placement)
    width = box.max.x - box.min.x
    height = box.max.y - box.min.y
    return isqrt(width * width + height * height) // 2


def pad_margin_nm(a, b, pad: str = "1") -> int:
    """The placement margin a landed pair sits at, derived from the board
    itself: pad gap minus the two half-diagonals — the courtyards' face
    separation. Uniform across pad-aligned pairs by the engine's stance."""
    return pad_gap_nm(a, b, pad) - half_diagonal(a) - half_diagonal(b)


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


def mixed_board(*refs: str, nets=()) -> Board:
    """An unplaced Board over the given comps — WIDE (a 12 × 2 mm courtyard)
    for refs starting ``Z``, TINY (1 × 1 mm) otherwise: the visit-order
    tests' world, same unit pads at two different reaches — with the given
    nets as ``((ref, pad), ...)`` pin tuples."""
    comps = tuple(
        CompIR(ref=ref, entry_name="WIDE" if ref.startswith("Z") else "TINY")
        for ref in refs
    )
    net_irs = tuple(NetIR(name=name, nodes=tuple(pins)) for name, pins in nets)
    return build_board(
        make_spec(netlist_ir=NetlistIR(comps=comps, nets=net_irs), footprint_irs=WORLD)
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
    """The scope visits in a board-derived canonical order (#52), never
    the caller's listing or any container's iteration order — so the
    outcome is a function of the Board, not of how the refs were spelled.
    Netmates anchor to whichever part placed first, which is what makes a
    non-canonical order visible in the result board."""

    def test_the_scope_s_listing_order_does_not_change_the_result(self):
        # one WIDE + one TINY netmate: whichever visits first lands on the
        # fallback grid and the other pad-aligns onto it — flip the visit
        # order and the board comes out different
        def coupled(*listing):
            return solve(
                mixed_board("Z9", "R1", nets=(("N1", (("Z9", "1"), ("R1", "1"))),)),
                refs=listing,
                budget=10_000,
            )

        assert coupled("Z9", "R1") == coupled("R1", "Z9")
        assert coupled("Z9", "R1").board.unplaced_refs() == ()

    def test_equal_reach_ties_stay_canonical(self):
        # two TINY netmates: equal reach, so the lexicographic tie-break
        # is all that keeps the visit order — and the result — independent
        # of the caller's listing (a bare stable sort would leak it)
        def coupled(*listing):
            return solve(
                net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),)),
                refs=listing,
                budget=10_000,
            )

        assert coupled("R1", "R2") == coupled("R2", "R1")

    def test_an_empty_scope_touches_nothing(self):
        board = net_board("R1", "R2")
        result = solve(board, refs=(), budget=10_000)
        assert result.board is board
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_the_big_part_attempts_first(self):
        # the smallest budget that lands anything places the ref that
        # visited first — reach-descending means the WIDE part, with the
        # TINY netmate's family still starved. Self-calibrating: the
        # candidates a family spends is guidance, the order is the point.
        board = mixed_board("Z9", "R1", nets=(("N1", (("Z9", "1"), ("R1", "1"))),))
        for budget in range(1, 64):
            result = solve(board, budget=budget)
            if result.board.placements:
                break
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert result.board.placement("Z9") is not None
        assert result.board.placement("R1") is None


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
    """A netmate placed during the call anchors every later candidate
    family (#63, #67): the place phase lands pad-aligned on it — never
    falling through to the coarse first-fit grid for want of an anchor.
    The Evaluator is the one live state, so a commit is visible to every
    later read by construction; an all-unplaced entry is the regression
    face — under the old stale mirror the families stayed empty for the
    whole run."""

    def test_unplaced_pair_lands_pad_aligned(self):
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        result = solve(board, budget=10_000)
        assert result.board.unplaced_refs() == ()
        assert error_findings(result.board) == ()
        placed_map = {p.ref: p for p in result.board.placements}
        # pad-aligned contact, not the coarse first-fit grid: the pair's
        # separation is cardinal at exactly the contact distance their own
        # courtyards state (the shared placement margin), never an
        # arbitrary grid position
        r1, r2 = placed_map["R1"], placed_map["R2"]
        # the courtyards sit separated, never lapped: the derived margin
        # (pad gap minus both half-diagonals) is non-negative
        assert pad_margin_nm(r1, r2) >= 0
        # cardinal separation — a pad-aligned ring position, not the
        # coarse grid's arbitrary (x, y)
        a = pad_world_center(r1, "1")
        b = pad_world_center(r2, "1")
        assert (a.x == b.x) or (a.y == b.y)
        # the landing tracks its anchor: a fallback-grid landing would be
        # anchor-blind and return the same spot whatever R1 did
        shifted = solve(placed(board, "R1", 20, 20), budget=10_000).board
        assert {p.ref: p.pos for p in shifted.placements}["R2"] != r2.pos
        assert error_findings(shifted) == ()

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
        # the chain lands pad-aligned end to end: one uniform margin at
        # every hop (the engine's placement margin, derived here from the
        # board itself), each separation cardinal — the stale-mirror
        # regression sent R3 to the fallback grid, where neither holds
        r1, r2, r3 = (placed_map[ref] for ref in ("R1", "R2", "R3"))
        assert pad_margin_nm(r1, r2) == pad_margin_nm(r2, r3)
        assert pad_margin_nm(r1, r2) >= 0
        for a, b in (
            (pad_world_center(r1, "1"), pad_world_center(r2, "1")),
            (pad_world_center(r2, "1"), pad_world_center(r3, "1")),
        ):
            assert (a.x == b.x) or (a.y == b.y)

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
            probes = counted_probes(board, budget)
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


class TestStopReasonHonesty:
    """A budget that truncates a scan stops ``BUDGET_EXHAUSTED``, never
    ``NO_FURTHER_IMPROVEMENT`` (#107). NFI is ADR-0013's local-optimum
    claim — a full round, zero commits — and a round whose last scan the
    budget cut short has candidates still unevaluated: it has not earned the
    label. The flip point is exact (``solve(N)`` NFI, ``solve(N - 1)``
    exhausted, N the untruncated probe count), which is what makes a
    seam-reported candidate count (#102) well-defined. The boards are the
    spec's fast fixtures — where the truncation window was first measured
    (placed_r1: N = 397 against a minimal-NFI reading of 366) — and cover
    both faces between them: placed_r1 places mid-call, converged is the
    improve-only fixed point. Labels only: the Board for any argument
    tuple is unchanged — probes and commits happen exactly when they did
    before (the only difference is which label a truncated quiet scan
    reports), witnessed at the session level by the untouched ecc83
    golden."""

    @staticmethod
    def _fixtures() -> tuple[Board, ...]:
        from test_session_tinytapeout import converged_board, placed_r1_board

        return (placed_r1_board(), converged_board())

    def test_the_flip_point_is_exact(self):
        for board in self._fixtures():
            n = counted_probes(board, 1_000_000)
            assert solve(board, budget=n).stop_reason is (
                StopReason.NO_FURTHER_IMPROVEMENT
            )
            assert solve(board, budget=n - 1).stop_reason is (
                StopReason.BUDGET_EXHAUSTED
            )

    def test_no_truncated_budget_stops_nfi(self):
        for board in self._fixtures():
            n = counted_probes(board, 1_000_000)
            for budget in range(1, n):
                assert solve(board, budget=budget).stop_reason is (
                    StopReason.BUDGET_EXHAUSTED
                )

    def test_an_nfi_claim_survives_a_generous_re_solve(self):
        """The identity clause (ADR-0013) at every budget that claims it:
        a result stopping NFI re-solved with room to spare returns the
        very Board object — the label was never claimed on an unchecked
        board (#107's failure mode)."""
        for board in self._fixtures():
            n = counted_probes(board, 1_000_000)
            for budget in range(1, n + 8):
                result = solve(board, budget=budget)
                if result.stop_reason is not StopReason.NO_FURTHER_IMPROVEMENT:
                    continue
                again = solve(result.board, budget=1_000_000)
                assert again.board is result.board


class TestCandidatesEvaluated:
    """The seam reports its own spend (#102): ``candidates_evaluated`` on
    ``SolveResult`` is the honest count of Candidates the call evaluated —
    ``min(budget, N)``, N the untruncated count — read off the result
    where the budget lands. The counted wrapper in these tests is the
    tests' trick that keeps the field honest; the harness reads the field,
    never the wrapper."""

    @staticmethod
    def _pair() -> Board:
        board = net_board("R1", "R2", nets=(("N1", (("R1", "1"), ("R2", "1"))),))
        return placed(placed(board, "R1", 5, 5), "R2", 35, 25)

    def test_the_count_agrees_with_the_instrumented_ledger(self):
        board = self._pair()
        n = counted_probes(board, 1_000_000)
        for budget in (0, 1, n // 2, n - 1, n, 10_000):
            result = solve(board, budget=budget)
            assert result.candidates_evaluated == counted_probes(board, budget)
            assert result.candidates_evaluated == min(budget, n)

    def test_a_no_op_resolve_counts_its_quiet_round(self):
        """Identity on no-op stands; the count is the work done, not a
        claim about the outcome — one quiet improve round, honestly."""
        board = self._pair()
        first = solve(board, budget=10_000)
        again = solve(first.board, budget=10_000)
        assert again.board is first.board
        assert again.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert again.candidates_evaluated == counted_probes(first.board, 10_000)
        assert again.candidates_evaluated > 0


class TestFreeRotation:
    """The ``free_rotation`` knob (ADR-0021's contract, ADR-0025's search
    shape): ``solve`` gains one keyword, default ``False`` — knob off the
    behaviour is today's to the byte, knob on the search may set each
    unlocked in-scope ref's rotation in 90° steps, side staying
    caller-fixed.

    The world: P1 is a SWIVEL part — PAIRED's pads ((0, 0) and (2 mm, 0),
    unit circles) under a yard that actually covers them ((-1, -1) to
    (3, 1) mm, so 4 × 2 mm at rotation 0 and 2 × 4 mm at 90°) — with
    netmates T1 at (20, 8) and T2 at (20, 20), **locked**: physically
    committed parts, so the movers' side of every outcome below is P1
    alone and both knob states' final boards are hand-derived. The net's
    pads want the x = 20 mm column: a rotated P1 stacks both pads onto
    the column (NetSpan 12 mm), a rotation-0 P1 drags pad 2 to x = 22
    (NetSpan 14 mm). Spans from the public geometry: SWIVEL's
    half-diagonal is 2_236_067 nm in every rotation — width and height
    swap at 90° — TINY's is 707_106 nm, the placement margin 1_000 nm,
    so the reach ring sits 2_944_173 nm from a netmate's pad:

    - knob off: P1 lands at (20 mm, 10.944173 mm) rotation 0, total
      14 mm — the first legal ring candidate tying the rotation-0 floor
      (the flush candidates overlap the netmates; ties after it lose the
      pose tie-break on higher y);
    - knob on: P1 lands at (20 mm, 10.944173 mm) rotation 270, total
      12 mm — the column floor, reached at the same ring spot: rotation
      90 is illegal there (its yard falls back into T1's), 270 stacks
      pad 2 down the column, and its lower y beats the rotation-90
      candidates on the pose tie-break.
    """

    _RING_SPAN = (
        isqrt(mm(4) ** 2 + mm(2) ** 2) // 2  # SWIVEL's half-diagonal —
        # rotation-invariant (width and height swap, the sum of squares
        # does not)
        + isqrt(mm(1) ** 2 + mm(1) ** 2) // 2  # TINY's
        + 1_000  # the placement margin
    )
    """Distance from a netmate's pad of the reach-ring candidates: 2_944_173
    nm, the same in every rotation."""

    @staticmethod
    def rotator_board() -> Board:
        """P1 unplaced, netmates placed and locked: the board both knob
        states solve."""
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(
                    comps=(
                        CompIR(ref="P1", entry_name="SWIVEL"),
                        CompIR(ref="T1", entry_name="TINY"),
                        CompIR(ref="T2", entry_name="TINY"),
                    ),
                    nets=(
                        NetIR(
                            name="N",
                            nodes=(
                                ("P1", "1"),
                                ("P1", "2"),
                                ("T1", "1"),
                                ("T2", "1"),
                            ),
                        ),
                    ),
                ),
                footprint_irs=SWIVEL_WORLD,
            )
        )
        return placed(
            placed(board, "T1", 20, 8, locked=True), "T2", 20, 20, locked=True
        )

    def test_the_knob_rejects_non_bool(self):
        board = self.rotator_board()
        for bad_knob in (1, 0, "yes", None, 1.5):
            with pytest.raises(Net2BoardError) as excinfo:
                solve(board, budget=10_000, free_rotation=bad_knob)
            assert isinstance(excinfo.value, SolveError)

    def test_knob_on_is_accepted_and_knob_off_shape_unchanged(self):
        """The knob's default: knob off P1 lands exactly as the pre-knob
        search landed it — rotation 0, the hand-derived rotation-0
        optimum."""
        span = self._RING_SPAN
        result = solve(self.rotator_board(), budget=100_000)
        placement = result.board.placement("P1")
        assert (placement.pos.x, placement.pos.y, placement.rotation) == (
            mm(20),
            mm(8) + span,
            0,
        )
        assert evaluate(result.board).total_nm == mm(14)
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_knob_on_accepted(self):
        """``free_rotation=True`` is a valid call — the search runs and
        reports through the same ``SolveResult`` surface."""
        result = solve(self.rotator_board(), budget=100_000, free_rotation=True)
        assert isinstance(result, SolveResult)
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_knob_on_places_at_the_best_rotation(self):
        """Knob on, place searches the per-rotation alignment families and
        lands P1 on the x = 20 mm column at rotation 270 — the hand-derived
        12 mm pose (rotation 90's yard falls back into locked T1's at the
        winning ring spot; 270 stacks pad 2 down the column and wins the
        pose tie-break by lower y). Knob off the same board stays at
        14 mm, rotation 0."""
        result = solve(self.rotator_board(), budget=100_000, free_rotation=True)
        placement = result.board.placement("P1")
        assert (
            placement.pos.x,
            placement.pos.y,
            placement.rotation,
        ) == (mm(20), mm(8) + self._RING_SPAN, 270)
        assert evaluate(result.board).total_nm == mm(12)
        for ref in ("P1", "T1", "T2"):
            assert result.board.placement(ref).rotation in ROTATIONS

    def test_knob_on_turns_a_caller_placed_part_in_place(self):
        """A caller's rotation is a starting Pose, not a preference
        (ADR-0021): P1 pre-placed unlocked at the knob-off optimum —
        rotation 0, total 14 mm — is re-posed by improve's in-place turn
        at no stickiness cost: same position, rotation 270, total 12 mm.
        Knob off the same board is a strict local optimum: the search
        commits nothing and returns the very Board object."""
        board = self.rotator_board().with_placement(
            "P1", mm(20), mm(8) + self._RING_SPAN, 0, "F.Cu"
        )
        knob_off = solve(board, budget=100_000)
        assert knob_off.board is board
        assert knob_off.board.placement("P1").rotation == 0
        knob_on = solve(board, budget=100_000, free_rotation=True)
        placement = knob_on.board.placement("P1")
        assert (
            placement.pos.x,
            placement.pos.y,
            placement.rotation,
        ) == (mm(20), mm(8) + self._RING_SPAN, 270)
        assert placement.locked is False
        assert evaluate(knob_on.board).total_nm == mm(12)

    def test_knob_on_does_not_churn_an_optimal_placement(self):
        """P1 pre-placed already at the knob-on optimum (rotation 270,
        12 mm): the knob-on search finds no strictly improving candidate —
        turns included — and returns the very Board object. The knob
        widens the family, it never forces a move."""
        board = self.rotator_board().with_placement(
            "P1", mm(20), mm(8) + self._RING_SPAN, 270, "F.Cu"
        )
        result = solve(board, budget=100_000, free_rotation=True)
        assert result.board is board
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT

    def test_the_knob_is_scoped_to_the_named_refs(self):
        """``refs`` plus the knob (ADR-0025: granularity is ``refs`` plus
        step composition): P2 is named, so its placement searches all
        four rotations and lands rotated on its own column; P1 — unlocked
        but unnamed — keeps its caller rotation 0, and the unlocked
        out-of-scope netmates are never probed at all."""
        board = self.scoped_board()
        result = solve(board, refs=("P2",), budget=100_000, free_rotation=True)
        placement = result.board.placement("P2")
        assert (
            placement.pos.x,
            placement.pos.y,
            placement.rotation,
        ) == (mm(30), mm(8) + self._RING_SPAN, 270)
        p1 = result.board.placement("P1")
        assert (p1.pos.x, p1.pos.y, p1.rotation) == (
            mm(20),
            mm(8) + self._RING_SPAN,
            0,
        )
        for ref in ("T3", "T4"):
            assert result.board.placement(ref).rotation == 0
        assert evaluate(result.board).total_nm == mm(26)

    def test_locked_keeps_its_whole_pose(self):
        """Locked means the engine may not change the Placement — neither
        Pose nor side (ADR-0021): P1 pre-placed locked at rotation 90 (a
        legal pose, one millimetre down the column from the unlocked
        optimum) stands byte-identical through a knob-on solve; the
        locked netmates never enter scope, so the search has no mover —
        the same Board object comes back."""
        board = self.rotator_board().with_placement(
            "P1", mm(20), mm(8) + self._RING_SPAN + mm(1), 90, "F.Cu", True
        )
        result = solve(board, budget=100_000, free_rotation=True)
        assert result.board is board
        placement = result.board.placement("P1")
        assert (
            placement.pos.x,
            placement.pos.y,
            placement.rotation,
            placement.locked,
        ) == (mm(20), mm(8) + self._RING_SPAN + mm(1), 90, True)

    def test_the_grid_fallback_stays_at_rotation_0(self):
        """The first-fit grid fallback never rotates (ADR-0025): a ref
        with no nets generates no alignment candidates, so place falls
        through to the outline scan and lands at its first-fit spot —
        rotation 0, even knob on. The part's own turns and ring tie
        with the standing pose afterwards, so the search commits
        nothing more."""
        board = net_board("R1")
        result = solve(board, budget=100_000, free_rotation=True)
        placement = result.board.placement("R1")
        assert (placement.pos.x, placement.pos.y, placement.rotation) == (
            mm(1),
            mm(1),
            0,
        )

    @staticmethod
    def scoped_board() -> Board:
        """Two independent columns: P1 pre-placed (rotation 0, the knob-off
        pose) with locked netmates T1/T2 on net N; P2 unplaced with
        unlocked netmates T3/T4 on net M — the same geometry one column
        right (x = 30 mm), so P2's hand-derived knob-on outcome mirrors
        P1's placement test."""
        board = build_board(
            make_spec(
                netlist_ir=NetlistIR(
                    comps=(
                        CompIR(ref="P1", entry_name="SWIVEL"),
                        CompIR(ref="P2", entry_name="SWIVEL"),
                        CompIR(ref="T1", entry_name="TINY"),
                        CompIR(ref="T2", entry_name="TINY"),
                        CompIR(ref="T3", entry_name="TINY"),
                        CompIR(ref="T4", entry_name="TINY"),
                    ),
                    nets=(
                        NetIR(
                            name="N",
                            nodes=(
                                ("P1", "1"),
                                ("P1", "2"),
                                ("T1", "1"),
                                ("T2", "1"),
                            ),
                        ),
                        NetIR(
                            name="M",
                            nodes=(
                                ("P2", "1"),
                                ("P2", "2"),
                                ("T3", "1"),
                                ("T4", "1"),
                            ),
                        ),
                    ),
                ),
                footprint_irs=SWIVEL_WORLD,
            )
        )
        board = placed(
            placed(board, "T1", 20, 8, locked=True), "T2", 20, 20, locked=True
        )
        board = placed(placed(board, "T3", 30, 8), "T4", 30, 20)
        return board.with_placement(
            "P1", mm(20), mm(8) + TestFreeRotation._RING_SPAN, 0, "F.Cu"
        )

    def test_generation_order_knob_off_prefix_first(self):
        """ADR-0025 §17's binding order, read whitebox off the private
        ``_Search`` (the order binds only under budget truncation, which
        no seam-level observation can separate from selection). The
        knob-off alignment family is pinned in full, hand-derived: flush
        onto T1's pad, the eight reach-ring candidates around it in the
        engine's direction order, then the same around T2, then the
        net-AABB centroid. A placed mover's knob-on improvement family
        is the knob-off family exactly as generated (alignments, then
        the local ring, the current Pose popped — the +y alignment *is*
        the mover's position), then the three in-place turns ascending,
        then the other rotations' alignment families ascending."""
        import net2board.solver._solve as solve_module
        from net2board.solver._evaluator import Pose

        span = self._RING_SPAN
        current = (mm(20), mm(8) + span)

        def family(y_pad: int) -> tuple[Pose, ...]:
            """The alignment family around one netmate's pad at
            (20 mm, y_pad), hand-derived."""
            return (
                Pose(mm(20), y_pad, 0),  # flush onto the pad
                Pose(mm(20) + span, y_pad, 0),  # E
                Pose(mm(20) - span, y_pad, 0),  # W
                Pose(mm(20), y_pad + span, 0),  # +y
                Pose(mm(20), y_pad - span, 0),  # -y
                Pose(mm(20) + span, y_pad + span, 0),  # NE
                Pose(mm(20) + span, y_pad - span, 0),  # SE
                Pose(mm(20) - span, y_pad + span, 0),  # NW
                Pose(mm(20) - span, y_pad - span, 0),  # SW
            )

        board = self.rotator_board().with_placement(
            "P1", current[0], current[1], 0, "F.Cu"
        )
        knob_off = solve_module._Search(board, ("P1",), 0)
        expected = (
            family(mm(8))
            + family(mm(20))
            + (Pose(mm(20), mm(14), 0),)  # the net-AABB centroid
        )
        assert knob_off._placement_candidates("P1") == expected

        knob_on = solve_module._Search(board, ("P1",), 0, free_rotation=True)
        on_moves = knob_on._move_candidates("P1")
        off_moves = knob_off._move_candidates("P1")
        assert Pose(current[0], current[1], 0) not in off_moves
        assert on_moves[: len(off_moves)] == off_moves
        assert on_moves[len(off_moves) : len(off_moves) + 3] == (
            Pose(current[0], current[1], 90),
            Pose(current[0], current[1], 180),
            Pose(current[0], current[1], 270),
        )
        rest = on_moves[len(off_moves) + 3 :]
        rest_rotations = [pose.rotation for pose in rest]
        assert rest_rotations == sorted(rest_rotations)
        assert set(rest_rotations) == {90, 180, 270}

    def test_placement_order_standing_first_then_rotations_ascending(self):
        """Place's family order (ADR-0025 §17, same whitebox): the
        standing rotation's alignments first — for an unplaced ref,
        rotation 0, exactly the knob-off family — then the other
        rotations' alignment families ascending. No turns: an unplaced
        ref has no current position to turn in place at."""
        import net2board.solver._solve as solve_module

        board = self.rotator_board()
        knob_off = solve_module._Search(board, ("P1",), 0)
        knob_on = solve_module._Search(board, ("P1",), 0, free_rotation=True)
        off_placements = knob_off._placement_candidates("P1")
        on_placements = knob_on._placement_candidates("P1")
        assert on_placements[: len(off_placements)] == off_placements
        rest = on_placements[len(off_placements) :]
        rest_rotations = [pose.rotation for pose in rest]
        assert rest_rotations == sorted(rest_rotations)
        assert set(rest_rotations) == {90, 180, 270}
