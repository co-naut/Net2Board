"""The tiny_tapeout slow-tier acceptance face (ADR-0019, #49).

The fast-tier section exercises the session module's measurement machinery
with fakes only — the candidate-count derivation (ADR-0013's determinism
read through the public seam, no clocks anywhere) and the wrapper-baseline
loop (injected evaluator and clock). The slow-tier section at the bottom
is the real acceptance run: the derived ratio, the linearity receipt, the
tiny replay, and the ADR-0001 re-measures — opt-in with ``pytest -m slow``.
"""

import shutil

import pytest

from conftest import TINY_OUTLINE, fresh_board
from net2board.examples.session import (
    WrapperBaseline,
    WrapperSample,
    candidate_count,
    measure_wrapper_baseline,
    move_mix,
    ring_variants,
    typical_move_us,
)
from net2board.model import Board
from net2board.solver import StopReason, solve


def placed_r1_board() -> Board:
    """R1 placed, R2/R3 unplaced — a small board with real search work."""
    return fresh_board().with_placement("R1", 5_000_000, 5_000_000, 0, "F.Cu")


def converged_board() -> Board:
    """All three comps placed and searched to the local optimum — the
    shape both improve-round readings take (``move_mix``'s bucket-sum
    identity and ``typical_move_us``'s truncated-half difference hold on
    a fixed point, where candidate families depend on board state
    alone)."""
    board = fresh_board().with_placement("R1", 5_000_000, 5_000_000, 0, "F.Cu")
    board = board.with_placement("R2", 20_000_000, 5_000_000, 0, "F.Cu")
    board = board.with_placement("R3", 30_000_000, 20_000_000, 0, "F.Cu")
    return solve(board, budget=100_000).board


class FakeClock:
    """A clock that only moves when told — the fast tier reads no wall."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestCandidateCount:
    """The harness derives the session's candidate count from the seam —
    never by instrumenting the Evaluator (that is the tests' trick, not
    the harness's) and never by widening ``SolveResult`` (ADR-0013's
    frozen shape). Budget checks cut the search short without changing
    its path, so spent == min(budget, N): the minimal budget whose stop
    reason flips to ``NO_FURTHER_IMPROVEMENT`` is exactly N."""

    def test_the_derivation_is_the_flip_point(self):
        board = placed_r1_board()
        count = candidate_count(board, ceiling=1_000_000)
        assert count >= 1
        assert solve(board, budget=count).stop_reason is (
            StopReason.NO_FURTHER_IMPROVEMENT
        )
        assert solve(board, budget=count - 1).stop_reason is (
            StopReason.BUDGET_EXHAUSTED
        )

    def test_a_ceiling_that_exhausts_reads_as_itself(self):
        board = placed_r1_board()
        assert candidate_count(board, ceiling=1) == 1

    def test_the_derivation_replays(self):
        board = placed_r1_board()
        assert candidate_count(board, ceiling=1_000_000) == candidate_count(
            board, ceiling=1_000_000
        )

    def test_scoped_refs_are_honoured(self):
        board = placed_r1_board()
        whole = candidate_count(board, ceiling=1_000_000)
        scoped = candidate_count(board, refs=("R2",), ceiling=1_000_000)
        assert 1 <= scoped < whole

    def test_the_derivation_reads_at_or_under_the_true_count(self):
        """The minimal-NFI budget is not the untruncated probe count
        (the docstring's claim, #51): a truncated final-round scan that
        finds no improvement still stops ``NO_FURTHER_IMPROVEMENT``, so
        the derivation can read low. Instrumentation is the tests' trick,
        never the harness's."""
        from net2board.solver import _solve

        board = converged_board()
        counted = {"n": 0}
        original = _solve._Search._probe

        def counting(self, ref, pos):
            counted["n"] += 1
            return original(self, ref, pos)

        _solve._Search._probe = counting
        try:
            true_run = solve(board, budget=1_000_000)
        finally:
            _solve._Search._probe = original
        assert true_run.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert counted["n"] > 0
        assert candidate_count(board, ceiling=1_000_000) <= counted["n"]


class TestMoveMix:
    """The improve-round move mix (ADR-0019's report-only reading):
    bucketed by footprint pad count — R1/R2 carry RES's 3 pads (the extra
    pad counts: transforms walk the footprint), R3 BIGRES's 2, and the
    ``9+`` bucket is empty here. Walls, not counts: a round's true probe
    count is not derivable through the seam (a truncated final-round scan
    that finds no improvement still stops ``NO_FURTHER_IMPROVEMENT``), so
    the buckets carry fixed-cost-subtracted walls."""

    def test_movers_land_in_their_pad_bucket(self):
        board = converged_board()
        buckets = {
            bucket.label: bucket
            for bucket in move_mix(board, ceiling=100_000, clock=FakeClock())
        }
        assert buckets["1-2 pads"].movers == 1
        assert buckets["3-8 pads"].movers == 2
        assert buckets["9+ pads"].movers == 0
        assert buckets["9+ pads"].wall_s == 0.0

    def test_a_stopped_clock_leaves_a_nonnegative_wall(self):
        board = converged_board()
        for bucket in move_mix(board, ceiling=100_000, clock=FakeClock()):
            assert bucket.wall_s >= 0.0


class TestTypicalMoveUs:
    """The typical-move reading: the slope of two budgeted solves over the
    seam's derived probe scale — the injected clock's four reads (start/end
    per solve) fully determine it, so the arithmetic is assertable without
    a wall."""

    def test_the_slope_isolates_the_per_probe_cost(self):
        board = converged_board()
        expected_scale = candidate_count(board, ceiling=100_000)
        clock = iter([0.0, 3.0, 3.0, 4.0]).__next__
        scale, per_probe_us = typical_move_us(board, ceiling=100_000, clock=clock)
        assert scale == expected_scale
        assert per_probe_us == (3.0 - 1.0) / (scale - scale // 2) * 1_000_000

    def test_a_stopped_clock_reads_zero(self):
        board = converged_board()
        scale, per_probe_us = typical_move_us(board, ceiling=100_000, clock=FakeClock())
        assert scale > 0
        assert per_probe_us == 0.0


class TestRingVariants:
    """The wrapper loop's candidate family: deterministic one-part moves —
    what a placement-wrapper search probes, full board each time."""

    def test_variants_are_distinct_moves_of_one_ref(self):
        base = placed_r1_board().with_placement("R2", 20_000_000, 15_000_000, 0, "F.Cu")
        variants = ring_variants(base, "R2", count=7)
        assert len(variants) == 7
        positions = {
            next(p.pos for p in v.placements if p.ref == "R2") for v in variants
        }
        assert len(positions) == 7
        base_r2 = next(p.pos for p in base.placements if p.ref == "R2")
        assert all(pos != base_r2 for pos in positions)

    def test_everything_but_the_target_is_untouched(self):
        base = placed_r1_board().with_placement("R2", 20_000_000, 15_000_000, 0, "F.Cu")
        for variant in ring_variants(base, "R2", count=5):
            assert variant.placements != base.placements
            others = tuple(p for p in variant.placements if p.ref != "R2")
            base_others = tuple(p for p in base.placements if p.ref != "R2")
            assert others == base_others

    def test_variants_stay_inside_the_outline(self):
        base = placed_r1_board().with_placement("R2", 39_000_000, 15_000_000, 0, "F.Cu")
        for variant in ring_variants(base, "R2", count=30):
            r2 = next(p.pos for p in variant.placements if p.ref == "R2")
            assert TINY_OUTLINE.min.x <= r2.x <= TINY_OUTLINE.max.x


class TestWrapperBaseline:
    """The wrapper-baseline loop over an injected evaluator and clock: the
    fast tier proves the bookkeeping (one reading per candidate, the mean,
    the spread receipt); the slow tier plugs in export + kicad-cli."""

    def test_one_reading_per_candidate_and_the_mean(self):
        clock = FakeClock()
        boards = [object()] * 5

        def evaluate(board):
            clock.advance(2.0)

        baseline = measure_wrapper_baseline(boards, evaluate, clock=clock)
        assert baseline.candidates == 5
        assert [sample.index for sample in baseline.samples] == [0, 1, 2, 3, 4]
        assert all(sample.seconds == 2.0 for sample in baseline.samples)
        assert baseline.per_candidate_s == 2.0

    def test_the_receipt_carries_the_measured_spread(self):
        clock = FakeClock()
        costs = [1.0, 2.0, 4.0]
        spent: list[float] = []

        def evaluate_by_index(board):
            seconds = costs[len(spent)]
            spent.append(seconds)
            clock.advance(seconds)

        baseline = measure_wrapper_baseline(
            [object()] * 3, evaluate_by_index, clock=clock
        )
        assert baseline.candidates == 3
        assert [sample.seconds for sample in baseline.samples] == costs
        assert baseline.per_candidate_s == sum(costs) / 3
        assert baseline.slowest_over_fastest == 4.0

    def test_no_candidates_is_a_caller_error(self):
        with pytest.raises(ValueError, match="20"):
            measure_wrapper_baseline([], lambda board: None, clock=FakeClock())


class TestPrintedReport:
    """The slow face's printed block, smoked over a synthetic report —
    the wrapper baseline named first (the harness's first measurement
    act), the ratio line carrying its floor."""

    def test_the_block_leads_with_the_wrapper_and_carries_the_ratio(self, capsys):
        import dataclasses

        from net2board.examples.session import (
            AcceptanceReport,
            Adr0001Remeasures,
            Exports,
            MacroRow,
            MicroRow,
            MoveMixBucket,
            Provenance,
            _print_measurements,
        )

        wrapper = WrapperBaseline(
            candidates=30,
            per_candidate_s=1.0,
            slowest_over_fastest=1.2,
            samples=tuple(WrapperSample(index=i, seconds=1.0) for i in range(30)),
        )
        macro = MacroRow(
            wall_s=2.0,
            candidates_evaluated=2_000,
            candidates_per_s=1_000.0,
            wrapper=wrapper,
            baseline_derived_s=2_000.0,
            baseline_arithmetic="1.000000 s/candidate x 2,000 candidates = 2,000.0 s",
            ratio=1_000.0,
            replay_byte_equal=True,
            objective_before_nm=0,
            objective_after_nm=5,
            stop_reason="no_further_improvement",
            unmet_constraint_ids=(),
            stranded=("J9",),
            move_mix=(
                MoveMixBucket(label="1-2 pads", movers=90, wall_s=0.62),
                MoveMixBucket(label="3-8 pads", movers=50, wall_s=0.31),
                MoveMixBucket(label="9+ pads", movers=9, wall_s=0.07),
            ),
            typical_move_us=81.4,
        )
        micro = MicroRow(
            wall_s=1.0,
            candidates_evaluated=100,
            candidates_per_step=(("improve", 100),),
            engine_per_candidate_s=0.01,
            wrapper=wrapper,
            ratio=100.0,
        )
        report = AcceptanceReport(
            provenance=Provenance(
                python="3.12",
                kicad_cli="10.0.6",
                jar_sha256="0" * 64,
                os="test",
                cpu="test",
                fixtures=(("ecc83", "15"),),
                budgets=(("BUDGET_TINY", 500_000),),
            ),
            tiny_tapeout_macro=macro,
            ecc83_micro=micro,
            adr0001_remeasures=Adr0001Remeasures(
                board_kib_at_150=1.0,
                retained_snapshot_kib=0.2,
                with_placement_us_at_150=7.0,
            ),
            exports=Exports(tiny_tapeout="", ecc83=""),
        )
        assert dataclasses.asdict(report)["tiny_tapeout_macro"]["ratio"] == 1_000.0

        _print_measurements(report)
        out = capsys.readouterr().out
        assert out.index("first act") < out.index("per-session (macro)")
        assert "ratio: 1,000x (floor 1,000x)" in out
        assert "adr-0001 re-measures" in out
        assert "move mix (improve-round wall)" in out
        assert "1-2 pads 62%" in out
        assert "typical move 81.4 us/probe" in out


ORACLE_HINT = "the slow tier is a local sign-off step, run with `pytest -m slow`"


@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("kicad-cli") is None,
    reason="kicad-cli absent — " + ORACLE_HINT + " (ADR-0019)",
)
class TestTinyAcceptance:
    """The real slow-tier acceptance face (ADR-0019, #49): the derived
    ratio with its floor, the linearity receipt, the replay, and the
    headline's pinned readings. The wrapper leg already ran kicad-cli for
    real inside the session-scoped fixture — these assertions read the
    report it produced."""

    def test_the_ratio_clears_the_registered_floor(self, tiny_acceptance):
        macro = tiny_acceptance.tiny_tapeout_macro
        assert macro.ratio >= 1_000, macro.baseline_arithmetic

    def test_the_baseline_is_derived_not_hardcoded(self, tiny_acceptance):
        from net2board.examples.session import BUDGET_TINY, WRAPPER_CANDIDATES_TINY

        macro = tiny_acceptance.tiny_tapeout_macro
        wrapper = macro.wrapper
        assert wrapper.candidates == WRAPPER_CANDIDATES_TINY
        assert 20 <= wrapper.candidates <= 50
        assert wrapper.per_candidate_s > 0
        assert macro.baseline_derived_s == pytest.approx(
            wrapper.per_candidate_s * macro.candidates_evaluated
        )
        assert macro.candidates_evaluated >= 1_000
        assert macro.candidates_evaluated <= BUDGET_TINY

    def test_the_linearity_receipt_is_recorded(self, tiny_acceptance):
        wrapper = tiny_acceptance.tiny_tapeout_macro.wrapper
        assert len(wrapper.samples) == wrapper.candidates
        assert all(sample.seconds > 0 for sample in wrapper.samples)
        assert [sample.index for sample in wrapper.samples] == list(
            range(wrapper.candidates)
        )
        assert wrapper.slowest_over_fastest > 0

    def test_the_headline_replays_byte_for_byte(self, tiny_acceptance):
        assert tiny_acceptance.tiny_tapeout_macro.replay_byte_equal is True

    def test_the_headline_readings_are_the_pinned_ones(self, tiny_acceptance):
        macro = tiny_acceptance.tiny_tapeout_macro
        assert macro.stop_reason == "no_further_improvement"
        assert macro.unmet_constraint_ids == ()
        assert macro.stranded == ()
        assert macro.objective_before_nm == 0
        assert macro.objective_after_nm > 0
        assert macro.wall_s > 0
        assert macro.candidates_per_s > 0

    def test_the_micro_row_carries_both_terms(self, tiny_acceptance):
        micro = tiny_acceptance.ecc83_micro
        assert micro.engine_per_candidate_s > 0
        assert micro.wrapper.candidates == 20
        assert 20 <= micro.wrapper.candidates <= 50
        assert micro.ratio > 0
        assert micro.candidates_evaluated > 0

    def test_the_adr0001_remeasures_are_recorded(self, tiny_acceptance):
        adr0001 = tiny_acceptance.adr0001_remeasures
        assert adr0001.board_kib_at_150 > 0
        assert adr0001.retained_snapshot_kib > 0
        assert adr0001.with_placement_us_at_150 > 0

    def test_the_improve_phase_readings_are_recorded(self, tiny_acceptance):
        """Report-only, never gated (ADR-0019): the mix and the typical
        move are recorded so the ADR-0016/0019 trigger can be read off
        each sign-off, not enforced by the tier."""
        macro = tiny_acceptance.tiny_tapeout_macro
        assert macro.typical_move_us > 0
        assert sum(bucket.movers for bucket in macro.move_mix) == 150
        assert sum(bucket.wall_s for bucket in macro.move_mix) > 0

    def test_the_provenance_block_names_its_versions(self, tiny_acceptance):
        provenance = tiny_acceptance.provenance
        assert "10." in provenance.kicad_cli
        assert len(provenance.jar_sha256) == 64
        assert provenance.python
