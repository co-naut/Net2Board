"""The tiny_tapeout slow-tier acceptance face (ADR-0019, #49).

The fast-tier section exercises the session module's measurement machinery
with fakes only — the seam-reported candidate count read against the
tests' instrumented ledger (#102, no clocks anywhere) and the
wrapper-baseline loop (injected evaluator and clock). The slow-tier
section at the bottom is the real acceptance run: the derived ratio, the
linearity receipt, the tiny replay, and the ADR-0001 re-measures — opt-in
with ``pytest -m slow``.
"""

import dataclasses
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import TINY_OUTLINE, counted_probes, fresh_board
from net2board.harness.perf import (
    AcceptanceReport,
    Adr0001Remeasures,
    Exports,
    MacroRow,
    MicroRow,
    MoveMixBucket,
    NamedMover,
    Provenance,
    WrapperBaseline,
    WrapperSample,
    bucket_refs,
    compare_perf_history,
    measure_wrapper_baseline,
    move_mix,
    named_movers,
    render,
    ring_variants,
    typical_move,
    write_perf_json,
)
from net2board.harness.session import TINY_ENTRY_CANDIDATES, TINY_HEADLINE_CANDIDATES
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


class TestCandidatesEvaluated:
    """The seam reports its own spend (#102): the tiny fixtures' counts —
    the pins' subject matter — read off ``SolveResult``, agreeing with the
    tests' instrumented ledger and honouring ``min(budget, N)`` exactly
    (#107 made the flip point exact, so the count is the untruncated truth,
    not a minimal-NFI reading that sits under it by the last round's quiet
    tail). The derivation and its ``expect`` fast path (#94) are gone: the
    harness reads the field; the wrapper here is the tests' trick that
    keeps it honest."""

    def test_the_count_is_the_min_of_budget_and_truth(self):
        board = placed_r1_board()
        n = counted_probes(board, 1_000_000)
        assert n > 0
        for budget in (0, 1, n // 2, n - 1, n, 2_000_000):
            result = solve(board, budget=budget)
            assert result.candidates_evaluated == min(budget, n)
            assert result.candidates_evaluated == counted_probes(board, budget)

    def test_a_ceiling_that_exhausts_reads_as_itself(self):
        board = placed_r1_board()
        assert solve(board, budget=1).candidates_evaluated == 1

    def test_the_count_replays(self):
        board = placed_r1_board()
        first = solve(board, budget=1_000_000)
        second = solve(board, budget=1_000_000)
        assert first == second

    def test_scoped_refs_are_honoured(self):
        """``refs`` scopes the spend as it scopes the search: the scoped
        solve evaluates strictly fewer Candidates than the whole-board
        one."""
        board = placed_r1_board()
        whole = solve(board, budget=1_000_000).candidates_evaluated
        scoped = solve(board, ("R2",), budget=1_000_000).candidates_evaluated
        assert 0 < scoped < whole

    def test_a_fixed_point_re_converges_at_its_own_count(self):
        """The searched board is the objective's fixed point but not a
        one-probe fixed point: a fresh solve walks one quiet round, the
        count is that walk — honestly, never zero — and the board comes
        back the very object (identity on no-op, ADR-0013)."""
        board = converged_board()
        result = solve(board, budget=1_000_000)
        assert result.board is board
        assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert result.candidates_evaluated == counted_probes(board, 1_000_000)
        assert result.candidates_evaluated > 0


class TestMoveMix:
    """The improve-round move mix (ADR-0019's report-only reading):
    bucketed by footprint pad count — R1/R2 carry RES's 3 pads (the extra
    pad counts: transforms walk the footprint), R3 BIGRES's 2, and the
    ``9+`` bucket is empty here. Walls, not counts: the buckets were
    pinned when a round's probe count was not derivable through the seam
    — a rationale #102 (seam-reported ``candidates_evaluated``) and #107
    (a truncated scan stops ``BUDGET_EXHAUSTED``, never a quiet
    ``NO_FURTHER_IMPROVEMENT``) have since retired — and the buckets stay
    fixed-cost-subtracted walls."""

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

    def test_buckets_carry_their_own_probe_counts(self):
        """Each bucket's probe count is its own scoped solve's spend
        (#112): the reading the per-probe trigger subject derives over —
        counted against the tests' ledger, never the harness's word. An
        empty bucket spends nothing."""
        board = converged_board()
        buckets = {
            bucket.label: bucket
            for bucket in move_mix(board, ceiling=100_000, clock=FakeClock())
        }
        assert buckets["1-2 pads"].candidates_evaluated == counted_probes(
            board, 100_000, refs=("R3",)
        )
        assert buckets["3-8 pads"].candidates_evaluated == counted_probes(
            board, 100_000, refs=("R1", "R2")
        )
        assert buckets["9+ pads"].candidates_evaluated == 0

    def test_named_refs_price_their_own_rows(self):
        """The named rows are a separate reading (#92): caller order,
        absent names skipped, each priced by its own scoped solve —
        never a bucket row with the ref packed into the label."""
        board = converged_board()
        named = named_movers(
            board, ("R2", "NOPE", "R1"), ceiling=100_000, clock=FakeClock()
        )
        assert [row.ref for row in named] == ["R2", "R1"]
        assert all(not row.locked for row in named)
        assert all(row.pads > 0 for row in named)
        assert all(row.wall_s >= 0.0 for row in named)
        for row in named:
            assert row.candidates_evaluated == counted_probes(
                board, 100_000, refs=(row.ref,)
            )

    def test_a_locked_ref_reports_locked_without_a_solve(self):
        """A locked name takes a zero wall, named so — and still reports
        its real pad count (the footprint's fact, not the placement's)."""
        board = converged_board().with_placement(
            "R2", 20_000_000, 5_000_000, 0, "F.Cu", locked=True
        )
        named = named_movers(board, ("R2",), ceiling=100_000, clock=FakeClock())
        assert [row.ref for row in named] == ["R2"]
        assert named[0].locked is True
        assert named[0].pads > 0
        assert named[0].wall_s == 0.0
        assert named[0].candidates_evaluated == 0

    def test_bucket_refs_is_the_selection_the_buckets_price(self):
        """The profile face prices one bucket through ``bucket_refs`` —
        the same selection ``move_mix`` prices, so a ``--scope`` reading
        pairs with the perf JSON's bucket rows."""
        board = converged_board()
        refs_by_label = bucket_refs(board)
        assert refs_by_label["1-2 pads"] == ("R3",)
        assert refs_by_label["3-8 pads"] == ("R1", "R2")
        assert refs_by_label["9+ pads"] == ()
        for bucket in move_mix(board, ceiling=100_000, clock=FakeClock()):
            assert len(refs_by_label[bucket.label]) == bucket.movers

    def test_bucket_refs_skips_locked_refs(self):
        """A locked ref is nobody's mover: out of its bucket's selection
        the same as out of the mix's walls."""
        board = converged_board().with_placement(
            "R2", 20_000_000, 5_000_000, 0, "F.Cu", locked=True
        )
        assert bucket_refs(board)["3-8 pads"] == ("R1",)


class TestTypicalMove:
    """The trigger subject (#112): the 1–2-pad bucket's per-probe cost —
    that bucket's scoped-solve wall over its own probe count, derived
    from the mix the harness already measures. The visit order cannot
    touch it: a bucket's reading is order-free by construction, where the
    retired truncated-half slope priced whichever movers the scan left
    for its trailing half."""

    def test_the_subject_is_the_passive_bucket_not_the_round(self):
        """The whole-round mean is not the subject: the big parts' cost
        stays documented beside, never folded in (ADR-0016's pin-count
        term documented, not flattened)."""
        mix = (
            MoveMixBucket(
                label="1-2 pads", movers=90, wall_s=1.0, candidates_evaluated=10_000
            ),
            MoveMixBucket(
                label="3-8 pads", movers=50, wall_s=9.0, candidates_evaluated=10_000
            ),
            MoveMixBucket(
                label="9+ pads", movers=9, wall_s=90.0, candidates_evaluated=10_000
            ),
        )
        scale, per_probe_us = typical_move(mix)
        assert scale == 10_000
        assert per_probe_us == 100.0

    def test_the_subject_reads_the_real_mix(self):
        """On a searched board the derivation is the 1–2-pad bucket's own
        numbers — the ledger counts its scoped solve, the wall is the
        bucket's."""
        board = converged_board()
        mix = move_mix(board, ceiling=100_000, clock=FakeClock())
        passive = next(b for b in mix if b.label == "1-2 pads")
        scale, per_probe_us = typical_move(mix)
        assert scale == passive.candidates_evaluated
        assert scale == counted_probes(board, 100_000, refs=("R3",))
        assert per_probe_us == passive.wall_s / passive.candidates_evaluated * 1_000_000

    def test_an_empty_passive_bucket_is_a_caller_error(self):
        """A board without 1–2-pad probes has no typical move to read —
        silence would mint a 0 µs/probe that reads like a triumph."""
        mix = (
            MoveMixBucket(
                label="1-2 pads", movers=0, wall_s=0.0, candidates_evaluated=0
            ),
        )
        with pytest.raises(ValueError, match="1-2"):
            typical_move(mix)


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

    def test_the_block_leads_with_the_wrapper_and_carries_the_ratio(self):
        report = synthetic_report()
        assert dataclasses.asdict(report)["tiny_tapeout_macro"]["ratio"] == 1_000.0

        out = render(report)
        assert out.index("first act") < out.index("per-session (macro)")
        assert "ratio: 1,000x (floor 1,000x)" in out
        assert "adr-0001 re-measures" in out
        assert "move mix (improve-round wall)" in out
        assert "1-2 pads 62%" in out
        assert "typical move 81.4 us/probe" in out

    def test_the_typical_move_names_its_subject(self):
        """The trigger subject is named (#112): the printed reading says
        which bucket's per-probe cost and over how many probes — a bare
        number invited the retired slope's 'what is this?' back."""
        out = render(synthetic_report())
        assert "typical move 81.4 us/probe (1-2 pads, 6,200 probes)" in out

    def test_buckets_render_us_per_probe_beside_share(self):
        """Every bucket shows µs/probe beside its wall share (#112) — the
        big parts' cost stays visible, the trigger subject's company."""
        out = render(synthetic_report())
        assert "1-2 pads 62% (100.0 us/probe)" in out
        assert "3-8 pads 31% (100.0 us/probe)" in out
        assert "9+ pads 7% (100.0 us/probe)" in out

    def test_named_rows_render_seconds_beside_the_bucket_shares(self):
        """The named rows price absolute seconds after the buckets — a
        single-ref solve is not a slice of the buckets' solve, so it
        never dilutes the shares (#92, #105): the big parts are
        documented, not flattened into them."""
        report = synthetic_report(
            named_movers=(
                NamedMover(
                    ref="J4",
                    pads=80,
                    locked=False,
                    wall_s=5.0,
                    candidates_evaluated=2_500,
                ),
                NamedMover(
                    ref="U6",
                    pads=56,
                    locked=True,
                    wall_s=0.0,
                    candidates_evaluated=0,
                ),
            )
        )
        out = render(report)
        assert "1-2 pads 62%" in out
        assert "3-8 pads 31%" in out
        assert "9+ pads 7%" in out
        assert "J4 (80 pads) 5.00 s (2,000.0 us/probe)" in out
        assert "U6 locked" in out


def synthetic_report(
    *,
    wall_s: float = 2.0,
    ratio: float = 1_000.0,
    typical_move_us: float = 81.4,
    move_mix_walls: tuple[float, float, float] = (0.62, 0.31, 0.07),
    named_movers: tuple[NamedMover, ...] = (),
    micro_wall_s: float = 1.0,
    micro_ratio: float = 100.0,
    cpu: str = "test",
    os: str = "test",
    python: str = "3.12",
) -> AcceptanceReport:
    """A fully-formed report at known numbers — the synthetic current run
    the printed-block and perf-history tests read. Bucket probes ride
    their wall at 10,000 probes per second, so every µs/probe reading is
    100.0 — a known literal, not a recomputation."""

    def wrapper_baseline() -> WrapperBaseline:
        return WrapperBaseline(
            candidates=30,
            per_candidate_s=1.0,
            slowest_over_fastest=1.2,
            samples=tuple(WrapperSample(index=i, seconds=1.0) for i in range(30)),
        )

    mix = tuple(
        MoveMixBucket(
            label=label,
            movers=movers,
            wall_s=wall,
            candidates_evaluated=int(wall * 10_000),
        )
        for label, movers, wall in (
            ("1-2 pads", 90, move_mix_walls[0]),
            ("3-8 pads", 50, move_mix_walls[1]),
            ("9+ pads", 9, move_mix_walls[2]),
        )
    )

    return AcceptanceReport(
        provenance=Provenance(
            python=python,
            kicad_cli="10.0.6",
            jar_sha256="0" * 64,
            os=os,
            cpu=cpu,
            fixtures=(("ecc83", "15"),),
            budgets=(("BUDGET_TINY", 500_000),),
        ),
        tiny_tapeout_macro=MacroRow(
            wall_s=wall_s,
            candidates_evaluated=2_000,
            candidates_per_s=2_000 / wall_s,
            wrapper=wrapper_baseline(),
            baseline_derived_s=2_000.0,
            baseline_arithmetic="1.000000 s/candidate x 2,000 candidates = 2,000.0 s",
            ratio=ratio,
            replay_byte_equal=True,
            objective_before_nm=0,
            objective_after_nm=5,
            stop_reason="no_further_improvement",
            unmet_constraint_ids=(),
            stranded=("J9",),
            move_mix=mix,
            named_movers=named_movers,
            typical_move_us=typical_move_us,
            typical_move_scale=6_200,
        ),
        ecc83_micro=MicroRow(
            wall_s=micro_wall_s,
            candidates_evaluated=100,
            candidates_per_step=(("improve", 100),),
            engine_per_candidate_s=micro_wall_s / 100,
            wrapper=wrapper_baseline(),
            ratio=micro_ratio,
        ),
        adr0001_remeasures=Adr0001Remeasures(
            board_kib_at_150=1.0,
            retained_snapshot_kib=0.2,
            with_placement_us_at_150=7.0,
        ),
        exports=Exports(tiny_tapeout="<tiny export>", ecc83="<ecc83 export>"),
    )


def write_previous(
    directory: Path,
    day: str,
    *,
    cpu: str = "test",
    os: str = "test",
    python: str = "3.12",
    wall_s: float = 2.0,
    ratio: float = 1_000.0,
    typical_move_us: float | None = 81.4,
    move_mix: list[dict] | None = None,
    named_movers: list[dict] | None = None,
    micro_wall_s: float = 1.0,
    micro_ratio: float = 100.0,
) -> Path:
    """A previous run's perf JSON, hand-built from literals — the
    independent reading the comparison is judged against (not produced
    through the writer under test). ``typical_move_us=None`` omits the
    field entirely: the pre-#112 shape the comparison must tolerate."""
    if move_mix is None:
        move_mix = [
            {
                "label": "1-2 pads",
                "movers": 90,
                "wall_s": 0.62,
                "candidates_evaluated": 6_200,
            },
            {
                "label": "3-8 pads",
                "movers": 50,
                "wall_s": 0.31,
                "candidates_evaluated": 3_100,
            },
            {
                "label": "9+ pads",
                "movers": 9,
                "wall_s": 0.07,
                "candidates_evaluated": 700,
            },
        ]
    macro: dict = {
        "wall_s": wall_s,
        "ratio": ratio,
        "move_mix": move_mix,
    }
    if typical_move_us is not None:
        macro["typical_move_us"] = typical_move_us
    if named_movers is not None:
        macro["named_movers"] = named_movers
    path = directory / f"{day}-perf.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "provenance": {"cpu": cpu, "os": os, "python": python},
                "tiny_tapeout_macro": macro,
                "ecc83_micro": {"wall_s": micro_wall_s, "ratio": micro_ratio},
            }
        )
    )
    return path


class TestPerfHistory:
    """The machine-readable perf history (#100, ADR-0026): the dated JSON
    beside the prose snapshot, and the same-machine ±20% comparison the
    sign-off reads. Synthetic reports throughout — the fast tier never
    measures."""

    def test_the_write_lands_beside_the_prose_snapshot(self, tmp_path):
        path = write_perf_json(synthetic_report(), tmp_path)
        assert path == tmp_path / (
            f"{datetime.now().astimezone().date().isoformat()}-perf.json"
        )
        payload = json.loads(path.read_text())
        assert payload["schema"] == 1
        macro = payload["tiny_tapeout_macro"]
        assert macro["wall_s"] == 2.0
        assert macro["ratio"] == 1_000.0
        assert macro["typical_move_us"] == 81.4
        assert macro["typical_move_scale"] == 6_200
        assert [bucket["label"] for bucket in macro["move_mix"]] == [
            "1-2 pads",
            "3-8 pads",
            "9+ pads",
        ]
        assert [bucket["candidates_evaluated"] for bucket in macro["move_mix"]] == [
            6_200,
            3_100,
            700,
        ]
        assert macro["named_movers"] == []
        assert payload["ecc83_micro"]["wall_s"] == 1.0
        assert payload["ecc83_micro"]["ratio"] == 100.0
        assert payload["provenance"]["cpu"] == "test"
        assert "exports" not in payload

    def test_without_history_the_comparison_says_so(self, tmp_path):
        """The first JSON seeds the history — the sign-off prints the
        skip, warns at nothing, fails at nothing (#100)."""
        lines = compare_perf_history(synthetic_report(), tmp_path)
        assert lines == ["no perf history to compare"]

    def test_the_write_and_the_comparison_read_one_clock(self, tmp_path):
        """The writer and the selector share the caller's date — the run's
        own JSON is never its own history, midnight straddle included
        (#100)."""
        today = date(2026, 9, 15)
        write_perf_json(synthetic_report(), tmp_path, today=today)
        lines = compare_perf_history(synthetic_report(), tmp_path, today=today)
        assert lines == ["no perf history to compare"]

    def test_a_changed_machine_is_incomparable(self, tmp_path):
        """CPU, OS, or Python changed — no deltas print, the skip does
        (ADR-0026's gate; #100)."""
        write_previous(tmp_path, "2026-09-01", cpu="other box")
        lines = compare_perf_history(
            synthetic_report(), tmp_path, today=date(2026, 9, 15)
        )
        assert len(lines) == 1
        assert "incomparable: machine changed" in lines[0]

    def test_the_comparison_names_the_latest_previous_run(self, tmp_path):
        """Two candidate histories — the newest strictly-before-today run
        is the one read, the header naming it (#100)."""
        write_previous(tmp_path, "2026-09-01")
        write_previous(tmp_path, "2026-09-08")
        lines = compare_perf_history(
            synthetic_report(), tmp_path, today=date(2026, 9, 15)
        )
        assert lines[0] == "perf history vs 2026-09-08:"
        assert all("2026-09-01" not in line for line in lines)
        # header + the ticket's eight readings: both walls, both ratios,
        # the typical move, and the three move-mix buckets
        assert len(lines) == 9

    def test_beyond_the_band_warns_both_ways(self, tmp_path):
        """+35.7% and -93.3% warn; exactly -20.0% does not — 'beyond' is
        exclusive (ADR-0026, #100)."""
        write_previous(
            tmp_path,
            "2026-09-08",
            wall_s=2.5,
            typical_move_us=60.0,
            micro_ratio=1_500.0,
        )
        lines = compare_perf_history(
            synthetic_report(), tmp_path, today=date(2026, 9, 15)
        )
        warned = [line for line in lines if "WARN" in line]
        assert warned == [
            "  typical move: 60.0 -> 81.4 us (+35.7%) WARN beyond ±20%",
            "  ecc83 ratio: 1,500 -> 100 (-93.3%) WARN beyond ±20%",
        ]
        assert "  tiny wall: 2.50 -> 2.00 s (-20.0%)" in lines

    def test_a_prior_without_the_new_fields_skips_and_names(self, tmp_path):
        """The named-movers precedent (#105): a previous run without the
        probe counts or the typical move is skipped and named, never
        crashed on (#112, no schema bump). The wall deltas still read."""
        write_previous(
            tmp_path,
            "2026-09-08",
            typical_move_us=None,
            move_mix=[{"label": "1-2 pads", "movers": 90, "wall_s": 0.62}],
            named_movers=[{"ref": "J4", "pads": 80, "locked": False, "wall_s": 4.0}],
        )
        report = synthetic_report(
            named_movers=(
                NamedMover(
                    ref="J4",
                    pads=80,
                    locked=False,
                    wall_s=5.0,
                    candidates_evaluated=2_500,
                ),
            )
        )
        lines = compare_perf_history(report, tmp_path, today=date(2026, 9, 15))
        assert "  typical move: absent in the previous run — skipped" in lines
        wall_line = next(line for line in lines if "move mix 1-2 pads" in line)
        assert wall_line == "  move mix 1-2 pads: 0.62 -> 0.62 s (+0.0%)"
        named_line = next(line for line in lines if "named mover J4" in line)
        assert named_line == (
            "  named mover J4: 4.00 -> 5.00 s (+25.0%) WARN beyond ±20%"
        )

    def test_per_probe_deltas_ride_their_reading_lines(self, tmp_path):
        """Both sides carrying probe counts, the µs/probe reading deltas
        ride the same line as the wall (#112) — the trigger subject and
        its company comparable at sign-off without a hand count pass."""
        write_previous(
            tmp_path,
            "2026-09-08",
            named_movers=[
                {
                    "ref": "J4",
                    "pads": 80,
                    "locked": False,
                    "wall_s": 4.0,
                    "candidates_evaluated": 2_000,
                }
            ],
        )
        report = synthetic_report(
            named_movers=(
                NamedMover(
                    ref="J4",
                    pads=80,
                    locked=False,
                    wall_s=5.0,
                    candidates_evaluated=2_500,
                ),
            )
        )
        lines = compare_perf_history(report, tmp_path, today=date(2026, 9, 15))
        assert (
            "  move mix 1-2 pads: 0.62 -> 0.62 s (+0.0%)"
            " · us/probe 100.0 -> 100.0 (+0.0%)"
        ) in lines
        assert (
            "  named mover J4: 4.00 -> 5.00 s (+25.0%) WARN beyond ±20%"
            " · us/probe 2,000.0 -> 2,000.0 (+0.0%)"
        ) in lines

    def test_move_mix_buckets_match_by_label(self, tmp_path):
        """A bucket the previous run lacks is skipped and named, never
        misread as a delta — and so is a bucket the current run lacks
        (matched by label, both directions named, #100)."""
        write_previous(
            tmp_path,
            "2026-09-08",
            move_mix=[
                {"label": "1-2 pads", "movers": 90, "wall_s": 0.5},
                {"label": "hand-tuned", "movers": 12, "wall_s": 0.1},
            ],
        )
        lines = compare_perf_history(
            synthetic_report(), tmp_path, today=date(2026, 9, 15)
        )
        assert "  move mix 1-2 pads: 0.50 -> 0.62 s (+24.0%) WARN beyond ±20%" in lines
        assert not any("3-8 pads" in line and "move mix" in line for line in lines[:-2])
        assert "  move mix: 2 bucket(s) absent in the previous run — skipped" in lines
        assert "  move mix: 1 bucket(s) gone from the current run — skipped" in lines

    def test_named_rows_match_by_ref(self, tmp_path):
        """A named row the previous run lacks is skipped and named, never
        misread as a delta — and so is a row the current run lacks
        (matched by ref, both directions named, #105)."""
        write_previous(
            tmp_path,
            "2026-09-08",
            named_movers=[
                {"ref": "J4", "pads": 80, "locked": False, "wall_s": 4.0},
                {"ref": "HAND", "pads": 12, "locked": False, "wall_s": 0.5},
            ],
        )
        report = synthetic_report(
            named_movers=(
                NamedMover(
                    ref="J4", pads=80, locked=False, wall_s=5.0, candidates_evaluated=0
                ),
                NamedMover(
                    ref="U6", pads=56, locked=False, wall_s=1.2, candidates_evaluated=0
                ),
            )
        )
        lines = compare_perf_history(report, tmp_path, today=date(2026, 9, 15))
        assert "  named mover J4: 4.00 -> 5.00 s (+25.0%) WARN beyond ±20%" in lines
        assert "  named movers: 1 absent in the previous run — skipped" in lines
        assert "  named movers: 1 gone from the current run — skipped" in lines


@pytest.mark.slow
class TestTinyAcceptance:
    """The real slow-tier acceptance face (ADR-0019, #49): the derived
    ratio with its floor, the linearity receipt, the replay, and the
    headline's pinned readings. The wrapper leg already ran kicad-cli for
    real inside the session-scoped fixture — these assertions read the
    report it produced. The oracle gate rides ``tiny_acceptance`` (the
    conftest ``kicad_cli`` fixture, #104)."""

    def test_the_ratio_clears_the_registered_floor(self, tiny_acceptance):
        macro = tiny_acceptance.tiny_tapeout_macro
        assert macro.ratio >= 1_000, macro.baseline_arithmetic

    def test_the_baseline_is_derived_not_hardcoded(self, tiny_acceptance):
        from net2board.harness.session import BUDGET_TINY, WRAPPER_CANDIDATES_TINY

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

    def test_the_counts_hold_their_pins(self, tiny_acceptance):
        """The pins are drift goldens (#102): the report's counts are read
        off the session's own results — no derivation left to fall back —
        so a drifted pin fails this assertion naming the fresh number. A
        re-pin, like a golden."""
        macro = tiny_acceptance.tiny_tapeout_macro
        assert macro.candidates_evaluated == TINY_ENTRY_CANDIDATES, (
            "the entry board's count drifted from"
            f" TINY_ENTRY_CANDIDATES — re-pin it to {macro.candidates_evaluated:,}"
        )
        assert (
            sum(bucket.candidates_evaluated for bucket in macro.move_mix)
            == TINY_HEADLINE_CANDIDATES
        ), (
            "the searched board's bucket probe counts drifted from"
            f" TINY_HEADLINE_CANDIDATES — re-pin it to"
            f" {sum(bucket.candidates_evaluated for bucket in macro.move_mix):,}"
        )

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
        """Report-only, never gated (ADR-0019): the mix, the typical move
        (#112's subject — the 1–2-pad bucket's µs/probe), and the named
        rows are recorded so the ADR-0016/0019 trigger can be read off
        each sign-off, not enforced by the tier. The buckets' movers sum
        to the board's 150; the named rows price beside them (#105) —
        J4 and U6 are 9+-pad parts already inside the ``9+ pads``
        bucket, so counting them again reads 152. The typical move is
        the passive bucket's own reading, and every priced reading
        carries the probe count it derives over."""
        macro = tiny_acceptance.tiny_tapeout_macro
        passive = next(
            bucket for bucket in macro.move_mix if bucket.label == "1-2 pads"
        )
        assert macro.typical_move_us == pytest.approx(
            passive.wall_s / passive.candidates_evaluated * 1_000_000
        )
        assert macro.typical_move_scale == passive.candidates_evaluated
        assert sum(bucket.movers for bucket in macro.move_mix) == 150
        assert [row.ref for row in macro.named_movers] == ["J4", "U6"]
        assert all(not row.locked and row.wall_s > 0 for row in macro.named_movers)
        assert all(row.candidates_evaluated > 0 for row in macro.named_movers)
        assert all(
            bucket.candidates_evaluated > 0
            for bucket in macro.move_mix
            if bucket.movers > 0
        )
        assert sum(bucket.wall_s for bucket in macro.move_mix) > 0

    def test_the_provenance_block_names_its_versions(self, tiny_acceptance):
        provenance = tiny_acceptance.provenance
        assert "10." in provenance.kicad_cli
        assert len(provenance.jar_sha256) == 64
        assert provenance.python
