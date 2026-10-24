"""The solve seam — budgeted, deterministic, Board-in/Board-out (ADR-0013).

``solve`` is the seam the orchestrator's inner loop hides behind: a pure
derivation in the ``build_board`` family — Board in, ``SolveResult`` out —
reading intent off the Board (no ``constraints`` and no ``objective``
parameter, so no intent channel bypasses the snapshot) and ending in
``legalize``, which makes the promise legal-or-named-failure. The reports say
*could not satisfy*, never *unsatisfiable*: budgeted local search neither
finds optima nor proves impossibility.

The algorithm is a deterministic greedy local search over the flat per-call
Evaluator (ADR-0016), in three phases:

- **Place** — every unplaced in-scope ref, biggest first (the board-derived
  reach-descending visit order, #52), lands at the best legal candidate from
  a wirelength-driven family: pad-alignment onto
  each placed netmate (flush, plus a ring of courtyard-clearing offsets),
  then per-net AABB centres — knob on (``free_rotation``, ADR-0021/0025),
  the alignment family is rebuilt at each of the four rotations, each
  rotation aligning its own pads. A candidate is probed before it is
  trusted; a
  ref no candidate satisfies falls to a coarse first-fit scan of the
  outline (rotation 0 throughout — the last-resort legality finder),
  and one that finds no legal position anywhere stays unplaced —
  failure-as-value, visible via ``board.unplaced_refs()``.
- **Improve** — rounds over the in-scope movers, biggest first; each
  mover scans the same wirelength family plus a small local ring — knob
  on, followed by the three in-place turns and the other rotations'
  alignment families, in ADR-0025 §17's order — and
  commits the best candidate whose key ``(error_count, total_nm)`` strictly
  improves on the current board. The key's legality-first ordering is
  ADR-0014's split made operational: a legal board only ever accepts legal
  candidates (the Objective ranks Requirement-legal boards), while an
  illegal one climbs the overlap/clearance gradient — the error-count term —
  toward the legal set. A full round with zero commits is a local optimum:
  ``NO_FURTHER_IMPROVEMENT``. The budget truncating a scan — candidates
  still unevaluated — is ``BUDGET_EXHAUSTED``, never a rounded-off optimum
  (#107).
- **Finalize** — the movers are materialized through ``with_placement``
  (the public construction path, so the returned Board satisfies every
  invariant it maintains), and the unbudgeted final ``legalize`` phase runs
  — identity by construction whenever the search stayed legal, so the
  reported objective equals the searched one (ADR-0014's conditional).

The guarantees, from the ADR:

- **Budget in candidates** — one candidate is one Evaluator probe (ADR-0016),
  counted whether accepted or rejected; the search reads each probe's counted
  verdict — the ranking key the full probe reports, not materialized (ADR-0016,
  amended by #64; the same legality notion, never a second check). The final
  ``legalize`` phase runs the full canonical path and is unbudgeted, because a
  budgeted final phase could return an illegal board on exhaustion. No wall
  clock anywhere.
- **Determinism unconditional** — same ``(board, refs, budget)``, same
  result. No caller seed: internalization is engine-fixed. Chaining is
  documented non-idempotence: ``solve(solve(b, n), n)`` is not
  ``solve(b, 2n)`` and neither is guaranteed better.
- **Failure is a value** — unmet Requirements surface as ids on the result
  and as ordinary Violations via ``run_drc``; a ref that comes back in
  ``board.unplaced_refs()`` *is* the placement failure. Input errors raise:
  a malformed budget, or a ``refs`` entry naming no component or a locked
  placement, raise under ``SolveError`` — the ref cases through the
  ref-carrying ``SolveRefError``.
- **Identity on no-op** — a call that commits nothing on a legal board
  returns the very Board object it was given; safe speculative calls.
- **Total order on ties** — equal-cost candidates resolve on the canonical
  value: ``(error_count, total_nm)`` first, then the Pose ``(x, y, rotation)``
  in integer nm (ADR-0025);
  movers are visited reach-descending — the board-derived canonical order,
  half-diagonal first, lexicographic ref breaking ties (#52). No
  ``dict``/``set`` iteration order leaks into a result.
- **O(1) snapshot coexistence** — the search works on the flat Evaluator
  (the one live state, read through ``placement``/``position``, #67),
  holding no Board snapshots beyond the entry board and the one
  materialization at the end; no beam.

Nothing is observable mid-solve: no callback, no cancellation, no trajectory
— the budget is the only control surface. The feedback is end-of-call only:
``stop_reason`` and the Candidates-evaluated count (#102), both read off the
finished result.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from math import isqrt

from net2board.drc import Severity, run_drc
from net2board.errors import Net2BoardError
from net2board.geometry import (
    Point,
    measure_box,
    pad_world_center,
)
from net2board.model import ROTATIONS, Board, Net, Placement
from net2board.solver._evaluator import Evaluator, Pose
from net2board.solver._legalize import legalize

__all__ = [
    "SolveError",
    "SolveRefError",
    "SolveResult",
    "StopReason",
    "solve",
]


class StopReason(Enum):
    """Why the search stopped (ADR-0013): ``NO_FURTHER_IMPROVEMENT`` — a
    full Round of untruncated Scans with zero commits, a local optimum
    without claiming a global one — or ``BUDGET_EXHAUSTED``, the
    orchestrator's cue to spend more. An extensible registry: a later
    reason appends itself here."""

    NO_FURTHER_IMPROVEMENT = "no_further_improvement"
    BUDGET_EXHAUSTED = "budget_exhausted"


class SolveError(Net2BoardError):
    """A malformed ``solve`` input (ADR-0013, ADR-0008's phase split): the
    budget, or the ``refs`` scope. Search outcomes are never exceptions —
    they are values on ``SolveResult``."""


@dataclass(frozen=True)
class SolveRefError(SolveError):
    """A ``refs`` entry the Board cannot honour (ADR-0013): it names no
    netlist component, or names a ``locked`` placement — a caller
    contradiction, since silently skipping it would hide an orchestrator
    bug."""

    ref: str

    def __str__(self) -> str:
        return (
            f"ref {self.ref!r} cannot be scoped: not an unlocked placement "
            f"or unplaced ref of this board"
        )


@dataclass(frozen=True)
class SolveResult:
    """The outcome of one ``solve`` call (ADR-0013): the Board (legal or
    honestly still illegal — ``run_drc`` is the authority), the canonically
    sorted ids of Requirements the returned board does not satisfy, the
    stop reason, and the Candidates the call evaluated (#102). No success
    boolean (``unmet_constraint_ids == ()`` plus a legal board *is* it), no
    carried Violations, no cost reading, no displacement report."""

    board: Board
    unmet_constraint_ids: tuple[str, ...]
    stop_reason: StopReason
    candidates_evaluated: int
    """The Budget denominated honestly: the Candidates this call probed,
    ``min(budget, N)`` — every one counted whether accepted or rejected
    (ADR-0016). A deterministic function of ``(board, refs, budget)``: it
    replays, and it is the harness's candidate denominator without a
    derivation solve. Never a mid-solve channel: it exists only on the
    finished result."""


_PLACEMENT_MARGIN_NM = 1_000
"""Clearance a placement candidate aims for past the faces it must clear —
not minimal, and always verified exactly by the probe."""

_RING_RADII_NM = (500_000, 1_000_000, 2_000_000, 4_000_000)
"""The local improvement ring: eight directions at four radii — the small,
deterministic perturbation family around a mover's current position."""

_GRID_PITCH_NM = 2_000_000
"""The fallback placement scan's pitch — coarse, first-fit, only reached
when no wirelength candidate is legal."""

_GRID_INSET_NM = 1_000_000
"""The fallback scan's inset from the outline, keeping candidates' yards
inside the board."""

ORIGIN = Point(0, 0)
"""The origin placement position — the reference point pad offsets are
read against (``pad_world_center`` on a placement at the origin composes
rotation and the back-side mirror exactly once)."""


def solve(
    board: Board,
    refs: Iterable[str] | None = None,
    *,
    budget: int,
    free_rotation: bool = False,
) -> SolveResult:
    """Improve a Board by budgeted deterministic local search (ADR-0013):
    Board in, ``SolveResult`` out, the final phase ``legalize``.

    ``refs`` names the parts *this call is about* — bare placement refs,
    matching ``with_placement``'s surface — defaulting to every unlocked
    placement plus every unplaced ref. The budget is counted in candidates
    evaluated (one Evaluator probe each) and is never exceeded;
    ``budget=0`` evaluates nothing. Every unplaced in-scope ref is placed,
    constraint-free or not; one that finds no legal position stays unplaced
    — visible as ``board.unplaced_refs()`` on the result. Locked placements
    never move, in scope or out.

    ``free_rotation`` (ADR-0021, ADR-0025) lets the search set each
    unlocked in-scope ref's rotation in exact 90° steps — knob off, every
    candidate carries its ref's standing rotation and the behaviour is
    the pre-knob search's to the byte. Side stays caller-fixed either
    way; a caller who cares about orientation locks the part.

    The result reports its own spend (#102): ``candidates_evaluated`` is
    the Candidates this call probed — ``min(budget, N)``, never more than
    the Budget — deterministic in ``(board, refs, budget)`` like the rest
    of the result.
    """
    checked_budget = _checked_budget(budget)
    checked_knob = _checked_knob(free_rotation)
    scope = _checked_scope(board, refs)
    search = _Search(board, scope, checked_budget, free_rotation=checked_knob)
    return search.run()


def _checked_budget(budget: int) -> int:
    """Validate the budget — the one non-ref invalid-input surface
    (ADR-0013). Zero is a valid no-op; everything else about a weak result
    is a value."""
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise SolveError(
            f"budget must be an integer (candidates evaluated), got {budget!r}"
        )
    if budget < 0:
        raise SolveError(f"budget must be non-negative, got {budget!r}")
    return budget


def _checked_knob(free_rotation: bool) -> bool:
    """Validate the knob (ADR-0021: it stays a ``bool`` over the call's
    scope — a truthy non-bool is a caller bug, not a preference)."""
    if not isinstance(free_rotation, bool):
        raise SolveError(f"free_rotation must be a boolean, got {free_rotation!r}")
    return free_rotation


def _checked_scope(board: Board, refs: Iterable[str] | None) -> tuple[str, ...]:
    """Resolve ``refs`` against the Board: bare component refs, unlocked,
    canonically sorted and deduplicated. ``None`` scopes every unlocked
    placement plus every unplaced ref (ADR-0013)."""
    if refs is None:
        locked = {placement.ref for placement in board.placements if placement.locked}
        return tuple(sorted({component.ref for component in board.components} - locked))
    if isinstance(refs, str):
        raise SolveError(
            f"refs must be a collection of refs, not a bare string: {refs!r}"
        )
    component_refs = {component.ref for component in board.components}
    locked = {placement.ref for placement in board.placements if placement.locked}
    scope: set[str] = set()
    for entry in refs:
        if not isinstance(entry, str):
            raise SolveError(f"refs entries must be strings, got {entry!r}")
        if entry not in component_refs or entry in locked:
            raise SolveRefError(entry)
        scope.add(entry)
    return tuple(sorted(scope))


@dataclass(frozen=True)
class _Candidate:
    """One probed candidate: its Pose and the probe's ranking key
    ``(error_count, total_nm)`` — legality first, wirelength second
    (ADR-0014's split made operational)."""

    pose: Pose
    errors: int
    total_nm: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.errors, self.total_nm)


class _Search:
    """One solve call's working state: the warm Evaluator — whose
    committed placements and positions are the only live state (#67) —
    this search's own originals snapshot, the probe ledger, and the
    canonical iteration orders."""

    def __init__(
        self,
        board: Board,
        scope: tuple[str, ...],
        budget: int,
        *,
        free_rotation: bool = False,
    ):
        self._board = board
        self._scope = _visit_order(board, scope)
        self._budget = budget
        self._free_rotation = free_rotation
        self._evaluator = Evaluator(board)
        # this search's own snapshot of where the input board stood — the
        # live state reads through the Evaluator (#67). Poses, not points
        # (ADR-0025): the Pose is the one operation's argument, and a Pose
        # snapshot is what makes finalize's skip rotation-aware.
        originals: dict[str, Pose | None] = {}
        for component in board.components:
            placement = board.placement(component.ref)
            originals[component.ref] = None if placement is None else Pose.of(placement)
        self._originals = originals
        self._spent = 0
        # #107: a scan the budget truncated with candidates unevaluated —
        # the round that contains it is not a local optimum, however quiet
        self._scan_truncated = False
        self._nets_by_ref: dict[str, list[Net]] = {}
        for net in board.nets:
            for ref, _pad in net.pins:
                self._nets_by_ref.setdefault(ref, []).append(net)

    def run(self) -> SolveResult:
        if self._spent >= self._budget:
            return self._finalize(StopReason.BUDGET_EXHAUSTED)
        exhausted = self._place_unplaced()
        if exhausted:
            return self._finalize(StopReason.BUDGET_EXHAUSTED)
        return self._finalize(self._improve())

    # Phase A — placement -------------------------------------------------

    def _place_unplaced(self) -> bool:
        """Place every unplaced in-scope ref, biggest first (#52). True
        when the budget ran out first."""
        for ref in self._scope:
            if self._evaluator.position(ref) is not None:
                continue
            if self._place_one(ref):
                return True
        return False

    def _place_one(self, ref: str) -> bool:
        """Land one unplaced ref at the best legal wirelength candidate,
        falling back to the first-fit outline scan. True when the budget
        ran out before the ref could be decided."""
        best: _Candidate | None = None
        for pose in self._placement_candidates(ref):
            if self._spent >= self._budget:
                return True
            probed = self._probe(ref, pose)
            if probed.errors == 0 and (best is None or _rank(probed) < _rank(best)):
                best = probed
        if best is None:
            for pose in self._grid_candidates():
                if self._spent >= self._budget:
                    return True
                probed = self._probe(ref, pose)
                if probed.errors == 0:
                    best = probed
                    break
        if best is not None:
            self._commit(ref, best)
        return False

    def _placement_candidates(self, ref: str) -> tuple[Pose, ...]:
        """The wirelength-driven placement family, probed in order: the
        standing-rotation pad-alignment family first, then — knob on —
        the other rotations' alignment families in ascending order
        (ADR-0025 §17: the knob-off prefix first, so a budget-truncated
        knob-on scan has examined everything a knob-off scan would
        have). Alignment only — the in-place turns are improve's moves
        at a current position, and place's callers are unplaced refs
        whose rotation is chosen at placement."""
        origin = self._origin(ref)
        candidates: dict[Pose, Pose] = {}
        for pose in self._alignment_candidates(ref, origin, origin.rotation):
            candidates.setdefault(pose, pose)
        if self._free_rotation:
            for pose in self._other_rotation_alignments(ref, origin, origin.rotation):
                candidates.setdefault(pose, pose)
        return tuple(candidates.values())

    def _origin(self, ref: str) -> Placement:
        """The ref's anchor at the origin pose — pad offsets are read
        against ``ORIGIN``, so ``pad_world_center`` composes the
        rotation and the back-side mirror exactly once per candidate
        block."""
        return dataclasses.replace(
            _anchor_placement(self._board, self._evaluator.placement(ref), ref),
            pos=ORIGIN,
        )

    def _other_rotation_alignments(
        self, ref: str, origin: Placement, standing: int
    ) -> Iterator[Pose]:
        """The other rotations' alignment families in ascending order
        (ADR-0025 §17: these follow the knob-off prefix — the standing
        rotation's family, first). ``ROTATIONS`` is the model's set —
        the one ``Placement`` validation enforces, so every candidate
        stays inside the allowed set by construction."""
        for rotation in ROTATIONS:
            if rotation == standing:
                continue
            yield from self._alignment_candidates(ref, origin, rotation)

    def _alignment_candidates(
        self, ref: str, origin: Placement, rotation: int
    ) -> tuple[Pose, ...]:
        """One rotation's pad-alignment family, probed in order:
        pad-alignment onto each placed netmate — flush, then a ring of
        courtyard-clearing offsets — then the per-net AABB centre. The
        alignment geometry is computed at ``rotation``'s pad offset
        exactly as the knob-off search computes it at the standing
        rotation (ADR-0025: each rotation aligns its own pads — this is
        not one family crossed with four rotations). Knob off, only the
        standing rotation's family is ever generated and this is the
        pre-knob family to the byte."""
        anchor = dataclasses.replace(origin, rotation=rotation)
        reach = _half_diagonal(anchor)
        candidates: dict[Pose, Pose] = {}
        for net in self._nets_by_ref.get(ref, ()):
            own_pads = [pad for mate, pad in net.pins if mate == ref]
            if not own_pads:
                continue
            offset = pad_world_center(anchor, own_pads[0])
            anchors: list[tuple[Point, int]] = []
            for mate, pad in net.pins:
                if mate == ref:
                    continue
                mate_placement = self._evaluator.placement(mate)
                if mate_placement is None:
                    continue
                anchors.append(
                    (
                        pad_world_center(mate_placement, pad),
                        _half_diagonal(mate_placement),
                    )
                )
            if not anchors:
                continue
            for anchor_point, clearance in anchors:
                pos = _minus(anchor_point, offset)
                pose = Pose(pos.x, pos.y, rotation)
                candidates.setdefault(pose, pose)
                for direction in _DIRECTIONS:
                    span = reach + clearance + _PLACEMENT_MARGIN_NM
                    pos = Point(
                        anchor_point.x + direction[0] * span - offset.x,
                        anchor_point.y + direction[1] * span - offset.y,
                    )
                    pose = Pose(pos.x, pos.y, rotation)
                    candidates.setdefault(pose, pose)
            centre = _minus(
                _aabb_centre([anchor_point for anchor_point, _ in anchors]), offset
            )
            pose = Pose(centre.x, centre.y, rotation)
            candidates.setdefault(pose, pose)
        return tuple(candidates.values())

    def _grid_candidates(self) -> tuple[Pose, ...]:
        """The deterministic first-fit scan: row-major over the outline,
        inset, at ``_GRID_PITCH_NM`` — rotation 0 throughout (ADR-0025:
        the grid is the last-resort legality finder, it never rotates,
        and improve turns what it places afterwards)."""
        outline = self._board.outline
        xs = range(
            outline.min.x + _GRID_INSET_NM,
            outline.max.x - _GRID_INSET_NM + 1,
            _GRID_PITCH_NM,
        )
        ys = range(
            outline.min.y + _GRID_INSET_NM,
            outline.max.y - _GRID_INSET_NM + 1,
            _GRID_PITCH_NM,
        )
        return tuple(Pose(x, y, 0) for y in ys for x in xs)

    # Phase B — improvement -----------------------------------------------

    def _improve(self) -> StopReason:
        """Rounds over the in-scope movers, biggest first (#52); each mover
        commits its best strictly-improving candidate. A full round with
        zero commits is the local optimum. A round whose scan the budget
        truncated — candidates unevaluated behind the cut — is not (#107):
        it stops ``BUDGET_EXHAUSTED``, however quiet the truncated scan
        was. A cut that already found an improving candidate still commits
        it (the truncation binds selection, ADR-0025 §17); the next
        round's first unevaluated candidate then reports the exhaustion."""
        while True:
            committed = False
            for mover in self._scope:
                best = self._best_move(mover)
                if best is None and self._scan_truncated:
                    return StopReason.BUDGET_EXHAUSTED
                if best is not None:
                    self._commit(mover, best)
                    committed = True
            if not committed:
                return StopReason.NO_FURTHER_IMPROVEMENT

    def _best_move(self, mover: str) -> _Candidate | None:
        """The mover's best candidate by ``(key, pose)`` — the canonical
        total order — or ``None`` when nothing beats staying put. The
        scan may have been budget-truncated (#107): the flag says so, so
        the caller can tell an honest optimum from a cut-short scan that
        merely found nothing in its prefix."""
        best: _Candidate | None = None
        for pose in self._move_candidates(mover):
            if self._spent >= self._budget:
                self._scan_truncated = True
                break
            probed = self._probe(mover, pose)
            if best is None or _rank(probed) < _rank(best):
                best = probed
        if best is None or best.key >= self._current_key():
            return None
        return best

    def _move_candidates(self, mover: str) -> tuple[Pose, ...]:
        """The improvement family, probed in order (ADR-0025 §17): the
        knob-off family first — the standing-rotation pad alignments,
        then the local ring at the standing rotation, never the position
        itself — then, knob on, the three in-place turns in ascending
        rotation, then the other rotations' alignment families in
        ascending order. The family dedup key is the Pose. Selection is
        decided by the ``(errors, total_nm)`` key and the
        ``(x, y, rotation)`` tie-break regardless of order, so the order
        binds only under budget truncation — where the knob-off prefix
        guarantees a truncated knob-on scan examined everything a
        knob-off scan would have, first."""
        origin = self._origin(mover)
        standing = origin.rotation
        candidates: dict[Pose, Pose] = {}
        for pose in self._alignment_candidates(mover, origin, standing):
            candidates.setdefault(pose, pose)
        placement = self._evaluator.placement(mover)
        if placement is not None:
            current = placement.pos
            for radius in _RING_RADII_NM:
                for direction in _DIRECTIONS:
                    pos = Point(
                        current.x + direction[0] * radius,
                        current.y + direction[1] * radius,
                    )
                    pose = Pose(pos.x, pos.y, standing)
                    candidates.setdefault(pose, pose)
            candidates.pop(Pose(current.x, current.y, standing), None)
            if self._free_rotation:
                for rotation in ROTATIONS:
                    if rotation == standing:
                        continue  # the current Pose is not its own candidate
                    pose = Pose(current.x, current.y, rotation)
                    candidates.setdefault(pose, pose)
        if self._free_rotation:
            for pose in self._other_rotation_alignments(mover, origin, standing):
                candidates.setdefault(pose, pose)
        return tuple(candidates.values())

    # Shared machinery ----------------------------------------------------

    def _probe(self, ref: str, pose: Pose) -> _Candidate:
        """One candidate evaluated — the budgeted unit (ADR-0016): counted
        whether accepted or rejected. The search reads the probe's counted
        verdict — the ranking key the full probe reports, not materialized
        (ADR-0016, amended by #64); the final ``legalize`` phase stays
        full-cost."""
        self._spent += 1
        errors, total_nm = self._evaluator.probe_key(ref, pose)
        return _Candidate(pose=pose, errors=errors, total_nm=total_nm)

    def _commit(self, ref: str, candidate: _Candidate) -> None:
        """Land a probed candidate. The Evaluator is the only live state
        (#67): its ``commit`` carries the full candidate (rotation, side,
        locked preserved by the plan), so the candidate families' reads —
        pad-alignment anchors included — see the new state through the
        Evaluator's own placements. The #63 stale-map bug class is gone
        structurally: there is no second map to forget."""
        self._evaluator.commit(ref, candidate.pose)

    def _current_key(self) -> tuple[int, int]:
        """The board's ranking key, read off the Evaluator's maintained
        count and total (#64) — no canonical assembly per mover."""
        return self._evaluator.current_key()

    def _finalize(self, stop_reason: StopReason) -> SolveResult:
        """Materialize the movers through the public construction path,
        run the unbudgeted final ``legalize`` phase, and read the unmet
        Requirement ids off what actually comes back."""
        board = self._board
        for ref in self._scope:
            pose = self._evaluator.pose(ref)
            # the skip compares Poses (ADR-0025): a position-only
            # comparison silently drops a part turned in place — the
            # latent defect the Pose typing fixes before #90's knob can
            # turn
            if pose is None or pose == self._originals[ref]:
                continue
            anchor = _anchor_placement(self._board, self._evaluator.placement(ref), ref)
            board = board.with_placement(
                ref, pose.x, pose.y, pose.rotation, anchor.side, anchor.locked
            )
        finalized = legalize(board).board
        return SolveResult(
            board=finalized,
            unmet_constraint_ids=_unmet_constraint_ids(finalized),
            stop_reason=stop_reason,
            candidates_evaluated=self._spent,
        )


def _anchor_placement(board: Board, placement: Placement | None, ref: str) -> Placement:
    """The ref's placement as it stands — an unplaced ref anchors at
    rotation 0 on ``F.Cu``, exactly the Evaluator's ``None → point``
    semantics (ADR-0016: side is caller-fixed; an unplaced ref's
    rotation starts at 0 and, knob on, is chosen at placement —
    ADR-0021).
    ``placement`` is the ref's live read (#67): the caller pulls it off
    the Evaluator, or off the Board for pre-search reads."""
    if placement is not None:
        return placement
    component = board.component(ref)
    if component is None:
        raise ValueError(
            f"unknown ref {ref!r}: not a component of this board's netlist"
        )
    return Placement(
        ref=ref,
        footprint=component.footprint,
        pos=ORIGIN,
        rotation=0,
        side="F.Cu",
    )


def _visit_order(board: Board, scope: tuple[str, ...]) -> tuple[str, ...]:
    """The canonical visit order (#52): board-derived reach descending —
    each ref's half-diagonal, measured on the placement as it stands (an
    unplaced ref measures at the origin anchor, exactly the placement
    candidates' semantics) — ties resolving on the lexicographic ref.
    The big parts attempt before the field converges around them: a long
    strip visited last searches a board with no room left. Deterministic
    by construction — a sorted total order, no ``dict``/``set`` iteration
    order leaks (ADR-0016's candidate-family machinery; ADR-0013's total
    order on candidate ties is untouched)."""

    def reach(ref: str) -> int:
        return _half_diagonal(_anchor_placement(board, board.placement(ref), ref))

    return tuple(sorted(scope, key=lambda ref: (-reach(ref), ref)))


def _rank(candidate: _Candidate) -> tuple[tuple[int, int], tuple[int, int, int]]:
    """The canonical candidate order: the ranking key first, then the
    Pose (ADR-0013's total order on ties, extended to rotation by
    ADR-0025 — knob off the third element is constant, so the order is
    exactly the pre-Pose one)."""
    return (
        candidate.key,
        (candidate.pose.x, candidate.pose.y, candidate.pose.rotation),
    )


def _half_diagonal(placement: Placement) -> int:
    """Half the diagonal of the placement's measure box — the circumscribed
    reach its courtyards clear in every direction (the courtyard-AABB /
    pad-bbox fallback posture)."""
    box = measure_box(placement)
    if box is None:
        return 0
    width = box.max.x - box.min.x
    height = box.max.y - box.min.y
    return isqrt(width * width + height * height) // 2


def _aabb_centre(points: list[Point]) -> Point:
    return Point(
        (min(point.x for point in points) + max(point.x for point in points)) // 2,
        (min(point.y for point in points) + max(point.y for point in points)) // 2,
    )


def _minus(a: Point, b: Point) -> Point:
    return Point(a.x - b.x, a.y - b.y)


_DIRECTIONS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)
"""The eight improvement directions — axis-aligned components, so diagonal
ring steps are exactly ``(±r, ±r)``, deterministic and integral."""


def _unmet_constraint_ids(board: Board) -> tuple[str, ...]:
    """The Requirements the Board does not satisfy: the ``constraint:<id>``
    leads of its error-severity findings, canonically sorted (ADR-0013's
    failure-as-value; ``run_drc`` stays the authority on what is wrong)."""
    return tuple(
        sorted(
            {
                finding.offending_refs[0][len("constraint:") :]
                for finding in run_drc(board)
                if finding.severity is Severity.ERROR
                and finding.offending_refs[0].startswith("constraint:")
            }
        )
    )
