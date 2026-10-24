"""The session lineage and the acceptance composition (ADR-0019,
amended #104): one Board lineage from intent to searched geometry, three
faces with no second harness to drift — the fast-tier acceptance test
imports ``run_session()`` (the replay double-run and the byte goldens),
``uv run python -m net2board.examples.session [output.kicad_pcb]`` prints
the ecc83 measurements and writes the searched export, and ``--slow``
runs the tiny_tapeout acceptance face — the wrapper baseline (the
harness's first act, ADR-0019), the full-budget headline with the derived
ratio, the improve-round move mix and typical-move reading (ADR-0019's
report-only mix; ADR-0016/0019's >100 µs trigger), the ecc83 micro row,
and the ADR-0001 re-measures — a flushed stderr line per phase as each
lands (#101), printing the provenance block, landing the
dated perf JSON, and comparing it against the latest previous run on the
same machine (ADR-0026's warn-only ±20% band, #100). Exit code 0 means
every beat held.

The ecc83 lineage starts from the committed acceptance table
(``apply_placements`` — M1's output is M2's input; nothing here re-authors
a placement) and walks seven pinned-budget solve steps: the under-budgeted
attempt and its full-budget retry (the ``BUDGET_EXHAUSTED``
demonstration), the improvement over the hand-tuned board, the
Criticality re-ranking, the relation statements, a deliberate
unsatisfiable Region — P7 gathered into the bottom band while a
Requirement EdgeMount holds it to the top edge — reported by
``unmet_constraint_ids`` and one ``run_drc`` finding, then relaxed away,
and the ``free_rotation`` beat closing the session (ADR-0024: rotation
enters as a beat, never a headline change).
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
import platform
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import TextIO

from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.errors import Net2BoardError
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_pcb
from net2board.geometry import Point, Rectangle
from net2board.harness.oracle import KICAD_CLI, kicad_drc_candidate
from net2board.harness.paths import SessionPaths
from net2board.harness.perf import (
    AcceptanceReport,
    Adr0001Remeasures,
    Exports,
    MacroRow,
    MicroRow,
    Provenance,
    measure_wrapper_baseline,
    move_mix,
    named_movers,
    ring_variants,
    typical_move,
    write_perf_json,
)
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

__all__ = [
    "BUDGET_MAIN",
    "BUDGET_TINY",
    "BUDGET_UNDERCUT",
    "TINY_ENTRY_CANDIDATES",
    "TINY_HEADLINE_CANDIDATES",
    "WRAPPER_CANDIDATES_ECC83",
    "WRAPPER_CANDIDATES_TINY",
    "PhaseReporter",
    "SessionBeatFailed",
    "SessionRecord",
    "SessionStep",
    "TinySessionRecord",
    "measure_acceptance",
    "run_session",
    "run_tiny_session",
    "session_candidate_counts",
]

OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
STACKUP = ("F.Cu", "B.Cu")

# The budget sizing rule (ADR-0019): these pins keep the session's added
# wall time ≈≤2 s on a developer machine — measured ≈1.1 s for all six
# solves (#98, with the pad checks on the foreign path; #92's native
# path is the remaining lever), re-measured ≈1.2 s for all seven with the
# rotation beat (2026-10-25, #115) — a sizing rule, never a timing
# assertion (nothing here reads a clock, and no test may). BUDGET_UNDERCUT is sized to cut the improve
# step strictly mid-work: the step still exhausts at 3,200 candidates and
# converges by 5,000, and at 1,600 its objective sits strictly between the
# baseline and the optimum. BUDGET_MAIN buys headroom past every
# convergence point; those steps stop on NO_FURTHER_IMPROVEMENT, never on
# the budget.
BUDGET_UNDERCUT = 1_600
BUDGET_MAIN = 20_000

# The tiny_tapeout slow-tier face. BUDGET_TINY follows BUDGET_MAIN's rule
# at macro scale: a round pinned value with generous headroom past the
# measured convergence point — #63's run (live placements map) converged
# at 1,414,784 candidates; #91's pad legality moved the point to
# 1,249,736 (NO_FURTHER_IMPROVEMENT, 2026-10-19) — never touching the
# budget (~2.0x headroom past the current point); a BUDGET_EXHAUSTED
# headline means the pin is wrong. Re-pinned for #63: with the candidate
# families working, the search probes ~5x more candidates than the
# stale-map search's 284,866 — the 500,000 pin sized to that crippled
# search exhausts (the harness's own guard). The wrapper loops are sized
# inside ADR-0019's 20-50-candidate linearity-receipt band — a receipt,
# never a timing assertion.
BUDGET_TINY = 2_500_000
WRAPPER_CANDIDATES_TINY = 30
WRAPPER_CANDIDATES_ECC83 = 20

# The two full-scope counts' pinned readings: what the all-unplaced entry
# solve and the searched headline board's re-solve (a fixed point of
# ``solve``) evaluate under ``BUDGET_TINY``'s ceiling. Since #102 the
# counts are seam-reported — each read off the session's own results, no
# derivation solves — and these pins are the drift goldens the slow tier
# asserts (the report's fresh number names itself in the failure — a
# re-pin, like a golden, #49's rule for budget pins). Entry 1,249,767 and
# headline 164,222 are the counter's exact untruncated counts (2026-10-19,
# #102's validation run, post-#91 pad legality): the retired derivation
# read each low by exactly 31 — the final round's quiet tail, the last
# mover's unfinished scan the #107 semantics now forbid calling NFI. The
# searched board is the objective's fixed point but not a one-probe fixed
# point — a fresh solve from it walks 164,222 probes to re-converge.
TINY_ENTRY_CANDIDATES = 1_249_767
TINY_HEADLINE_CANDIDATES = 164_222

TINY_OUTLINE = Rectangle(min=Point(0, 0), max=Point(104_500_000, 81_000_000))
TINY_STACKUP = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

# The strand reading the headline pins: nothing — the visit order is
# board-derived reach descending (#52), so the 62 mm PinHeader_1x24 strip
# attempts before the other 149 parts converge around it and lands with
# the field. A non-empty reading here means the search stranded a part:
# the engine reports it as the value it is — board.unplaced_refs().
TINY_STRANDED = ()


class SessionBeatFailed(Net2BoardError):
    """A session step's pinned beat did not hold (#104): the stop reason,
    the strand reading, the legalize delta, or the containment posture
    read other than by construction. A typed failure, never a bare
    ``assert`` — ``python -O`` strips asserts, and the acceptance face
    must fail under it. ``profile --budget`` catches this type to profile
    a deliberately truncated run."""


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
    candidates_evaluated: int
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
    free_rotation: bool = False,
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
    result = solve(board, refs, budget=budget, free_rotation=free_rotation)
    if result.stop_reason is not expect_stop:
        raise SessionBeatFailed(
            f"{name}: stopped {result.stop_reason.name}, expected {expect_stop.name}"
        )
    if result.unmet_constraint_ids != expect_unmet:
        raise SessionBeatFailed(
            f"{name}: unmet {result.unmet_constraint_ids}, expected {expect_unmet}"
        )
    if result.board.unplaced_refs() != expect_unplaced:
        raise SessionBeatFailed(
            f"{name}: unplaced {result.board.unplaced_refs()}, "
            f"expected {expect_unplaced}"
        )
    objective = evaluate(result.board)
    relegalized = legalize(result.board)
    # ADR-0013's reading, measured: the step's board is already the
    # legalizer's fixed point, so re-deriving repair moves nothing and the
    # legalize-induced objective delta is exactly 0.
    legalize_delta_nm = evaluate(relegalized.board).total_nm - objective.total_nm
    if legalize_delta_nm != 0 or relegalized.board is not result.board:
        raise SessionBeatFailed(
            f"{name}: legalize delta {legalize_delta_nm} nm "
            f"(expected exactly 0 — the step's board is the legalizer's fixed point)"
        )
    for finding in run_drc(result.board):
        if finding.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE:
            raise SessionBeatFailed(f"{name}: containment finding {finding}")
        if finding.type is ViolationType.EDGE_MOUNT_UNMET:
            raise SessionBeatFailed(f"{name}: EdgeMount finding {finding}")
    return SessionStep(
        name=name,
        budget=budget,
        refs=refs,
        stop_reason=result.stop_reason,
        candidates_evaluated=result.candidates_evaluated,
        unmet_constraint_ids=result.unmet_constraint_ids,
        legalize_delta_nm=legalize_delta_nm,
        objective=objective,
        board=result.board,
    )


def run_session(fixtures: SessionPaths | None = None) -> SessionRecord:
    """The whole session over one Board lineage — deterministic, so a
    second call replays it byte for byte. The fixture roots are stated
    inputs (#68): ``None`` resolves ``SessionPaths.repo_default()``."""
    if fixtures is None:
        fixtures = SessionPaths.repo_default()
    spec = BoardSpec.from_files(
        fixtures.ecc83_netlist, fixtures.ecc83_footprints, OUTLINE, STACKUP
    )
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

    # The free_rotation beat (ADR-0024's goldens clause): rotation enters
    # as a beat, never a headline change — one knob-on step closes the
    # session, and its board is the export the GUI sign-off reads (item 5,
    # rotated parts facing sensibly). Measured 2026-10-25 (#115): from the
    # searched optimum the knob-on walk converges at 6,628 candidates
    # inside BUDGET_MAIN (~3x headroom), changes no rotation — the board
    # is certified turn-optimal, not assumed — and still improves the
    # objective by 500,000 nm (P2, found because the rotation families
    # re-order the walk; the knob-off re-solve spends 1,181 candidates to
    # prove itself stuck). The turning bite lives in the replay face
    # (TestKnobOnReplay: U1 to 270 degrees from the baseline); the beat
    # face's rotated part is R3, at the committed table's own 90 degrees.
    rotation = _step("rotation", board, budget=BUDGET_MAIN, free_rotation=True)
    board = rotation.board
    steps.append(rotation)

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


def _tiny_entry(fixtures: SessionPaths) -> Board:
    """The all-unplaced tiny_tapeout board — the headline's entry."""
    spec = BoardSpec.from_files(
        fixtures.tiny_netlist, fixtures.tiny_footprints, TINY_OUTLINE, TINY_STACKUP
    )
    return build_board(spec)


def run_tiny_session(fixtures: SessionPaths | None = None) -> TinySessionRecord:
    """The tiny_tapeout headline (ADR-0019's slow-tier acceptance): one
    full-budget solve from the all-unplaced board — 150 components, 114
    nets, no authored intent, wirelength and containment only. The strand
    reading is pinned (``TINY_STRANDED``); legalize delta exactly 0,
    containment clean, and the unmet set empty are asserted by
    construction in ``_step``. The wrapper baseline is measured against
    this fixture's boards in ``measure_acceptance`` — never back-filled
    from ecc83 (ADR-0019). Fixture roots are stated inputs (#68)."""
    if fixtures is None:
        fixtures = SessionPaths.repo_default()
    entry = _tiny_entry(fixtures)
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
    """Each ecc83 step's exact candidate count, read off the record's own
    steps (#102): every step's solve reports its ``candidates_evaluated``
    on the result, the step records it, and replay carries it."""
    return tuple(step.candidates_evaluated for step in record.steps)


def _jar_sha256(fixtures: SessionPaths) -> str:
    jar = fixtures.freerouting_jar
    if jar is None or not jar.exists():
        return "absent"
    return hashlib.sha256(jar.read_bytes()).hexdigest()


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


class PhaseReporter:
    """The per-phase progress line (#101): ``[+312s] tiny session done``
    to ``out`` — stderr by default, resolved at call time so runtime
    redirection (a detached run's log, a test's capture) is picked up —
    flushed immediately. A death or a slowdown then points at a phase,
    and the lines double as a free progress read for long runs. Elapsed
    wall runs from the reporter's construction, on the same injected
    clock as the measurement face it reports on."""

    def __init__(
        self,
        out: TextIO | None = None,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._out = out
        self._clock = clock
        self._start = clock()

    def phase(self, label: str) -> None:
        """Write one phase line."""
        elapsed = self._clock() - self._start
        print(
            f"[+{elapsed:.0f}s] {label}",
            file=sys.stderr if self._out is None else self._out,
            flush=True,
        )


def measure_acceptance(
    *,
    clock: Callable[[], float] = perf_counter,
    fixtures: SessionPaths | None = None,
    results_dir: Path | None = None,
    today: date | None = None,
    progress: TextIO | None = None,
) -> AcceptanceReport:
    """The slow-tier measurement face. The wrapper baseline is the first
    *measurement* act (ADR-0019 — a named step, never back-filled from
    ecc83), and it prices the searched board: a wrapper candidate is a
    fully placed export, so the headline solve has to run before the
    wrapper loop can. The printed output names it first; the ratio and
    the ecc83 micro row follow; ADR-0001's re-measures close. Every
    reading enters the report with the numbers that produced it; the
    ratio is asserted by the slow-tier test, reported here. Fixture
    roots are stated inputs (#68). The report also lands as
    ``docs/results/<date>-perf.json`` — the machine-readable history the
    sign-off compares against (ADR-0026, #100); ``results_dir`` redirects
    the write, which the slow-tier tests do — a pytest run must not touch
    the repo's history. ``today`` pins the JSON's date: the caller passes
    the same value to the comparison, so one clock names the file and
    reads the history. ``progress`` takes the per-phase lines — a
    ``PhaseReporter`` on it (stderr by default), one flushed line per
    phase (#101): a death points at the phase that never reported."""
    reporter = PhaseReporter(progress, clock=clock)
    reporter.phase("acceptance run started")
    if fixtures is None:
        fixtures = SessionPaths.repo_default()
    provenance = Provenance(
        python=sys.version.split()[0],
        kicad_cli=KICAD_CLI.version(),
        jar_sha256=_jar_sha256(fixtures),
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
    reporter.phase("provenance done")

    # The headline, measured: one session wall (the acceptance path —
    # solve, legalize, the per-step DRC reads), then one replay run for
    # the byte-equality check. The entry board is rebuilt only for its
    # outline geometry below; its candidate count is the headline step's
    # own, read off the record (#102) — the session's headline solve at
    # BUDGET_TINY is the entry walk.
    start = clock()
    record = run_tiny_session(fixtures)
    tiny_wall_s = clock() - start
    reporter.phase("tiny session done")
    replay = run_tiny_session(fixtures) == record
    reporter.phase("replay done")
    entry = _tiny_entry(fixtures)
    candidates = record.headline.candidates_evaluated

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
    reporter.phase("tiny wrapper baseline done")

    # The improve-phase readings, on the searched board: the round's move
    # mix, the typical move derived from it (#112 — the ADR-0016/0019
    # trigger subject, no solves of its own), and the big parts' named
    # rows (#92) priced beside — never inside.
    mix = move_mix(
        record.headline.board,
        ceiling=BUDGET_TINY,
        clock=clock,
    )
    reporter.phase("move mix done")
    typical_scale, typical_move_us = typical_move(mix)
    reporter.phase("typical move done")
    named = named_movers(
        record.headline.board,
        ("J4", "U6"),
        ceiling=BUDGET_TINY,
        clock=clock,
    )
    reporter.phase("named movers done")
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
        named_movers=named,
        typical_move_us=typical_move_us,
        typical_move_scale=typical_scale,
    )

    # The micro row: both terms on ecc83 in this run.
    start = clock()
    ecc83 = run_session(fixtures)
    ecc83_wall_s = clock() - start
    reporter.phase("ecc83 session done")
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
    reporter.phase("ecc83 wrapper baseline done")
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
    reporter.phase("ADR-0001 re-measures done")

    report = AcceptanceReport(
        provenance=provenance,
        tiny_tapeout_macro=macro,
        ecc83_micro=micro,
        adr0001_remeasures=adr0001,
        exports=Exports(tiny_tapeout=record.export, ecc83=ecc83.export),
    )
    write_perf_json(report, results_dir, today)
    reporter.phase("perf JSON written")
    return report
