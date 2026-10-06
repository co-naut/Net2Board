"""The M2 session — one Board lineage from intent to searched geometry (ADR-0019).

Three faces in one module, so there is no second harness to drift: the
fast-tier acceptance test imports ``run_session()`` (the replay double-run
and the byte goldens), ``uv run python -m net2board.examples.session
[output.kicad_pcb]`` prints the ecc83 measurements and writes the searched
export, and ``--slow`` runs the tiny_tapeout acceptance face — the wrapper
baseline (the harness's first act, ADR-0019), the full-budget headline
with the derived ratio, the improve-round move mix and typical-move
reading (ADR-0019's report-only mix; ADR-0016/0019's >100 µs trigger),
the ecc83 micro row, and the ADR-0001 re-measures — printing the
provenance block. Exit code 0 means every beat held.

The ecc83 lineage starts from the committed acceptance table
(``apply_placements`` — M1's output is M2's input; nothing here re-authors
a placement) and walks six pinned-budget solve steps: the under-budgeted
attempt and its full-budget retry (the ``BUDGET_EXHAUSTED``
demonstration), the improvement over the hand-tuned board, the
Criticality re-ranking, the relation statements, and a deliberate
unsatisfiable Region — P7 gathered into the bottom band while a
Requirement EdgeMount holds it to the top edge — reported by
``unmet_constraint_ids`` and one ``run_drc`` finding, then relaxed away.
Every step asserts its reading: legalize delta exactly 0, the pinned
strand/stop/unmet reading, containment clean or sanctioned.

The tiny_tapeout face (slow tier — wall clock only ever here) is the
headline: the wrapper baseline measured on the same fixture, one
full-budget solve from the all-unplaced board, and the self-normalizing
ratio of the derived wrapper baseline to the measured wall. The first
real run under the lexicographic visit order stranded J9 — the 62 mm
pin-header strip attempted after the other 149 parts had converged —
resolved by the board-derived reach-descending visit order (#52): the
biggest parts attempt first, and the strip lands with the field.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_pcb
from net2board.geometry import Point, Rectangle
from net2board.model import (
    Board,
    Constraint,
    Criticality,
    Group,
    KeepTogether,
    Placement,
    Preference,
    Proximity,
    Region,
    Requirement,
)
from net2board.objective import Objective, evaluate
from net2board.solver import StopReason, legalize, solve

REPO_ROOT = Path(__file__).resolve().parents[3]
ECC83_DIR = REPO_ROOT / "kicad_demo" / "ecc83"
NETLIST_PATH = ECC83_DIR / "ecc83-pp.net"
FOOTPRINT_DIR = ECC83_DIR / "footprints.pretty"
DEFAULT_OUTPUT = ECC83_DIR / "net2board-ecc83-session.kicad_pcb"
OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
STACKUP = ("F.Cu", "B.Cu")

# The budget sizing rule (ADR-0019): these pins keep the session's added
# wall time ≈≤2 s on a developer machine — measured ≈0.5 s for all six
# solves — a sizing rule, never a timing assertion (nothing here reads a
# clock, and no test may). BUDGET_UNDERCUT is sized to cut the improve
# step strictly mid-work: the step still exhausts at 3,200 candidates and
# converges by 5,000, and at 1,600 its objective sits strictly between the
# baseline and the optimum. BUDGET_MAIN buys headroom past every
# convergence point; those steps stop on NO_FURTHER_IMPROVEMENT, never on
# the budget.
BUDGET_UNDERCUT = 1_600
BUDGET_MAIN = 20_000

# The tiny_tapeout slow-tier face. BUDGET_TINY follows BUDGET_MAIN's rule
# at macro scale: a round pinned value with generous headroom past the
# measured convergence point — the #63 run (live placements map) converged
# at 1,414,784 candidates (NO_FURTHER_IMPROVEMENT, ~1.77x headroom here),
# never touching the budget; a BUDGET_EXHAUSTED headline means the pin
# is wrong. Re-pinned for #63: with the candidate families working, the
# search probes ~5x more candidates than the stale-map search's 284,866 —
# the 500,000 pin sized to that crippled search exhausts (the harness's
# own guard). The wrapper loops are sized inside ADR-0019's
# 20-50-candidate linearity-receipt band — a receipt, never a timing
# assertion.
BUDGET_TINY = 2_500_000
WRAPPER_CANDIDATES_TINY = 30
WRAPPER_CANDIDATES_ECC83 = 20

TINY_DIR = REPO_ROOT / "kicad_demo" / "tiny_tapeout"
TINY_NETLIST_PATH = TINY_DIR / "tinytapeout.net"
TINY_FOOTPRINT_DIR = TINY_DIR / "footprints.pretty"
DEFAULT_TINY_OUTPUT = TINY_DIR / "net2board-tiny-session.kicad_pcb"
TINY_OUTLINE = Rectangle(min=Point(0, 0), max=Point(104_500_000, 81_000_000))
TINY_STACKUP = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

# The strand reading the headline pins: nothing — the visit order is
# board-derived reach descending (#52), so the 62 mm PinHeader_1x24 strip
# attempts before the other 149 parts converge around it and lands with
# the field. A non-empty reading here means the search stranded a part:
# the engine reports it as the value it is — board.unplaced_refs().
TINY_STRANDED = ()

FREEROUTING_JAR = Path(
    os.environ.get("NET2BOARD_FREEROUTING_JAR", "~/tools/freerouting-2.4.1.jar")
).expanduser()
"""The pinned router jar, for the provenance block's sha256 line — the
pin itself lives in tests/test_dsn_freerouting.py."""


def candidate_count(
    board: Board, refs: tuple[str, ...] | None = None, *, ceiling: int
) -> int:
    """The search's minimal no-further-improvement budget — the harness's
    seam-derived candidate denominator (ADR-0019: "derived, not
    instrumented"; ``SolveResult`` stays frozen, ADR-0013).

    ADR-0013's budget cuts the search short without changing its path, so
    the probes spent under budget ``b`` are exactly ``min(b, N)`` where
    ``N`` is the untruncated count. The minimal budget whose ``stop_reason``
    reads ``NO_FURTHER_IMPROVEMENT`` is therefore **at or under** ``N`` —
    a truncated final-round scan that finds no improvement still stops
    ``NO_FURTHER_IMPROVEMENT``, so the derivation reads low by whatever
    quiet tail the last round's scope had. The gap is bounded by that tail
    and immaterial to the wall-time arithmetic (every counted probe is a
    real probe), but the derivation is a minimal-NFI budget, not the exact
    untruncated count — claim it as such. Found by an exponential gallop
    then bisection, each probe solve costing ``min(budget, N)`` of the real
    work. Deterministic (the search is), so the derivation replays. The
    scope must evaluate at least one candidate; an empty scope is a
    degenerate reading, not a harness case.
    """
    if solve(board, refs, budget=ceiling).stop_reason is StopReason.BUDGET_EXHAUSTED:
        return ceiling
    low, high = 0, ceiling
    step = 1
    while step < high:
        if solve(board, refs, budget=step).stop_reason is (
            StopReason.NO_FURTHER_IMPROVEMENT
        ):
            high = step
            break
        low = step
        step *= 2
    while high - low > 1:
        mid = (low + high) // 2
        if solve(board, refs, budget=mid).stop_reason is (
            StopReason.NO_FURTHER_IMPROVEMENT
        ):
            high = mid
        else:
            low = mid
    return high


_MOVE_MIX_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1-2 pads", 0, 2),
    ("3-8 pads", 3, 8),
    ("9+ pads", 9, None),
)
"""The move-mix buckets (ADR-0019's report-only reading): movers grouped
by footprint pad count — the count the probe's pad-centre transforms scale
with (ADR-0016's shape: linear in the moved part's pads and degree)."""


@dataclass(frozen=True)
class MoveMixBucket:
    """One move-mix bucket: how many of the default scope's movers it holds
    and the improve-round wall their probes cost — the same solve at budget
    zero (the per-call fixed cost: Evaluator build, finalize) subtracted, so
    what remains is probe work. Timing, not counting: a round's true probe
    count is not derivable through the seam (a truncated final-round scan
    that finds no improvement still stops ``NO_FURTHER_IMPROVEMENT``, so a
    minimal-NFI-budget derivation reads low), and time share is the reading
    the mix exists for."""

    label: str
    movers: int
    wall_s: float


def move_mix(
    board: Board, *, ceiling: int, clock: Callable[[], float]
) -> tuple[MoveMixBucket, ...]:
    """The improve-round move mix (ADR-0019's pre-registered reading,
    report-only — never a threshold): where the searched board's scoped
    probe work goes, by mover pad count.

    The scope is ``solve``'s default (every unlocked placement plus every
    unplaced ref); movers are bucketed by footprint pad count, and each
    bucket's wall is a full-budget scoped solve on the searched board minus
    that solve's zero-budget fixed cost (Evaluator build, finalize) — the
    probe work those movers' searches cost. On a board that is the search's
    fixed point a scoped solve is exactly one improve round; while it is
    not (#63), the wall is the scoped search's real work all the same —
    which is the reading the mix exists for. Probe counts are deliberately
    absent: the seam cannot report them exactly (``candidate_count``), and
    the share of the wall is the honest reading anyway."""
    locked = {placement.ref for placement in board.placements if placement.locked}
    pads_of = {
        component.ref: len(component.footprint.pads)
        for component in board.components
        if component.ref not in locked
    }
    buckets: list[MoveMixBucket] = []
    for label, low, high in _MOVE_MIX_BUCKETS:
        refs = tuple(
            sorted(
                ref
                for ref, pads in pads_of.items()
                if pads >= low and (high is None or pads <= high)
            )
        )
        wall_s = 0.0
        if refs:
            start = clock()
            solve(board, refs, budget=ceiling)
            full_s = clock() - start
            start = clock()
            solve(board, refs, budget=0)
            fixed_s = clock() - start
            wall_s = max(full_s - fixed_s, 0.0)
        buckets.append(MoveMixBucket(label=label, movers=len(refs), wall_s=wall_s))
    return tuple(buckets)


def typical_move_us(
    board: Board, *, ceiling: int, clock: Callable[[], float]
) -> tuple[int, float]:
    """The improve phase's per-probe cost — the reading ADR-0016/0019
    pre-registered their spatial-index trigger on ("a typical move over
    100 µs on the hard fixture"), measured through the public seam.

    ``candidate_count`` derives the probe scale — the search's minimal
    no-further-improvement budget, or ``ceiling`` when the scope does not
    converge under it (either bounds a real prefix of the search). Two
    budgeted solves — the full scale and its truncated half — are timed
    around the injected ``clock``; a budget cuts the search short without
    changing its path (ADR-0013), so the difference of the two walls
    isolates exactly the trailing half's probes: the slope is the
    per-probe cost and the shared fixed costs (Evaluator build, finalize)
    cancel. Returns the scale and the per-probe microseconds."""
    scale = candidate_count(board, ceiling=ceiling)
    start = clock()
    solve(board, budget=scale)
    full_s = clock() - start
    start = clock()
    solve(board, budget=scale // 2)
    half_s = clock() - start
    per_probe_s = (full_s - half_s) / (scale - scale // 2)
    return scale, per_probe_s * 1_000_000


@dataclass(frozen=True)
class WrapperSample:
    """One wrapper candidate's wall time — a linearity-receipt reading
    (ADR-0019): roughly constant per-candidate cost is what licenses the
    derived baseline's multiplication."""

    index: int
    seconds: float


@dataclass(frozen=True)
class WrapperBaseline:
    """The measured wrapper cost on one fixture: the per-candidate mean,
    the spread receipt, and every sample behind them. Absolutes only —
    the ratio is computed where both of its terms were measured."""

    candidates: int
    per_candidate_s: float
    slowest_over_fastest: float
    samples: tuple[WrapperSample, ...]


def ring_variants(base: Board, ref: str, *, count: int) -> tuple[Board, ...]:
    """The wrapper loop's candidate family: ``count`` deterministic
    one-part moves of ``ref`` alternating outwards in 0.5 mm steps from
    its current position — never the position itself, the solver's local
    ring rule — clamped inside the outline. What a placement-wrapper
    search probes: a full board export each time. Distinct by
    construction (the row's span must fit the outline; the harness calls
    it with 20–50). The base Board is never touched."""
    placement = next(p for p in base.placements if p.ref == ref)
    span = 500_000
    margin = 1_000_000
    back = (count // 2) * span
    forth = ((count + 1) // 2) * span
    start = min(
        max(placement.pos.x, base.outline.min.x + margin + back),
        base.outline.max.x - margin - forth,
    )
    return tuple(
        base.with_placement(
            ref,
            start + ((index // 2) + 1) * span * (1 if index % 2 == 0 else -1),
            placement.pos.y,
            placement.rotation,
            placement.side,
            placement.locked,
        )
        for index in range(count)
    )


def measure_wrapper_baseline(
    variants: Iterable[Board],
    evaluate: Callable[[Board], object],
    *,
    clock: Callable[[], float],
) -> WrapperBaseline:
    """Time a real wrapper loop, candidate by candidate: ``evaluate`` runs
    once per variant — the full export-oracle-parse work a wrapper-driven
    search pays per candidate — with the injected ``clock`` read around
    it. The mean is the per-candidate cost; the slowest-over-fastest
    sample ratio is the linearity receipt. Wall clock enters only through
    the injected callable: nothing here reads one itself."""
    boards = tuple(variants)
    if not boards:
        raise ValueError(
            "the wrapper loop needs candidates (ADR-0019's receipt runs 20-50); got 0"
        )
    samples = []
    for index, board in enumerate(boards):
        start = clock()
        evaluate(board)
        samples.append(WrapperSample(index=index, seconds=clock() - start))
    total = sum(sample.seconds for sample in samples)
    fastest = min(sample.seconds for sample in samples)
    slowest = max(sample.seconds for sample in samples)
    return WrapperBaseline(
        candidates=len(samples),
        per_candidate_s=total / len(samples),
        slowest_over_fastest=slowest / fastest if fastest > 0 else math.inf,
        samples=tuple(samples),
    )


# The stated beats. Bounds carry their measured provenance: C2 pad 2 sits
# 15.87 mm from U1 pad 8, same side (bound 16 mm); C2's courtyard spans
# (15.2, 1.0)..(22.2, 4.0) mm (the region gathers exactly that);
# R1+R2's union is 19.8 × 3.0 mm (kept within 20 × 8). ``REGION_BAD`` is
# the deliberate contradiction: its band excludes the top-edge mount band
# P7's Requirement EdgeMount sanctions, so no position can satisfy both.
CRIT_GND = Constraint(
    id="crit-gnd", kind=Preference(), relation=Criticality(net="GND"), weight=0
)
CRIT_HOT = Constraint(
    id="crit-hot",
    kind=Preference(),
    relation=Criticality(net="Net-(U1A-G)"),
    weight=4,
)
KT_BEAT = Constraint(
    id="kt-beat",
    kind=Requirement(),
    relation=KeepTogether(
        target="group:g-beat", max_width_nm=20_000_000, max_height_nm=8_000_000
    ),
)
PROX_BEAT = Constraint(
    id="prox-beat",
    kind=Requirement(),
    relation=Proximity(
        ref_a="C2",
        pad_a="2",
        ref_b="U1",
        pad_b="8",
        bound_nm=16_000_000,
        same_side=True,
    ),
)
REGION_BEAT = Constraint(
    id="region-beat",
    kind=Requirement(),
    relation=Region(
        target="placement:C2",
        rect=Rectangle(Point(15_000_000, 0), Point(23_000_000, 5_000_000)),
    ),
)
REGION_BAD = Constraint(
    id="region-bad",
    kind=Requirement(),
    relation=Region(
        target="placement:P7",
        rect=Rectangle(Point(0, 20_000_000), Point(40_000_000, 30_000_000)),
    ),
)


@dataclass(frozen=True)
class SessionStep:
    """One solve step's replay record — the pinned budget, the scope, and
    the readings the acceptance face asserts."""

    name: str
    budget: int
    refs: tuple[str, ...] | None
    stop_reason: StopReason
    unmet_constraint_ids: tuple[str, ...]
    legalize_delta_nm: int
    objective: Objective
    board: Board


@dataclass(frozen=True)
class SessionRecord:
    """The session's output: the hand-tuned baseline, every step in order,
    and the final export bytes — everything replay compares."""

    baseline_nm: int
    steps: tuple[SessionStep, ...]
    export: str


def _step(
    name: str,
    board: Board,
    *,
    budget: int,
    refs: tuple[str, ...] | None = None,
    expect_stop: StopReason = StopReason.NO_FURTHER_IMPROVEMENT,
    expect_unmet: tuple[str, ...] = (),
    expect_unplaced: tuple[str, ...] = (),
) -> SessionStep:
    """One pinned-budget solve and its by-construction checks: legalize
    delta exactly 0 (ADR-0013 — each step ends Requirement-legal, and the
    one deliberate contradiction in ``unsat`` names a finding no move can
    fix, so the unbudgeted finalize phase is identity either way), the
    pinned strand reading, containment clean or sanctioned, and the
    pre-registered reading of the step."""
    result = solve(board, refs, budget=budget)
    assert result.stop_reason is expect_stop, (
        f"{name}: stopped {result.stop_reason.name}, expected {expect_stop.name}"
    )
    assert result.unmet_constraint_ids == expect_unmet
    assert result.board.unplaced_refs() == expect_unplaced
    objective = evaluate(result.board)
    relegalized = legalize(result.board)
    # ADR-0013's reading, measured: the step's board is already the
    # legalizer's fixed point, so re-deriving repair moves nothing and the
    # legalize-induced objective delta is exactly 0.
    legalize_delta_nm = evaluate(relegalized.board).total_nm - objective.total_nm
    assert legalize_delta_nm == 0
    assert relegalized.board is result.board
    for finding in run_drc(result.board):
        assert finding.type is not ViolationType.COURTYARD_OUTSIDE_OUTLINE
        assert finding.type is not ViolationType.EDGE_MOUNT_UNMET
    return SessionStep(
        name=name,
        budget=budget,
        refs=refs,
        stop_reason=result.stop_reason,
        unmet_constraint_ids=result.unmet_constraint_ids,
        legalize_delta_nm=legalize_delta_nm,
        objective=objective,
        board=result.board,
    )


def run_session() -> SessionRecord:
    """The whole session over one Board lineage — deterministic, so a
    second call replays it byte for byte."""
    spec = BoardSpec.from_files(NETLIST_PATH, FOOTPRINT_DIR, OUTLINE, STACKUP)
    baseline = apply_placements(build_board(spec))
    baseline_nm = evaluate(baseline).total_nm

    steps: list[SessionStep] = []
    board = baseline
    undercut = _step(
        "undercut",
        board,
        budget=BUDGET_UNDERCUT,
        expect_stop=StopReason.BUDGET_EXHAUSTED,
    )
    board = undercut.board
    steps.append(undercut)

    improved = _step("improve", board, budget=BUDGET_MAIN)
    board = improved.board
    steps.append(improved)

    for constraint in (CRIT_GND, CRIT_HOT):
        board = board.with_constraint(constraint)
    criticality = _step("criticality", board, budget=BUDGET_MAIN)
    board = criticality.board
    steps.append(criticality)

    board = board.with_group(Group(id="g-beat", refs=("R1", "R2")))
    for constraint in (KT_BEAT, PROX_BEAT, REGION_BEAT):
        board = board.with_constraint(constraint)
    relations = _step("relations", board, budget=BUDGET_MAIN)
    board = relations.board
    steps.append(relations)

    unsat = _step(
        "unsat",
        board.with_constraint(REGION_BAD),
        budget=BUDGET_MAIN,
        refs=("P7",),
        expect_unmet=("region-bad",),
    )
    board = unsat.board.without_constraint("region-bad")
    steps.append(unsat)

    relaxed = _step("relaxed", board, budget=BUDGET_MAIN, refs=("P7",))
    board = relaxed.board
    steps.append(relaxed)

    return SessionRecord(
        baseline_nm=baseline_nm,
        steps=tuple(steps),
        export=export_pcb(board),
    )


@dataclass(frozen=True)
class TinySessionRecord:
    """The tiny_tapeout headline's replay record (ADR-0019): the entry
    objective (all-unplaced — PARTIAL terms, 0 nm), the one full-budget
    step, and the searched export bytes. Deterministic end to end, so a
    second call replays it byte for byte; the wall times and the derived
    candidate count live in the measurement face, never here."""

    entry_objective: Objective
    headline: SessionStep
    export: str


def _tiny_entry() -> Board:
    """The all-unplaced tiny_tapeout board — the headline's entry."""
    spec = BoardSpec.from_files(
        TINY_NETLIST_PATH, TINY_FOOTPRINT_DIR, TINY_OUTLINE, TINY_STACKUP
    )
    return build_board(spec)


def run_tiny_session() -> TinySessionRecord:
    """The tiny_tapeout headline (ADR-0019's slow-tier acceptance): one
    full-budget solve from the all-unplaced board — 150 components, 114
    nets, no authored intent, wirelength and containment only. The strand
    reading is pinned (``TINY_STRANDED``); legalize delta exactly 0,
    containment clean, and the unmet set empty are asserted by
    construction in ``_step``. The wrapper baseline is measured against
    this fixture's boards in ``measure_acceptance`` — never back-filled
    from ecc83 (ADR-0019)."""
    entry = _tiny_entry()
    headline = _step(
        "headline",
        entry,
        budget=BUDGET_TINY,
        expect_unplaced=TINY_STRANDED,
    )
    return TinySessionRecord(
        entry_objective=evaluate(entry),
        headline=headline,
        export=export_pcb(headline.board),
    )


def session_candidate_counts(record: SessionRecord) -> tuple[int, ...]:
    """Each ecc83 step's exact candidate count, derived from the record
    the same way the headline's is: every step carries its board, scope,
    and pinned budget, so ``candidate_count`` re-derives what the step
    spent without re-walking the lineage by hand."""
    return tuple(
        candidate_count(step.board, step.refs, ceiling=step.budget)
        for step in record.steps
    )


_DRC_COMMAND = ("kicad-cli", "pcb", "drc", "--format", "json", "--severity-all")


def kicad_drc_candidate(board: Board, workdir: Path) -> int:
    """One wrapper candidate, evaluated the wrapper way: export the whole
    board, one real oracle run, parse the violation count. This is the
    per-candidate work the derived baseline prices in — an engine that
    shelled to the oracle for every candidate would pay exactly this,
    times every candidate. A board the oracle cannot parse is a raised
    error, never a cost-free reading."""
    board_path = workdir / "candidate.kicad_pcb"
    board_path.write_text(export_pcb(board))
    report_path = workdir / "candidate.drc.json"
    result = subprocess.run(
        (*_DRC_COMMAND, "-o", str(report_path), str(board_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kicad-cli exited {result.returncode} on a wrapper candidate\n"
            f"stdout:\n{result.stdout}stderr:\n{result.stderr}"
        )
    return len(json.loads(report_path.read_text())["violations"])


def _oracle_version() -> str:
    result = subprocess.run(
        ("kicad-cli", "version"), capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "absent"


def _jar_sha256() -> str:
    if not FREEROUTING_JAR.exists():
        return "absent"
    return hashlib.sha256(FREEROUTING_JAR.read_bytes()).hexdigest()


def _deep_size(*roots: object) -> int:
    """Deep size over unique reachable objects (ADR-0001's method): one
    memo across all roots, so payload sharing between boards counts once
    and the marginal retained snapshot is a difference of two walks."""
    seen: set[int] = set()
    total = 0
    stack: list[object] = list(roots)
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        total += sys.getsizeof(obj)
        if isinstance(obj, dict):
            stack.extend(obj.items())
        elif isinstance(obj, (tuple, list, frozenset)):
            stack.extend(obj)
        elif hasattr(obj, "__dict__"):
            stack.append(obj.__dict__)
        elif hasattr(obj, "__slots__"):
            stack.extend(
                getattr(obj, slot) for slot in obj.__slots__ if hasattr(obj, slot)
            )
    return total


def _measure_derive_rate(board: Board, *, clock: Callable[[], float]) -> float:
    """ADR-0001's derive rate on this board: mean ``with_placement`` wall
    in microseconds over 100 canonical upserts — one call per orchestrator
    step, measured here because wall clock lives only in the slow face."""
    placement = board.placements[0]
    start = clock()
    for index in range(100):
        board.with_placement(
            placement.ref, placement.pos.x + index, placement.pos.y, 0, "F.Cu"
        )
    return (clock() - start) / 100 * 1_000_000


@dataclass(frozen=True)
class Provenance:
    """The provenance block's versions and machine (ADR-0019): everything
    a stranger needs to reproduce the numbers."""

    python: str
    kicad_cli: str
    jar_sha256: str
    os: str
    cpu: str
    fixtures: tuple[tuple[str, str], ...]
    budgets: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class MacroRow:
    """The tiny_tapeout per-session row: the measured wall (the
    acceptance path — solve, legalize, the per-step ``run_drc`` reads),
    the derived baseline, and their ratio with the arithmetic that
    produced it. ``candidates_per_s`` is the acceptance path's rate, not
    a bare probe rate — the probe-level reading is
    ``typical_move_us`` (the ADR-0016/0019 trigger subject) beside the
    round's move mix."""

    wall_s: float
    candidates_evaluated: int
    candidates_per_s: float
    wrapper: WrapperBaseline
    baseline_derived_s: float
    baseline_arithmetic: str
    ratio: float
    replay_byte_equal: bool
    objective_before_nm: int
    objective_after_nm: int
    stop_reason: str
    unmet_constraint_ids: tuple[str, ...]
    stranded: tuple[str, ...]
    move_mix: tuple[MoveMixBucket, ...]
    typical_move_us: float


@dataclass(frozen=True)
class MicroRow:
    """The ecc83 per-candidate row: both terms measured in this run — the
    session's own wall over its total candidates against the wrapper loop
    on the searched ecc83 board."""

    wall_s: float
    candidates_evaluated: int
    candidates_per_step: tuple[tuple[str, int], ...]
    engine_per_candidate_s: float
    wrapper: WrapperBaseline
    ratio: float


@dataclass(frozen=True)
class Adr0001Remeasures:
    """ADR-0001's snapshot and derive rates, re-measured on the searched
    tiny_tapeout board (the entry board has no placements to copy)."""

    board_kib_at_150: float
    retained_snapshot_kib: float
    with_placement_us_at_150: float


@dataclass(frozen=True)
class Exports:
    """The two searched exports this run produced."""

    tiny_tapeout: str
    ecc83: str


@dataclass(frozen=True)
class AcceptanceReport:
    """The slow-tier face's output: provenance, the two scope-labelled
    rows, ADR-0001's re-measures, and the exports. Frozen, so a slow-tier
    test reads it directly and the results snapshot renders it with
    ``dataclasses.asdict``."""

    provenance: Provenance
    tiny_tapeout_macro: MacroRow
    ecc83_micro: MicroRow
    adr0001_remeasures: Adr0001Remeasures
    exports: Exports


def _central_placement(board: Board, centre: Point) -> Placement:
    """The placement nearest ``centre`` — the wrapper loop's mover,
    chosen deterministically (ties break on the ref)."""
    return min(
        board.placements,
        key=lambda p: (
            abs(p.pos.x - centre.x) + abs(p.pos.y - centre.y),
            p.ref,
        ),
    )


def measure_acceptance(
    *, clock: Callable[[], float] = perf_counter
) -> AcceptanceReport:
    """The slow-tier measurement face. The wrapper baseline is the first
    *measurement* act (ADR-0019 — a named step, never back-filled from
    ecc83), and it prices the searched board: a wrapper candidate is a
    fully placed export, so the headline solve has to run before the
    wrapper loop can. The printed output names it first; the ratio and
    the ecc83 micro row follow; ADR-0001's re-measures close. Every
    reading enters the report with the numbers that produced it; the
    ratio is asserted by the slow-tier test, reported here."""
    provenance = Provenance(
        python=sys.version.split()[0],
        kicad_cli=_oracle_version(),
        jar_sha256=_jar_sha256(),
        os=platform.platform(),
        cpu=platform.processor() or platform.machine(),
        fixtures=(
            ("ecc83", "15 comps / 9 nets / 40x30 mm"),
            ("tiny_tapeout", "150 comps / 114 nets / 104.5x81 mm / 4 Cu"),
        ),
        budgets=(
            ("BUDGET_UNDERCUT", BUDGET_UNDERCUT),
            ("BUDGET_MAIN", BUDGET_MAIN),
            ("BUDGET_TINY", BUDGET_TINY),
        ),
    )

    # The headline, measured: one session wall (the acceptance path —
    # solve, legalize, the per-step DRC reads), then one replay run for
    # the byte-equality check, then the candidate count, derived.
    start = clock()
    record = run_tiny_session()
    tiny_wall_s = clock() - start
    replay = run_tiny_session() == record
    entry = _tiny_entry()
    candidates = candidate_count(entry, ceiling=BUDGET_TINY)

    # The harness's first measurement act: the wrapper baseline on the
    # searched board — a fully placed 150-footprint tiny_tapeout, one
    # real export + oracle run per candidate, 30 candidates with
    # per-candidate samples.
    tiny_target = _central_placement(
        record.headline.board,
        Point(entry.outline.max.x // 2, entry.outline.max.y // 2),
    )
    with tempfile.TemporaryDirectory() as scratch:
        workdir = Path(scratch)
        tiny_wrapper = measure_wrapper_baseline(
            ring_variants(
                record.headline.board, tiny_target.ref, count=WRAPPER_CANDIDATES_TINY
            ),
            lambda board: kicad_drc_candidate(board, workdir),
            clock=clock,
        )
    baseline_s = tiny_wrapper.per_candidate_s * candidates

    # The improve-phase readings, on the searched board: the typical move
    # (the ADR-0016/0019 trigger subject) and the round's move mix
    # (ADR-0019's report-only reading).
    _, typical_move = typical_move_us(
        record.headline.board, ceiling=BUDGET_TINY, clock=clock
    )
    mix = move_mix(record.headline.board, ceiling=BUDGET_TINY, clock=clock)
    macro = MacroRow(
        wall_s=tiny_wall_s,
        candidates_evaluated=candidates,
        candidates_per_s=candidates / tiny_wall_s,
        wrapper=tiny_wrapper,
        baseline_derived_s=baseline_s,
        baseline_arithmetic=(
            f"{tiny_wrapper.per_candidate_s:.6f} s/candidate "
            f"x {candidates:,} candidates = {baseline_s:,.1f} s"
        ),
        ratio=baseline_s / tiny_wall_s,
        replay_byte_equal=replay,
        objective_before_nm=record.entry_objective.total_nm,
        objective_after_nm=record.headline.objective.total_nm,
        stop_reason=record.headline.stop_reason.value,
        unmet_constraint_ids=record.headline.unmet_constraint_ids,
        stranded=TINY_STRANDED,
        move_mix=mix,
        typical_move_us=typical_move,
    )

    # The micro row: both terms on ecc83 in this run.
    start = clock()
    ecc83 = run_session()
    ecc83_wall_s = clock() - start
    counts = session_candidate_counts(ecc83)
    ecc83_total = sum(counts)
    engine_per_candidate_s = ecc83_wall_s / ecc83_total
    ecc83_target = _central_placement(
        ecc83.steps[-1].board, Point(20_000_000, 15_000_000)
    )
    with tempfile.TemporaryDirectory() as scratch:
        workdir = Path(scratch)
        ecc83_wrapper = measure_wrapper_baseline(
            ring_variants(
                ecc83.steps[-1].board,
                ecc83_target.ref,
                count=WRAPPER_CANDIDATES_ECC83,
            ),
            lambda board: kicad_drc_candidate(board, workdir),
            clock=clock,
        )
    micro = MicroRow(
        wall_s=ecc83_wall_s,
        candidates_evaluated=ecc83_total,
        candidates_per_step=tuple(zip((step.name for step in ecc83.steps), counts)),
        engine_per_candidate_s=engine_per_candidate_s,
        wrapper=ecc83_wrapper,
        ratio=ecc83_wrapper.per_candidate_s / engine_per_candidate_s,
    )

    # ADR-0001's re-measures on the searched board.
    searched = record.headline.board
    first = searched.placements[0]
    moved = searched.with_placement(
        first.ref, first.pos.x + 1_000, first.pos.y, 0, "F.Cu"
    )
    adr0001 = Adr0001Remeasures(
        board_kib_at_150=_deep_size(searched) / 1024,
        retained_snapshot_kib=(_deep_size(searched, moved) - _deep_size(searched))
        / 1024,
        with_placement_us_at_150=_measure_derive_rate(searched, clock=clock),
    )

    return AcceptanceReport(
        provenance=provenance,
        tiny_tapeout_macro=macro,
        ecc83_micro=micro,
        adr0001_remeasures=adr0001,
        exports=Exports(tiny_tapeout=record.export, ecc83=ecc83.export),
    )


def _print_measurements(report: AcceptanceReport) -> None:
    """The provenance block (ADR-0019), led by the wrapper baseline — the
    harness's first measurement act, named first — then the headline with
    the derived-baseline arithmetic, the micro row, and ADR-0001's
    re-measures."""
    provenance = report.provenance
    print("provenance:")
    print(f"  python: {provenance.python}")
    print(f"  kicad_cli: {provenance.kicad_cli}")
    print(f"  jar_sha256: {provenance.jar_sha256}")
    print(f"  os: {provenance.os}")
    print(f"  cpu: {provenance.cpu}")
    print(f"  fixtures: {dict(provenance.fixtures)}")
    print(f"  budgets: {dict(provenance.budgets)}")

    macro = report.tiny_tapeout_macro
    wrapper = macro.wrapper
    print(
        f"first act, wrapper baseline on tiny_tapeout: {wrapper.candidates} candidates, "
        f"{wrapper.per_candidate_s:.6f} s/candidate, "
        f"slowest/fastest {wrapper.slowest_over_fastest:.3f} (linearity receipt)"
    )
    print(
        f"tiny_tapeout, per-session (macro): wall {macro.wall_s:.2f} s, "
        f"{macro.candidates_evaluated:,} candidates "
        f"({macro.candidates_per_s:.0f}/s acceptance-path), "
        f"stop {macro.stop_reason}, "
        f"unmet {list(macro.unmet_constraint_ids)}, "
        f"stranded {list(macro.stranded)}"
    )
    print(
        f"  objective: {macro.objective_before_nm:,} nm before "
        f"-> {macro.objective_after_nm:,} nm after"
    )
    print(f"  derived baseline: {macro.baseline_arithmetic}")
    print(f"  ratio: {macro.ratio:,.0f}x (floor 1,000x)")
    print(f"  replay byte-equal: {macro.replay_byte_equal}")
    mix_wall = sum(bucket.wall_s for bucket in macro.move_mix)
    if mix_wall > 0:
        parts = " · ".join(
            f"{bucket.label} {bucket.wall_s / mix_wall:.0%}"
            for bucket in macro.move_mix
        )
    else:
        parts = " · ".join(
            f"{bucket.label} {bucket.movers} movers" for bucket in macro.move_mix
        )
    print(
        f"  move mix (improve-round wall): {parts}; "
        f"typical move {macro.typical_move_us:.1f} us/probe"
    )

    micro = report.ecc83_micro
    micro_wrapper = micro.wrapper
    print(
        f"ecc83, per-candidate (micro): engine {micro.engine_per_candidate_s * 1e6:.1f} us, "
        f"wrapper {micro_wrapper.per_candidate_s:.3f} s "
        f"({micro_wrapper.candidates} candidates, "
        f"slowest/fastest {micro_wrapper.slowest_over_fastest:.3f}), "
        f"ratio {micro.ratio:,.0f}x"
    )

    adr0001 = report.adr0001_remeasures
    print(
        f"adr-0001 re-measures @150 placements: board {adr0001.board_kib_at_150:.1f} KiB, "
        f"retained snapshot {adr0001.retained_snapshot_kib:.2f} KiB marginal, "
        f"with_placement {adr0001.with_placement_us_at_150:.1f} us"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    slow = "--slow" in args
    args = [arg for arg in args if arg != "--slow"]
    if len(args) > 1:
        print(
            "usage: python -m net2board.examples.session [--slow] [output.kicad_pcb]",
            file=sys.stderr,
        )
        return 2
    output = Path(args[0]) if args else None

    if slow:
        report = measure_acceptance()
        _print_measurements(report)
        tiny_output = output or DEFAULT_TINY_OUTPUT
        tiny_output.parent.mkdir(parents=True, exist_ok=True)
        tiny_output.write_text(report.exports.tiny_tapeout)
        ecc83_output = (
            tiny_output.with_name(tiny_output.stem + "-ecc83.kicad_pcb")
            if output
            else DEFAULT_OUTPUT
        )
        ecc83_output.parent.mkdir(parents=True, exist_ok=True)
        ecc83_output.write_text(report.exports.ecc83)
        print(f"export: wrote {tiny_output}")
        print(f"export: wrote {ecc83_output}")
        return 0

    output = output or DEFAULT_OUTPUT
    record = run_session()
    print(f"baseline: objective {record.baseline_nm:,} nm (the committed table)")
    for step in record.steps:
        print(
            f"{step.name}: budget {step.budget:,} -> {step.stop_reason.name}, "
            f"unmet {len(step.unmet_constraint_ids)}, "
            f"objective {step.objective.total_nm:,} nm"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(record.export)
    print(f"export: wrote {output} ({len(record.export.encode())} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
