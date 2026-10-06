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
  then per-net AABB centres. A candidate is probed before it is trusted; a
  ref no candidate satisfies falls to a coarse first-fit scan of the
  outline, and one that finds no legal position anywhere stays unplaced —
  failure-as-value, visible via ``board.unplaced_refs()``.
- **Improve** — rounds over the in-scope movers, biggest first; each
  mover scans the same wirelength family plus a small local ring, and
  commits the best candidate whose key ``(error_count, total_nm)`` strictly
  improves on the current board. The key's legality-first ordering is
  ADR-0014's split made operational: a legal board only ever accepts legal
  candidates (the Objective ranks Requirement-legal boards), while an
  illegal one climbs the overlap/clearance gradient — the error-count term —
  toward the legal set. A full round with zero commits is a local optimum:
  ``NO_FURTHER_IMPROVEMENT``. Probes running out first is
  ``BUDGET_EXHAUSTED``.
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
  value: ``(error_count, total_nm)`` first, then ``(x, y)`` in integer nm;
  movers are visited reach-descending — the board-derived canonical order,
  half-diagonal first, lexicographic ref breaking ties (#52). No
  ``dict``/``set`` iteration order leaks into a result.
- **O(1) snapshot coexistence** — the search works on the flat Evaluator
  and a position map, holding no Board snapshots beyond the entry board and
  the one materialization at the end; no beam.

Nothing is observable mid-solve: no callback, no cancellation, no trajectory
— the budget is the only control surface and ``stop_reason`` the only
feedback.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from math import isqrt

from net2board.drc import Severity, run_drc
from net2board.errors import Net2BoardError
from net2board.geometry import (
    Point,
    Rectangle,
    pad_world_bbox,
    pad_world_center,
    shape_aabb,
    world_courtyard,
)
from net2board.model import Board, Net, Placement
from net2board.solver._evaluator import Evaluator
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
    full scan of every in-scope mover found no accepted candidate, a local
    optimum without claiming a global one — or ``BUDGET_EXHAUSTED``, the
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
    sorted ids of Requirements the returned board does not satisfy, and the
    stop reason. No success boolean (``unmet_constraint_ids == ()`` plus a
    legal board *is* it), no carried Violations, no cost reading, no
    displacement report."""

    board: Board
    unmet_constraint_ids: tuple[str, ...]
    stop_reason: StopReason


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
    board: Board, refs: Iterable[str] | None = None, *, budget: int
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
    """
    checked_budget = _checked_budget(budget)
    scope = _checked_scope(board, refs)
    search = _Search(board, scope, checked_budget)
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
    """One probed candidate: its position and the probe's ranking key
    ``(error_count, total_nm)`` — legality first, wirelength second
    (ADR-0014's split made operational)."""

    pos: Point
    errors: int
    total_nm: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.errors, self.total_nm)


class _Search:
    """One solve call's working state: the warm Evaluator, the position
    map, the live placements map, the probe ledger, and the canonical
    iteration orders."""

    def __init__(self, board: Board, scope: tuple[str, ...], budget: int):
        self._board = board
        self._scope = _visit_order(board, scope)
        self._budget = budget
        self._evaluator = Evaluator(board)
        self._placements = {placement.ref: placement for placement in board.placements}
        self._positions: dict[str, Point | None] = {
            component.ref: (
                self._placements[component.ref].pos
                if component.ref in self._placements
                else None
            )
            for component in board.components
        }
        self._originals: dict[str, Point | None] = dict(self._positions)
        self._spent = 0
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
            if self._positions[ref] is not None:
                continue
            if self._place_one(ref):
                return True
        return False

    def _place_one(self, ref: str) -> bool:
        """Land one unplaced ref at the best legal wirelength candidate,
        falling back to the first-fit outline scan. True when the budget
        ran out before the ref could be decided."""
        best: _Candidate | None = None
        for pos in self._placement_candidates(ref):
            if self._spent >= self._budget:
                return True
            probed = self._probe(ref, pos)
            if probed.errors == 0 and (best is None or _rank(probed) < _rank(best)):
                best = probed
        if best is None:
            for pos in self._grid_candidates():
                if self._spent >= self._budget:
                    return True
                probed = self._probe(ref, pos)
                if probed.errors == 0:
                    best = probed
                    break
        if best is not None:
            self._commit(ref, best)
        return False

    def _placement_candidates(self, ref: str) -> tuple[Point, ...]:
        """The wirelength-driven placement family, probed in order:
        pad-alignment onto each placed netmate — flush, then a ring of
        courtyard-clearing offsets — then the per-net AABB centre."""
        origin = dataclasses.replace(
            _anchor_placement(self._board, self._placements, ref), pos=ORIGIN
        )
        reach = _half_diagonal(origin)
        candidates: dict[tuple[int, int], Point] = {}
        for net in self._nets_by_ref.get(ref, ()):
            own_pads = [pad for mate, pad in net.pins if mate == ref]
            if not own_pads:
                continue
            offset = pad_world_center(origin, own_pads[0])
            anchors: list[tuple[Point, int]] = []
            for mate, pad in net.pins:
                if mate == ref:
                    continue
                mate_placement = self._placements.get(mate)
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
            for anchor, clearance in anchors:
                flush = _minus(anchor, offset)
                candidates.setdefault((flush.x, flush.y), flush)
                for direction in _DIRECTIONS:
                    span = reach + clearance + _PLACEMENT_MARGIN_NM
                    pos = Point(
                        anchor.x + direction[0] * span - offset.x,
                        anchor.y + direction[1] * span - offset.y,
                    )
                    candidates.setdefault((pos.x, pos.y), pos)
            centre = _minus(_aabb_centre([anchor for anchor, _ in anchors]), offset)
            candidates.setdefault((centre.x, centre.y), centre)
        return tuple(candidates.values())

    def _grid_candidates(self) -> tuple[Point, ...]:
        """The deterministic first-fit scan: row-major over the outline,
        inset, at ``_GRID_PITCH_NM``."""
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
        return tuple(Point(x, y) for y in ys for x in xs)

    # Phase B — improvement -----------------------------------------------

    def _improve(self) -> StopReason:
        """Rounds over the in-scope movers, biggest first (#52); each mover
        commits its best strictly-improving candidate. A full round with
        zero commits is the local optimum."""
        while True:
            if self._spent >= self._budget:
                return StopReason.BUDGET_EXHAUSTED
            committed = False
            for mover in self._scope:
                if self._spent >= self._budget:
                    return StopReason.BUDGET_EXHAUSTED
                best = self._best_move(mover)
                if best is not None:
                    self._commit(mover, best)
                    committed = True
            if not committed:
                return StopReason.NO_FURTHER_IMPROVEMENT

    def _best_move(self, mover: str) -> _Candidate | None:
        """The mover's best candidate by ``(key, pos)`` — the canonical
        total order — or ``None`` when nothing beats staying put."""
        best: _Candidate | None = None
        for pos in self._move_candidates(mover):
            if self._spent >= self._budget:
                break
            probed = self._probe(mover, pos)
            if best is None or _rank(probed) < _rank(best):
                best = probed
        if best is None or best.key >= self._current_key():
            return None
        return best

    def _move_candidates(self, mover: str) -> tuple[Point, ...]:
        """The improvement family: the wirelength placements (pad
        alignments, net centres), then the local ring around the current
        position — never the position itself."""
        candidates: dict[tuple[int, int], Point] = {}
        for pos in self._placement_candidates(mover):
            candidates.setdefault((pos.x, pos.y), pos)
        current = self._positions[mover]
        if current is not None:
            for radius in _RING_RADII_NM:
                for direction in _DIRECTIONS:
                    pos = Point(
                        current.x + direction[0] * radius,
                        current.y + direction[1] * radius,
                    )
                    candidates.setdefault((pos.x, pos.y), pos)
            candidates.pop((current.x, current.y), None)
        return tuple(candidates.values())

    # Shared machinery ----------------------------------------------------

    def _probe(self, ref: str, pos: Point) -> _Candidate:
        """One candidate evaluated — the budgeted unit (ADR-0016): counted
        whether accepted or rejected. The search reads the probe's counted
        verdict — the ranking key the full probe reports, not materialized
        (ADR-0016, amended by #64); the final ``legalize`` phase stays
        full-cost."""
        self._spent += 1
        errors, total_nm = self._evaluator.probe_key(ref, pos)
        return _Candidate(pos=pos, errors=errors, total_nm=total_nm)

    def _commit(self, ref: str, candidate: _Candidate) -> None:
        """Land a probed candidate: the Evaluator, the position map, and
        the placements map all move together (#63) — the placements map
        is what the candidate families read for pad-alignment anchors,
        so a stale entry blinds every later family to this netmate."""
        self._evaluator.commit(ref, candidate.pos)
        self._positions[ref] = candidate.pos
        self._placements[ref] = dataclasses.replace(
            _anchor_placement(self._board, self._placements, ref), pos=candidate.pos
        )

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
            pos = self._positions[ref]
            if pos is None or pos == self._originals[ref]:
                continue
            anchor = _anchor_placement(self._board, self._placements, ref)
            board = board.with_placement(
                ref, pos.x, pos.y, anchor.rotation, anchor.side, anchor.locked
            )
        finalized = legalize(board).board
        return SolveResult(
            board=finalized,
            unmet_constraint_ids=_unmet_constraint_ids(finalized),
            stop_reason=stop_reason,
        )


def _anchor_placement(
    board: Board, placements: dict[str, Placement], ref: str
) -> Placement:
    """The ref's placement as it stands — an unplaced ref anchors at
    rotation 0 on ``F.Cu``, exactly the Evaluator's ``None → point``
    semantics (ADR-0016: rotation and side are caller-fixed in M2)."""
    placement = placements.get(ref)
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
    placements = {placement.ref: placement for placement in board.placements}

    def reach(ref: str) -> int:
        return _half_diagonal(_anchor_placement(board, placements, ref))

    return tuple(sorted(scope, key=lambda ref: (-reach(ref), ref)))


def _rank(candidate: _Candidate) -> tuple[tuple[int, int], tuple[int, int]]:
    """The canonical candidate order: the ranking key first, then the
    position (ADR-0013's total order on ties)."""
    return (candidate.key, (candidate.pos.x, candidate.pos.y))


def _half_diagonal(placement: Placement) -> int:
    """Half the diagonal of the placement's measure box — the circumscribed
    reach its courtyards clear in every direction (the courtyard-AABB /
    pad-bbox fallback posture)."""
    box = _measure_box(placement)
    if box is None:
        return 0
    width = box.max.x - box.min.x
    height = box.max.y - box.min.y
    return isqrt(width * width + height * height) // 2


def _measure_box(placement: Placement) -> Rectangle | None:
    """The box a part occupies for clearance arithmetic — the courtyard's
    AABB, else the pad bounding box (ADR-0011's fallback), else ``None``."""
    yard = world_courtyard(placement)
    if yard is not None:
        return shape_aabb(yard)
    try:
        return pad_world_bbox(placement)
    except ValueError:
        return None


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
