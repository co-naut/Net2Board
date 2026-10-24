"""The harness's measurement face (ADR-0019, ADR-0026): the seam-reported
readings — candidate counts, the improve-round move mix, the typical
move, the wrapper baseline — the acceptance report's dataclasses, its
printed block, and the dated perf history the sign-off compares against.

No session lives here: every function prices what a caller brings — a
``Board``, a move mix, or a report. The acceptance composition —
which sessions to run and in what order — is ``harness.session``'s.

Timing enters only through the injected ``clock``: nothing here reads a
clock itself, and no test may (ADR-0016's no-wall-clock-in-the-fast-tier
rule).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from net2board.harness.paths import REPO_ROOT
from net2board.model import Board
from net2board.solver import solve

__all__ = [
    "AcceptanceReport",
    "Adr0001Remeasures",
    "Exports",
    "MacroRow",
    "MicroRow",
    "MoveMixBucket",
    "NamedMover",
    "Provenance",
    "WrapperBaseline",
    "WrapperSample",
    "bucket_refs",
    "compare_perf_history",
    "latest_previous_perf_json",
    "measure_wrapper_baseline",
    "move_mix",
    "named_movers",
    "render",
    "ring_variants",
    "signoff_date",
    "typical_move",
    "write_perf_json",
]


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
    """One move-mix bucket: how many of the default scope's movers it holds,
    the improve-round wall their probes cost, and the probe count that wall
    is over — the same solve at budget zero (the per-call fixed cost:
    Evaluator build, finalize) subtracted from the wall, so what remains is
    probe work. The count is the scoped solve's own ``candidates_evaluated``
    (#102): the denominator of the bucket's µs/probe reading (#112) — the
    1–2-pad bucket's is the ADR-0016/0019 trigger's subject. Time share
    stays the reading the mix compares; the µs/probe reading rides beside
    it."""

    label: str
    movers: int
    wall_s: float
    candidates_evaluated: int


def _probe_wall(
    board: Board, refs: tuple[str, ...], *, ceiling: int, clock: Callable[[], float]
) -> tuple[float, int]:
    """The scoped solve's probe wall and probe count: the full-budget
    solve minus the same solve at budget zero (the per-call fixed cost —
    Evaluator build, finalize), floored at zero, beside the solve's own
    ``candidates_evaluated`` (#102). The reading both ``move_mix``'s
    buckets and ``named_movers``' rows price their work with."""
    start = clock()
    full = solve(board, refs, budget=ceiling)
    full_s = clock() - start
    probes = full.candidates_evaluated
    start = clock()
    solve(board, refs, budget=0)
    fixed_s = clock() - start
    return max(full_s - fixed_s, 0.0), probes


@dataclass(frozen=True)
class NamedMover:
    """One named mover's row (#92): a single ref priced by its own scoped
    solve, beside the buckets — the big parts' cost documented, not
    flattened into ``9+ pads``. A locked ref takes ``locked=True`` and a
    zero wall (it cannot be scoped), named so; ``pads`` stays the
    footprint's count either way. The probe count is the scoped solve's
    own ``candidates_evaluated`` (#112) — the row's µs/probe denominator;
    a locked row evaluates nothing."""

    ref: str
    pads: int
    locked: bool
    wall_s: float
    candidates_evaluated: int


def bucket_refs(board: Board) -> dict[str, tuple[str, ...]]:
    """The buckets' movers: label → the unlocked refs whose footprint pad
    count falls in the bucket's range, sorted — ``move_mix``'s own
    selection, exposed so a caller can price one bucket's refs without
    re-deriving the selection (the profile face's ``--scope``)."""
    locked = {placement.ref for placement in board.placements if placement.locked}
    pads_of = {
        component.ref: len(component.footprint.pads)
        for component in board.components
        if component.ref not in locked
    }
    return {
        label: tuple(
            sorted(
                ref
                for ref, pads in pads_of.items()
                if pads >= low and (high is None or pads <= high)
            )
        )
        for label, low, high in _MOVE_MIX_BUCKETS
    }


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
    which is the reading the mix exists for. The buckets price walls, not
    probe counts: each scoped solve's spend now rides its own result
    (``candidates_evaluated``, #102), and the share of the wall is the
    reading the mix compares.

    Single refs are priced beside the buckets, never inside them
    (``named_movers``, #92): a named row is not a bucket, and the buckets'
    movers and walls stay the three-bucket mix ADR-0019 described."""
    refs_by_label = bucket_refs(board)
    buckets: list[MoveMixBucket] = []
    for label, _low, _high in _MOVE_MIX_BUCKETS:
        refs = refs_by_label[label]
        wall_s = 0.0
        probes = 0
        if refs:
            wall_s, probes = _probe_wall(board, refs, ceiling=ceiling, clock=clock)
        buckets.append(
            MoveMixBucket(
                label=label,
                movers=len(refs),
                wall_s=wall_s,
                candidates_evaluated=probes,
            )
        )
    return tuple(buckets)


def named_movers(
    board: Board,
    refs: tuple[str, ...],
    *,
    ceiling: int,
    clock: Callable[[], float],
) -> tuple[NamedMover, ...]:
    """The named movers' rows (#92), beside ``move_mix``'s buckets: each
    ref priced by its own scoped solve (``_probe_wall``'s reading), in
    caller order. An absent name is skipped; a repeat is priced once; a
    locked name takes a zero wall, named so, its pad count still the
    footprint's. A locked ref never enters ``solve``'s scope (a caller
    error there), so the lock is read before any solve."""
    locked = {placement.ref for placement in board.placements if placement.locked}
    pads_of = {
        component.ref: len(component.footprint.pads) for component in board.components
    }
    rows: list[NamedMover] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen or ref not in pads_of:
            continue
        seen.add(ref)
        pads = pads_of[ref]
        if ref in locked:
            rows.append(
                NamedMover(
                    ref=ref, pads=pads, locked=True, wall_s=0.0, candidates_evaluated=0
                )
            )
            continue
        wall_s, probes = _probe_wall(board, (ref,), ceiling=ceiling, clock=clock)
        rows.append(
            NamedMover(
                ref=ref,
                pads=pads,
                locked=False,
                wall_s=wall_s,
                candidates_evaluated=probes,
            )
        )
    return tuple(rows)


def typical_move(
    mix: tuple[MoveMixBucket, ...],
) -> tuple[int, float]:
    """The typical move — the reading ADR-0016/0019 pre-registered their
    spatial-index trigger on ("a typical move over 100 µs on the hard
    fixture") — derived from the round's move mix (#112): the 1–2-pad
    bucket's probe wall over its own ``candidates_evaluated``. The
    subject is the passive move that dominates a real round: a bucket's
    reading is order-free by construction, where the retired
    truncated-half slope priced whichever movers the reach-descending
    scan left for its trailing half — and amplified timing noise doing
    it (identical trees read 110.75 and 151.3 µs the same day). The big
    parts' cost stays documented beside the subject (the buckets' and
    named rows' own µs/probe), never folded in. Returns the probe scale
    the reading derives over and the per-probe microseconds. A board
    with no 1–2-pad bucket has no typical move at all; a passive bucket
    that evaluated nothing has no per-probe reading — either caller
    error, never a zero."""
    passive = next((bucket for bucket in mix if bucket.label == "1-2 pads"), None)
    if passive is None:
        raise ValueError(
            "the typical move reads the 1-2 pads bucket; the mix carries none"
        )
    if passive.candidates_evaluated <= 0:
        raise ValueError("the typical move reads the 1-2 pads bucket's probes; got 0")
    return passive.candidates_evaluated, _us_per_probe(
        passive.wall_s, passive.candidates_evaluated
    )


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
    typical_move_us: float
    typical_move_scale: int
    """The typical move (#112) — the ADR-0016/0019 trigger subject, the
    1–2-pad bucket's µs/probe — and the probe scale it derives over (that
    bucket's own ``candidates_evaluated``). Derived from the move mix; no
    solves of its own."""
    move_mix: tuple[MoveMixBucket, ...]
    named_movers: tuple[NamedMover, ...]
    """The named rows (#92): single refs priced beside the buckets —
    the big parts' cost documented, not flattened into ``9+ pads``."""


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


def render(report: AcceptanceReport) -> str:
    """The acceptance report's printed block (#104, public — the face is
    the interface): the provenance block (ADR-0019), led by the wrapper
    baseline — the harness's first measurement act, named first — then
    the headline with the derived-baseline arithmetic, the micro row, and
    ADR-0001's re-measures."""
    provenance = report.provenance
    lines = [
        "provenance:",
        f"  python: {provenance.python}",
        f"  kicad_cli: {provenance.kicad_cli}",
        f"  jar_sha256: {provenance.jar_sha256}",
        f"  os: {provenance.os}",
        f"  cpu: {provenance.cpu}",
        f"  fixtures: {dict(provenance.fixtures)}",
        f"  budgets: {dict(provenance.budgets)}",
    ]

    macro = report.tiny_tapeout_macro
    wrapper = macro.wrapper
    lines += [
        (
            f"first act, wrapper baseline on tiny_tapeout: {wrapper.candidates} candidates, "
            f"{wrapper.per_candidate_s:.6f} s/candidate, "
            f"slowest/fastest {wrapper.slowest_over_fastest:.3f} (linearity receipt)"
        ),
        (
            f"tiny_tapeout, per-session (macro): wall {macro.wall_s:.2f} s, "
            f"{macro.candidates_evaluated:,} candidates "
            f"({macro.candidates_per_s:.0f}/s acceptance-path), "
            f"stop {macro.stop_reason}, "
            f"unmet {list(macro.unmet_constraint_ids)}, "
            f"stranded {list(macro.stranded)}"
        ),
        (
            f"  objective: {macro.objective_before_nm:,} nm before "
            f"-> {macro.objective_after_nm:,} nm after"
        ),
        f"  derived baseline: {macro.baseline_arithmetic}",
        f"  ratio: {macro.ratio:,.0f}x (floor 1,000x)",
        f"  replay byte-equal: {macro.replay_byte_equal}",
    ]
    bucket_wall = sum(bucket.wall_s for bucket in macro.move_mix)
    if bucket_wall > 0:
        parts = " · ".join(
            f"{bucket.label} {bucket.wall_s / bucket_wall:.0%}"
            + (
                f" ({_us_per_probe(bucket.wall_s, bucket.candidates_evaluated):.1f}"
                " us/probe)"
                if bucket.candidates_evaluated
                else ""
            )
            for bucket in macro.move_mix
        )
    else:
        parts = " · ".join(
            f"{bucket.label} {bucket.movers} movers" for bucket in macro.move_mix
        )
    named_parts = " · ".join(
        f"{row.ref} locked"
        if row.locked
        else (
            f"{row.ref} ({row.pads} pads) {row.wall_s:.2f} s"
            + (
                f" ({_us_per_probe(row.wall_s, row.candidates_evaluated):,.1f}"
                " us/probe)"
                if row.candidates_evaluated
                else ""
            )
        )
        for row in macro.named_movers
    )
    if named_parts:
        parts += f"; named {named_parts}"
    lines.append(
        f"  move mix (improve-round wall): {parts}; "
        f"typical move {macro.typical_move_us:.1f} us/probe "
        f"(1-2 pads, {macro.typical_move_scale:,} probes)"
    )

    micro = report.ecc83_micro
    micro_wrapper = micro.wrapper
    lines.append(
        f"ecc83, per-candidate (micro): engine {micro.engine_per_candidate_s * 1e6:.1f} us, "
        f"wrapper {micro_wrapper.per_candidate_s:.3f} s "
        f"({micro_wrapper.candidates} candidates, "
        f"slowest/fastest {micro_wrapper.slowest_over_fastest:.3f}), "
        f"ratio {micro.ratio:,.0f}x"
    )

    adr0001 = report.adr0001_remeasures
    lines.append(
        f"adr-0001 re-measures @150 placements: board {adr0001.board_kib_at_150:.1f} KiB, "
        f"retained snapshot {adr0001.retained_snapshot_kib:.2f} KiB marginal, "
        f"with_placement {adr0001.with_placement_us_at_150:.1f} us"
    )
    return "\n".join(lines) + "\n"


def signoff_date() -> date:
    """Today on this machine's clock — the local sign-off date the prose
    snapshots carry, tz-aware (DTZ). Callers thread it through
    ``measure_acceptance`` and ``compare_perf_history`` so one clock both
    names the perf JSON and reads the history (never the run's own file,
    even across a midnight)."""
    return datetime.now().astimezone().date()


def _perf_history_dir(results_dir: Path | None) -> Path:
    """The perf history's home: the caller's directory, or the repo's
    ``docs/results`` beside the prose snapshots (ADR-0019's dated-files
    pattern; #100)."""
    return results_dir or REPO_ROOT / "docs" / "results"


def _perf_payload(report: AcceptanceReport) -> dict:
    """The JSON face of an acceptance report: every reading it holds, the
    two export blobs excepted (they are files already), under a
    ``schema`` marker for the comparison's future (ADR-0026, #100)."""
    payload = asdict(report)
    del payload["exports"]
    return {"schema": 1, **payload}


def write_perf_json(
    report: AcceptanceReport, results_dir: Path | None = None, today: date | None = None
) -> Path:
    """The machine-readable perf history: ``<date>-perf.json`` beside the
    prose snapshot — one file per day, the day's last run winning
    (ADR-0026, #100). Silent by design: an artifact, not a print. The
    caller's ``today`` names the file — the same value the comparison
    reads, so a run is never its own history even across a midnight."""
    directory = _perf_history_dir(results_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{(today or signoff_date()).isoformat()}-perf.json"
    path.write_text(json.dumps(_perf_payload(report), indent=2) + "\n")
    return path


_PERF_JSON = re.compile(r"^(\d{4}-\d{2}-\d{2})-perf\.json$")


def latest_previous_perf_json(directory: Path, today: date) -> Path | None:
    """The newest perf JSON strictly before ``today`` — the run the
    sign-off compares against (#100). Same-day re-runs overwrite their
    file, so today's is never its own history."""
    dated = []
    for path in directory.glob("*-perf.json"):
        match = _PERF_JSON.match(path.name)
        if match and (stamp := date.fromisoformat(match.group(1))) < today:
            dated.append((stamp, path))
    return max(dated)[1] if dated else None


def _us_per_probe(wall_s: float, probes: int) -> float:
    """One reading's per-probe microseconds — the arithmetic every
    µs/probe figure in the face shares (#112)."""
    return wall_s / probes * 1_000_000


def _band_suffix(delta_pct: float) -> str:
    """A delta's percentage-and-WARN suffix — the ADR-0026 ±20% band,
    shared by every delta line so a band change is a one-site edit."""
    pct = f" ({delta_pct:+.1f}%)"
    return pct + (" WARN beyond ±20%" if abs(delta_pct) > 20.0 else "")


def _delta_line(label: str, old: float, new: float, fmt: str, unit: str) -> str:
    """One reading's delta line — the WARN riding it when the drift
    leaves ADR-0026's ±20% band. Warn-only: a warning opens a ticket,
    fails nothing (#100)."""
    if old:
        return f"  {label}: {fmt.format(old)} -> {fmt.format(new)}{unit}{_band_suffix((new - old) / old * 100)}"
    return f"  {label}: {fmt.format(old)} -> {fmt.format(new)}{unit} (n/a)"


def _probe_fragment(old: dict, new_wall_s: float, new_probes: int) -> str:
    """A delta line's µs/probe fragment (#112): the old reading derived
    from the previous run's wall over its own probe count, the new from
    the current's — the ADR-0026 band judged on it exactly as on walls.
    An empty string when either side carries no probe count: the
    pre-#112 shape leaves the fragment off its wall line (no crash, no
    fabricated number)."""
    old_probes = old.get("candidates_evaluated")
    if not old_probes or not new_probes:
        return ""
    old_us = _us_per_probe(old["wall_s"], old_probes)
    new_us = _us_per_probe(new_wall_s, new_probes)
    return f" · us/probe {old_us:,.1f} -> {new_us:,.1f}{_band_suffix((new_us - old_us) / old_us * 100)}"


def compare_perf_history(
    report: AcceptanceReport,
    results_dir: Path | None = None,
    *,
    today: date | None = None,
) -> list[str]:
    """The sign-off's reading (ADR-0026, #100): the new report against
    the latest previous JSON — every delta prints, anything beyond ±20%
    warns, and a warning opens a ticket, failing nothing. Comparison
    requires matching machine fields (CPU, OS, Python); otherwise it
    prints ``incomparable: machine changed`` and skips."""
    previous = latest_previous_perf_json(
        _perf_history_dir(results_dir), today or signoff_date()
    )
    if previous is None:
        return ["no perf history to compare"]
    current = _perf_payload(report)
    prior = json.loads(previous.read_text())
    changed = [
        field
        for field in ("cpu", "os", "python")
        if prior["provenance"].get(field) != current["provenance"][field]
    ]
    if changed:
        diff = ", ".join(
            f"{field}: {prior['provenance'].get(field)!r}"
            f" -> {current['provenance'][field]!r}"
            for field in changed
        )
        return [f"incomparable: machine changed ({diff})"]
    macro_prior = prior["tiny_tapeout_macro"]
    macro_now = current["tiny_tapeout_macro"]
    micro_prior = prior["ecc83_micro"]
    micro_now = current["ecc83_micro"]
    lines = [f"perf history vs {previous.stem.removesuffix('-perf')}:"]

    def scalar(label, field, old, new, fmt, unit):
        lines.append(_delta_line(label, old[field], new[field], fmt, unit))

    scalar("tiny wall", "wall_s", macro_prior, macro_now, "{:.2f}", " s")
    scalar("tiny ratio", "ratio", macro_prior, macro_now, "{:,.0f}", "")
    if "typical_move_us" in macro_prior:
        scalar(
            "typical move", "typical_move_us", macro_prior, macro_now, "{:.1f}", " us"
        )
    else:
        lines.append("  typical move: absent in the previous run — skipped")
    prior_mix = {b["label"]: b for b in macro_prior["move_mix"]}
    now_mix = [b["label"] for b in macro_now["move_mix"]]
    absent = 0
    for bucket in macro_now["move_mix"]:
        if bucket["label"] in prior_mix:
            prior_bucket = prior_mix[bucket["label"]]
            lines.append(
                _delta_line(
                    f"move mix {bucket['label']}",
                    prior_bucket["wall_s"],
                    bucket["wall_s"],
                    "{:.2f}",
                    " s",
                )
                + _probe_fragment(
                    prior_bucket, bucket["wall_s"], bucket["candidates_evaluated"]
                )
            )
        else:
            absent += 1
    gone = [label for label in prior_mix if label not in now_mix]
    if absent:
        lines.append(
            f"  move mix: {absent} bucket(s) absent in the previous run — skipped"
        )
    if gone:
        lines.append(
            f"  move mix: {len(gone)} bucket(s) gone from the current run — skipped"
        )
    prior_named = {row["ref"]: row for row in macro_prior.get("named_movers", [])}
    now_named = macro_now.get("named_movers", [])
    absent_named = 0
    for row in now_named:
        if row["ref"] in prior_named:
            prior_row = prior_named[row["ref"]]
            lines.append(
                _delta_line(
                    f"named mover {row['ref']}",
                    prior_row["wall_s"],
                    row["wall_s"],
                    "{:.2f}",
                    " s",
                )
                + _probe_fragment(prior_row, row["wall_s"], row["candidates_evaluated"])
            )
        else:
            absent_named += 1
    gone_named = [
        ref for ref in prior_named if ref not in {row["ref"] for row in now_named}
    ]
    if absent_named:
        lines.append(
            f"  named movers: {absent_named} absent in the previous run — skipped"
        )
    if gone_named:
        lines.append(
            f"  named movers: {len(gone_named)} gone from the current run — skipped"
        )
    scalar("ecc83 wall", "wall_s", micro_prior, micro_now, "{:.2f}", " s")
    scalar("ecc83 ratio", "ratio", micro_prior, micro_now, "{:,.0f}", "")
    return lines
