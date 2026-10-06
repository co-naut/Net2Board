"""The private, per-call Evaluator — probe/commit, bit-for-bit (ADR-0016).

``solve``'s search and ``legalize``'s repair loop need legality and cost at
10³–10⁵ candidates per step; ``run_drc(board)`` and ``evaluate(board)`` are
full recompute. This module holds the incremental path: a flat, slot-indexed
working state built once from the input Board, serving both consumers under
the one delta vocabulary — set a ref's position (``None → point`` is the
same op; rotation and side are caller-fixed in M2, and a position is never
withdrawn, since a removal is not materialisable through
``with_placement``).

The contract is bit-for-bit: after every probe and commit, the Violations
tuple and the Objective equal ``run_drc`` / ``evaluate`` on the equivalent
Board. Nothing here redefines a measure — findings are produced by drc's own
finding builders (the shared bodies the checks and their locality companions
call), hinge terms by the objective's own ``_hinge``, the verdict order by
drc's own ``_sort_key``. The seeded-walk property test (``test_evaluator``)
pins all of it exactly.

The probe contract has two faces (ADR-0016, amended by #64): ``probe``
returns the full canonical verdict — the sorted findings tuple and the
assembled Objective — while ``probe_key`` returns the *counted* verdict, the
candidate's ``(error_count, total_nm)`` ranking key computed from the same
merged findings and term deltas without materializing any of it. The counted
verdict is the same legality notion, unmaterialized — never a second, weaker
check (ADR-0012): ``run_drc`` stays the authority, and ``solve``'s inner loop
reads only the count; the final ``legalize`` phase stays full-cost.

How locality is spent (ADR-0016):

- **Courtyard overlap** is handled natively over the flat arrays: a probe
  recomputes the moved slot's world courtyard and re-tests only the pairs
  involving it. Since #64's amendment the candidate pairs come from a
  **courtyard grid** — per-side cell buckets over the committed slots' yard
  AABBs, cell size derived from the board's own courtyard extents — instead
  of an O(n) scan: the query gathers the neighbourhood, a strict-AABB pass
  (conservative for ``shapes_overlap``'s strict-interior semantics) rejects
  the rest, and the exact predicate runs on the survivors.
  ``shapes_overlap``'s call count stays on the ``Probe`` as
  ``overlap_tests``, now density-bounded rather than linear. The check
  declares no companion because its locality lives here.
- **The five companioned checks** re-derive only the findings anchored to
  the moved ref, through the same finding builders the full recompute uses;
  intent (constraints, groups, EdgeMount exemptions) is read off the Board
  once and constant for the call.
- **A check with neither a fast path nor a companion** forces a full
  recompute of itself on a materialised candidate Board — absence is slow,
  never wrong, the protocol's structural guarantee.
- **The objective** recomputes the moved ref's incident NetSpans O(degree)
  over cached pad centres (the moved slot's centres recompute per probe,
  linear in its pad count) and the incident Preference hinges through
  ``_hinge``; every other term is carried. Criticality scales spans, moves
  nothing. A future ``TERMS`` kind is refused loudly at construction —
  the loud extension door.

``locked`` is carried through candidates verbatim: it is search policy
(ADR-0012), not evaluation state — no check or term reads it, so bit-for-bit
never depends on it. Evaluator instances are single-call objects; they hold
mutable working state and must not be shared across calls or threads.
"""

from __future__ import annotations

from dataclasses import dataclass

from net2board.drc import (
    CHECKS,
    COMPANIONS,
    Severity,
    Violation,
    _anchor_members,
    _canonical,
    _courtyard_outside_outline,
    _courtyard_overlap,
    _edge_mount_exemptions,
    _edge_mount_finding,
    _edge_mount_unmet,
    _keep_together_finding,
    _keep_together_unmet,
    _overhang_finding,
    _overlap_violation,
    _proximity_finding,
    _proximity_unmet,
    _region_finding,
    _region_unmet,
    _sort_key,
)
from net2board.geometry import (
    Point,
    Rectangle,
    Shape,
    shape_aabb,
    shapes_overlap,
    world_courtyard,
    world_point,
)
from net2board.model import (
    Board,
    Constraint,
    Criticality,
    EdgeMount,
    Footprint,
    KeepTogether,
    Placement,
    Preference,
    Proximity,
    Region,
    Requirement,
)
from net2board.objective import (
    TERMS,
    Objective,
    ObjectiveTerm,
    TermKind,
    TermStatus,
    _criticality_weights,
    _hinge,
    evaluate,
)

__all__: tuple[str, ...] = ()
"""Nothing is exported: the Evaluator never crosses a call or the public
API (ADR-0016). ``solve``, ``legalize``, and their results live one module
up — ``net2board.solver``'s public surface."""


@dataclass(frozen=True)
class Probe:
    """One non-mutating candidate read: the verdict and cost delta
    (ADR-0016). ``violations`` and ``objective`` are the exact full-recompute
    results the equivalent Board would yield; ``delta_nm`` is
    ``objective.total_nm - `` the current total. ``overlap_tests`` and
    ``span_recomputes`` count the probe's locality work — the exact
    ``shapes_overlap`` calls after the grid's coarse pass, and the NetSpan
    recomputes — operation counts for the complexity assertion, never wall
    clock."""

    violations: tuple[Violation, ...]
    objective: Objective
    delta_nm: int
    overlap_tests: int
    span_recomputes: int


@dataclass(frozen=True)
class _MovePlan:
    """Everything one position-set touches — probe reads it, commit writes
    it. ``changed_findings`` entries apply in order (drops precede adds per
    key); ``changed_terms`` keys always exist in the term store (one term
    per net, one per non-Criticality Preference, both total)."""

    slot: int
    candidate: Placement
    candidate_yard: Shape | None
    candidate_centers: tuple[Point, ...] | None
    changed_findings: tuple[tuple[int, tuple, Violation | None], ...]
    changed_terms: tuple[tuple[str, ObjectiveTerm], ...]
    preview_total: int
    overlap_tests: int
    span_recomputes: int


class _MoveView:
    """The placements as they stand with one candidate applied — zero-copy
    and lazy (ADR-0016, amended by #64): the finding builders and the
    objective's hinges read it through ``get``, so a probe whose moved ref
    anchors no constraint never materializes the map. The one value that
    differs from the committed state is the candidate itself."""

    __slots__ = ("_base", "_candidate", "_moved")

    def __init__(
        self, base: dict[str, Placement], moved: str, candidate: Placement
    ) -> None:
        self._base = base
        self._moved = moved
        self._candidate = candidate

    def get(self, ref: str, default: Placement | None = None) -> Placement | None:
        if ref == self._moved:
            return self._candidate
        return self._base.get(ref, default)


def _pad_index(footprint: Footprint) -> dict[str, int]:
    """First pad wins, matching ``pad_world_center``'s first-match scan."""
    index: dict[str, int] = {}
    for position, pad in enumerate(footprint.pads):
        index.setdefault(pad.number, position)
    return index


class Evaluator:
    """The flat, per-call evaluation state (ADR-0016): legality and cost
    under single-ref position moves, bit-for-bit with ``run_drc`` and
    ``evaluate``. Created once per ``solve``/``legalize`` call; never
    exported."""

    def __init__(self, board: Board) -> None:
        unsupported = [
            kind
            for kind in TERMS
            if kind not in (TermKind.NET_SPAN, TermKind.PREFERENCE)
        ]
        if unsupported:
            raise NotImplementedError(
                f"Evaluator computes only NET_SPAN and PREFERENCE terms; "
                f"TERMS gained {unsupported} — teach the Evaluator the new "
                f"kind (ADR-0014's registry is appendable, this refusal is "
                f"the extension door)"
            )
        self._board = board
        self._outline = board.outline
        self._exempt = _edge_mount_exemptions(board)
        self._criticality = _criticality_weights(board)
        # slots cover every component, placed or not; an unplaced slot is a
        # None position the delta vocabulary can fill (None → point)
        self._refs: list[str] = []
        self._slot_of: dict[str, int] = {}
        self._footprints: list[Footprint] = []
        for component in board.components:
            self._slot_of[component.ref] = len(self._refs)
            self._refs.append(component.ref)
            self._footprints.append(component.footprint)
        placements = {placement.ref: placement for placement in board.placements}
        self._slots: list[Placement | None] = [
            placements.get(ref) for ref in self._refs
        ]
        self._courtyards: list[Shape | None] = [
            world_courtyard(placement) if placement is not None else None
            for placement in self._slots
        ]
        # the courtyard grid (ADR-0016, amended by #64): per-side cell
        # buckets over committed slots' yard AABBs. Placed yard slots insert
        # here; commit — the only writer after that — moves a slot by
        # removing its old cells and inserting the new ones. Slots never
        # unplace and never change side in M2, so that is the whole
        # invalidation story.
        self._cell = self._grid_cell()
        self._cells: dict[str, dict[tuple[int, int], list[int]]] = {}
        self._slot_cells: list[tuple[tuple[int, int], ...] | None] = [None] * len(
            self._refs
        )
        self._slot_boxes: list[Rectangle | None] = [None] * len(self._refs)
        self._mark: list[int] = [0] * len(self._refs)
        self._generation = 0
        for slot, placement in enumerate(self._slots):
            if placement is not None and self._courtyards[slot] is not None:
                self._index_insert(slot, placement)
        # every net pin resolved to (slot, pad index) once — the per-lookup
        # pad scan dies here (ADR-0016). A pin of a ref outside the netlist
        # resolves to no slot: permanently unplaced, exactly as
        # evaluate() sees it. An unknown pad number raises here, the same
        # ValueError pad_world_center would raise mid-evaluation.
        self._centers: list[tuple[Point, ...] | None] = [None] * len(self._refs)
        pad_indexes = [_pad_index(footprint) for footprint in self._footprints]
        self._net_names: list[str] = []
        self._net_pins: list[tuple[tuple[int | None, int], ...]] = []
        slot_nets: list[list[int]] = [[] for _ in self._refs]
        for net_index, net in enumerate(board.nets):
            self._net_names.append(net.name)
            resolved: list[tuple[int | None, int]] = []
            for ref, pad_number in net.pins:
                slot = self._slot_of.get(ref)
                if slot is None:
                    resolved.append((None, -1))
                    continue
                index = pad_indexes[slot].get(pad_number)
                if index is None:
                    raise ValueError(
                        f"unknown pad {pad_number!r} on {ref!r}: footprint "
                        f"{self._footprints[slot].entry_name!r} has pads "
                        f"{tuple(pad.number for pad in self._footprints[slot].pads)}"
                    )
                resolved.append((slot, index))
                slot_nets[slot].append(net_index)
            self._net_pins.append(tuple(resolved))
        self._slot_nets: tuple[tuple[int, ...], ...] = tuple(
            tuple(dict.fromkeys(nets))  # a ref may hold two pads of one net
            for nets in slot_nets
        )
        for slot, placement in enumerate(self._slots):
            if placement is not None and self._slot_nets[slot]:
                self._centers[slot] = tuple(
                    world_point(pad.local_pos, placement)
                    for pad in placement.footprint.pads
                )
        self._placements: dict[str, Placement] = {
            ref: placement
            for ref, placement in zip(self._refs, self._slots)
            if placement is not None
        }
        # committed drift off the input Board, for the foreign-check paths
        # that materialise a candidate Board (empty for a never-committed
        # Evaluator, and empty again whenever commits return to the base
        # state)
        self._base_placements = placements
        self._drift: dict[str, Placement] = {}
        # constraint incidence over expanded anchors; Criticality is
        # net-based and moves nothing
        self._constraints: tuple[Constraint, ...] = board.constraints
        slot_constraints: list[list[int]] = [[] for _ in self._refs]
        for index, constraint in enumerate(self._constraints):
            relation = constraint.relation
            if isinstance(relation, Criticality):
                continue
            if isinstance(relation, Proximity):
                members = (relation.ref_a, relation.ref_b)
            else:
                members = _anchor_members(board, relation.target)
            for ref in members:
                slot = self._slot_of.get(ref)
                if slot is not None:
                    slot_constraints[slot].append(index)
        self._slot_constraints: tuple[tuple[int, ...], ...] = tuple(
            tuple(anchors) for anchors in slot_constraints
        )
        # check positions + keyed finding storage, one dict per check
        self._overlap_index = self._index_of(_courtyard_overlap)
        self._containment_index = self._index_of(_courtyard_outside_outline)
        self._proximity_index = self._index_of(_proximity_unmet)
        self._region_index = self._index_of(_region_unmet)
        self._edge_mount_index = self._index_of(_edge_mount_unmet)
        self._keep_together_index = self._index_of(_keep_together_unmet)
        self._handled: frozenset[int] = frozenset(
            index
            for index in (
                self._overlap_index,
                self._containment_index,
                self._proximity_index,
                self._region_index,
                self._edge_mount_index,
                self._keep_together_index,
            )
            if index is not None
        )
        self._findings: list[dict[tuple, Violation | None]] = [{} for _ in CHECKS]
        for index, check in enumerate(CHECKS):
            for raw in check(board):
                violation = _canonical(raw, board)
                self._findings[index][self._key_of(violation)] = violation
        # the running ERROR count over the findings — the counted verdict's
        # legality half, maintained by commit (#64)
        self._errors: int = sum(
            1
            for stored in self._findings
            for violation in stored.values()
            if violation is not None and violation.severity is Severity.ERROR
        )
        # objective state — the one full evaluation at entry; ``legalize``'s
        # identity-on-legal reads legality off the same warm state
        objective = evaluate(board)
        self._terms: dict[str, ObjectiveTerm] = {
            term.ref: term for term in objective.terms
        }
        self._total: int = objective.total_nm

    def probe(self, ref: str, pos: Point) -> Probe:
        """The candidate verdict and cost delta, without mutating state
        (ADR-0016): rejections — the common case — cost no bookkeeping."""
        plan = self._plan(ref, pos)
        changed_by_check: dict[int, dict[tuple, Violation | None]] = {}
        for index, key, violation in plan.changed_findings:
            changed_by_check.setdefault(index, {})[key] = violation
        findings: list[Violation] = []
        for index, stored in enumerate(self._findings):
            if index in changed_by_check:
                merged = dict(stored)
                merged.update(changed_by_check[index])
                stored = merged
            findings.extend(
                violation for violation in stored.values() if violation is not None
            )
        terms = dict(self._terms)
        for term_ref, term in plan.changed_terms:
            terms[term_ref] = term
        return Probe(
            violations=tuple(sorted(findings, key=_sort_key)),
            objective=self._objective_of(terms, plan.preview_total),
            delta_nm=plan.preview_total - self._total,
            overlap_tests=plan.overlap_tests,
            span_recomputes=plan.span_recomputes,
        )

    def probe_key(self, ref: str, pos: Point) -> tuple[int, int]:
        """The counted verdict (ADR-0016, amended by #64): the candidate's
        ranking key ``(error_count, total_nm)`` — the same legality notion
        the full probe reports, computed from the same merged findings and
        term deltas, but never materialized: no sorted findings tuple, no
        term-store copy, no Objective assembly. ``solve``'s inner loop
        reads only this; ``run_drc`` stays the authority, and the final
        ``legalize`` phase stays full-cost."""
        plan = self._plan(ref, pos)
        return (
            self._count_errors(self._errors, plan.changed_findings),
            plan.preview_total,
        )

    def commit(self, ref: str, pos: Point) -> None:
        """Set ``ref``'s position — the sole mutation, one slot at a time
        (ADR-0016). Unknown refs raise ``ValueError``."""
        plan = self._plan(ref, pos)
        slot = plan.slot
        self._errors = self._count_errors(self._errors, plan.changed_findings)
        # the grid's remove half reads the slot's *old* buckets — it runs
        # before the placement swaps, so the invalidation reads no state it
        # is about to overwrite
        if self._slot_cells[slot] is not None:
            self._index_remove(slot)
        self._slots[slot] = plan.candidate
        self._courtyards[slot] = plan.candidate_yard
        if plan.candidate_yard is not None:
            self._index_insert(slot, plan.candidate)
        self._centers[slot] = plan.candidate_centers
        self._placements[ref] = plan.candidate
        base = self._base_placements.get(ref)
        if base is not None and base == plan.candidate:
            self._drift.pop(ref, None)
        else:
            self._drift[ref] = plan.candidate
        for index, key, violation in plan.changed_findings:
            self._findings[index][key] = violation
        for term_ref, term in plan.changed_terms:
            self._terms[term_ref] = term
        self._total = plan.preview_total

    def violations(self) -> tuple[Violation, ...]:
        """The current findings exactly as ``run_drc`` would return them."""
        return tuple(
            sorted(
                (
                    violation
                    for stored in self._findings
                    for violation in stored.values()
                    if violation is not None
                ),
                key=_sort_key,
            )
        )

    def objective(self) -> Objective:
        """The current objective exactly as ``evaluate`` would return it."""
        return self._objective_of(self._terms, self._total)

    def current_key(self) -> tuple[int, int]:
        """The current board's ranking key — the error count and total the
        full readings report, maintained by ``commit`` (#64)."""
        return self._errors, self._total

    def _plan(self, ref: str, pos: Point) -> _MovePlan:
        slot = self._slot_of.get(ref)
        if slot is None:
            raise ValueError(
                f"unknown ref {ref!r}: not a component of this board's netlist"
            )
        current = self._slots[slot]
        candidate = Placement(
            ref=ref,
            footprint=self._footprints[slot],
            pos=pos,
            rotation=current.rotation if current is not None else 0,
            side=current.side if current is not None else "F.Cu",
            locked=current.locked if current is not None else False,
        )
        view = _MoveView(self._placements, ref, candidate)
        yard = world_courtyard(candidate)
        centers = (
            tuple(
                world_point(pad.local_pos, candidate)
                for pad in candidate.footprint.pads
            )
            if self._slot_nets[slot]
            else None
        )
        changed: list[tuple[int, tuple, Violation | None]] = []
        overlap_tests = self._overlap_changed(slot, ref, candidate, yard, changed)
        self._constraint_changed(slot, ref, candidate, view, changed)
        self._foreign_changed(ref, candidate, changed)
        changed_terms = self._objective_changed(slot, candidate, view, centers)
        preview_total = self._total
        for term_ref, term in changed_terms:
            preview_total += term.weighted_nm - self._terms[term_ref].weighted_nm
        return _MovePlan(
            slot=slot,
            candidate=candidate,
            candidate_yard=yard,
            candidate_centers=centers,
            changed_findings=tuple(changed),
            changed_terms=tuple(changed_terms),
            preview_total=preview_total,
            overlap_tests=overlap_tests,
            span_recomputes=len(self._slot_nets[slot]),
        )

    def _grid_cell(self) -> int:
        """The grid's cell size: the upper-middle courtyard extent over
        courtyard-bearing footprints (the sorted middle — a deterministic
        order statistic), floored at 1 nm — a board-derived constant,
        independent of where anything sits (90° rotations only swap a
        rectangle's axes, so the footprint-local extent is the world
        extent). Never so small that the largest courtyard spans more than
        ~64 cells per axis: the few oversized parts pay only their own
        inserts and queries (#64)."""
        extents: list[int] = []
        for footprint in self._footprints:
            if footprint.courtyard is None:
                continue
            box = shape_aabb(footprint.courtyard)
            extents.append(box.max.x - box.min.x)
            extents.append(box.max.y - box.min.y)
        if not extents:
            return 1
        median = sorted(extents)[len(extents) // 2]
        largest = max(extents)
        return max(1, median, -(-largest // 64))

    def _index_insert(self, slot: int, placement: Placement) -> None:
        """Bucket a committed slot into every cell its yard AABB covers."""
        box = shape_aabb(self._courtyards[slot])
        cells = self._cells.setdefault(placement.side, {})
        keys = tuple(
            (cx, cy)
            for cx in range(box.min.x // self._cell, box.max.x // self._cell + 1)
            for cy in range(box.min.y // self._cell, box.max.y // self._cell + 1)
        )
        for key in keys:
            bucket = cells.setdefault(key, [])
            bucket.append(slot)
        self._slot_cells[slot] = keys
        self._slot_boxes[slot] = box

    def _index_remove(self, slot: int) -> None:
        """Drop a slot's old buckets — the invalidation half of commit."""
        keys = self._slot_cells[slot]
        if keys is None:
            return
        cells = self._cells[self._slots[slot].side]
        for key in keys:
            bucket = cells[key]
            bucket.remove(slot)
            if not bucket:
                del cells[key]
        self._slot_cells[slot] = None
        self._slot_boxes[slot] = None

    def _grid_query(self, box: Rectangle, side: str) -> list[int]:
        """The candidate slots whose cells the box covers, each once —
        cells in row-major order, a generation mark deduplicating the slots
        that straddle cell boundaries. The gathered set is a pure function
        of the committed positions, so the probe's work is deterministic
        (ADR-0013: no iteration order leaks — the findings normalize on the
        canonical sort and the test count is order-free). The mark and
        generation are probe scratch — no committed state changes, so
        "probe reads; commit is the only writer" (ADR-0016) holds where it
        matters: on the board's state."""
        self._generation += 1
        generation = self._generation
        cells = self._cells.get(side)
        gathered: list[int] = []
        if not cells:
            return gathered
        for cx in range(box.min.x // self._cell, box.max.x // self._cell + 1):
            for cy in range(box.min.y // self._cell, box.max.y // self._cell + 1):
                bucket = cells.get((cx, cy))
                if bucket is None:
                    continue
                for other_slot in bucket:
                    if self._mark[other_slot] == generation:
                        continue
                    self._mark[other_slot] = generation
                    gathered.append(other_slot)
        return gathered

    def _overlap_changed(
        self,
        slot: int,
        ref: str,
        candidate: Placement,
        yard: Shape | None,
        changed: list[tuple[int, tuple, Violation | None]],
    ) -> int:
        """The native overlap delta: drop the moved ref's pair findings,
        then re-test it against the grid's gathered neighbourhood — the
        cell query, a strict-AABB pass (a pair failing it cannot pass
        ``shapes_overlap``'s strict interior), and the exact predicate on
        the survivors (ADR-0016, amended by #64). Returns the number of
        ``shapes_overlap`` tests."""
        index = self._overlap_index
        if index is None:
            return 0
        self._drop_ref_findings(index, ref, changed)
        if yard is None:
            return 0
        box = shape_aabb(yard)
        tests = 0
        for other_slot in self._grid_query(box, candidate.side):
            if other_slot == slot:
                continue
            other_box = self._slot_boxes[other_slot]
            if (
                other_box.min.x >= box.max.x
                or box.min.x >= other_box.max.x
                or other_box.min.y >= box.max.y
                or box.min.y >= other_box.max.y
            ):
                continue
            other = self._slots[other_slot]
            tests += 1
            if shapes_overlap(yard, self._courtyards[other_slot]):
                changed.append(
                    (
                        index,
                        (None, tuple(sorted((ref, other.ref)))),
                        _overlap_violation(candidate, other, self._board),
                    )
                )
        return tests

    def _constraint_changed(
        self,
        slot: int,
        ref: str,
        candidate: Placement,
        view: _MoveView,
        changed: list[tuple[int, tuple, Violation | None]],
    ) -> None:
        """Re-derive the containment finding and every constraint finding
        anchored to the moved ref, through the finding builders the full
        recompute path itself calls."""
        containment = self._containment_index
        if containment is not None:
            changed.append(
                (
                    containment,
                    (None, (ref,)),
                    _overhang_finding(
                        candidate, self._outline, self._exempt, self._board
                    ),
                )
            )
        for constraint_index in self._slot_constraints[slot]:
            constraint = self._constraints[constraint_index]
            if not isinstance(constraint.kind, Requirement):
                continue  # Preferences never produce Violations (ADR-0011)
            relation = constraint.relation
            match relation:
                case Proximity():
                    if self._proximity_index is not None:
                        changed.append(
                            (
                                self._proximity_index,
                                (
                                    constraint.id,
                                    tuple(sorted((relation.ref_a, relation.ref_b))),
                                ),
                                _proximity_finding(constraint, view, self._board),
                            )
                        )
                case Region():
                    if self._region_index is not None:
                        changed.append(
                            (
                                self._region_index,
                                (constraint.id, (ref,)),
                                _region_finding(constraint, ref, view, self._board),
                            )
                        )
                case EdgeMount():
                    if self._edge_mount_index is not None:
                        changed.append(
                            (
                                self._edge_mount_index,
                                (constraint.id, (ref,)),
                                _edge_mount_finding(
                                    constraint, relation, candidate, self._board
                                ),
                            )
                        )
                case KeepTogether():
                    if self._keep_together_index is not None:
                        members = tuple(
                            sorted(_anchor_members(self._board, relation.target))
                        )
                        changed.append(
                            (
                                self._keep_together_index,
                                (constraint.id, members),
                                _keep_together_finding(constraint, view, self._board),
                            )
                        )

    def _foreign_changed(
        self,
        ref: str,
        candidate: Placement,
        changed: list[tuple[int, tuple, Violation | None]],
    ) -> None:
        """Checks outside the Evaluator's fast paths: companion-driven
        delta where a companion is declared, full recompute of the check
        otherwise — absence is slow, never wrong (ADR-0016). The candidate
        Board is materialised through the public construction path — the
        input Board with the evaluator's committed drift and the candidate
        move applied — and only when a foreign check actually asks."""
        candidate_board: Board | None = None

        def materialise() -> Board:
            board = self._board
            for drifted_ref, drifted in self._drift.items():
                if drifted_ref == ref:
                    continue
                board = board.with_placement(
                    drifted.ref,
                    drifted.pos.x,
                    drifted.pos.y,
                    drifted.rotation,
                    drifted.side,
                    drifted.locked,
                )
            return board.with_placement(
                ref,
                candidate.pos.x,
                candidate.pos.y,
                candidate.rotation,
                candidate.side,
                candidate.locked,
            )

        for index, check in enumerate(CHECKS):
            if index in self._handled:
                continue
            if candidate_board is None:
                candidate_board = materialise()
            companion = COMPANIONS.get(check)
            if companion is not None:
                self._drop_ref_findings(index, ref, changed)
                for raw in companion(candidate_board, ref):
                    violation = _canonical(raw, candidate_board)
                    changed.append((index, self._key_of(violation), violation))
            else:
                for key in self._findings[index]:
                    changed.append((index, key, None))
                for raw in check(candidate_board):
                    violation = _canonical(raw, candidate_board)
                    changed.append((index, self._key_of(violation), violation))

    def _objective_changed(
        self,
        slot: int,
        candidate: Placement,
        view: _MoveView,
        centers: tuple[Point, ...] | None,
    ) -> list[tuple[str, ObjectiveTerm]]:
        """The moved ref's incident NetSpans (O(degree) recompute over
        cached pad centres) and incident Preference hinges (``_hinge``, the
        objective's own body). Everything else is carried."""
        changed: list[tuple[str, ObjectiveTerm]] = []
        for net_index in self._slot_nets[slot]:
            changed.append(
                (
                    f"net:{self._net_names[net_index]}",
                    self._net_span_term(net_index, slot, centers),
                )
            )
        for constraint_index in self._slot_constraints[slot]:
            constraint = self._constraints[constraint_index]
            if not isinstance(constraint.kind, Preference):
                continue
            if isinstance(constraint.relation, Criticality):
                continue
            raw, status = _hinge(constraint.relation, view, self._board)
            term_ref = f"constraint:{constraint.id}"
            changed.append(
                (
                    term_ref,
                    ObjectiveTerm(
                        kind=TermKind.PREFERENCE,
                        ref=term_ref,
                        weight=constraint.weight,
                        raw_nm=raw,
                        weighted_nm=constraint.weight * raw,
                        status=status,
                    ),
                )
            )
        return changed

    def _net_span_term(
        self, net_index: int, moved_slot: int, moved_centers: tuple[Point, ...] | None
    ) -> ObjectiveTerm:
        """One net's term, recomputed from scratch — the mirror of the
        objective's per-net body over cached centres (ADR-0016: recompute,
        never maintain). The moved slot reads fresh centres; every other
        placed pin reads its cache; an unplaced pin marks the net PARTIAL.
        """
        points = []
        unplaced = False
        for slot, pad_index in self._net_pins[net_index]:
            if slot is None:
                unplaced = True
                continue
            if slot == moved_slot:
                points.append(moved_centers[pad_index])
                continue
            cached = self._centers[slot]
            if cached is None:
                unplaced = True
                continue
            points.append(cached[pad_index])
        if len(points) <= 1:
            span = 0
        else:
            xs = [point.x for point in points]
            ys = [point.y for point in points]
            span = (max(xs) - min(xs)) + (max(ys) - min(ys))
        name = self._net_names[net_index]
        weight = self._criticality.get(name, 1)
        return ObjectiveTerm(
            kind=TermKind.NET_SPAN,
            ref=f"net:{name}",
            weight=weight,
            raw_nm=span,
            weighted_nm=weight * span,
            status=TermStatus.PARTIAL if unplaced else TermStatus.ACTIVE,
        )

    @staticmethod
    def _index_of(check) -> int | None:
        return next(
            (index for index, registered in enumerate(CHECKS) if registered is check),
            None,
        )

    def _count_errors(
        self, base: int, changed: tuple[tuple[int, tuple, Violation | None], ...]
    ) -> int:
        """The ERROR count over the merged findings — the identical walk
        over the identical delta the full assembly would apply, shared by
        the counted verdict and ``commit``'s bookkeeping (#64). Entries
        apply in order (drops precede adds per key), so an already-touched
        key reads its applied value, not the store's."""
        errors = base
        applied: dict[tuple[int, tuple], Violation | None] = {}
        for index, key, violation in changed:
            entry = (index, key)
            if entry in applied:
                stored = applied[entry]
            else:
                stored = self._findings[index].get(key)
            if stored is not None and stored.severity is Severity.ERROR:
                errors -= 1
            if violation is not None and violation.severity is Severity.ERROR:
                errors += 1
            applied[entry] = violation
        return errors

    @staticmethod
    def _objective_of(terms: dict[str, ObjectiveTerm], total_nm: int) -> Objective:
        """The canonical Objective over a term store — evaluate's sort and
        sum, the one assembly both ``objective()`` and every probe use."""
        return Objective(
            total_nm=total_nm,
            terms=tuple(
                sorted(
                    terms.values(),
                    key=lambda term: (term.kind.value, term.ref),
                )
            ),
        )

    def _drop_ref_findings(
        self, index: int, ref: str, changed: list[tuple[int, tuple, Violation | None]]
    ) -> None:
        """Queue the removal of check ``index``'s findings whose placement
        refs contain the moved ref — the Locality axiom's drop half
        (ADR-0016), shared by the overlap scan and the foreign companions."""
        for key, stored in self._findings[index].items():
            if stored is not None and ref in key[1]:
                changed.append((index, key, None))

    @staticmethod
    def _key_of(violation: Violation) -> tuple[str | None, tuple[str, ...]]:
        """A finding's storage key: the leading ``constraint:<id>`` when
        present (ADR-0009's variable-arity convention), then the bare
        placement refs in canonical order."""
        refs = violation.offending_refs
        if refs[0].startswith("constraint:"):
            return (
                refs[0][len("constraint:") :],
                tuple(ref[len("placement:") :] for ref in refs[1:]),
            )
        return (None, tuple(ref[len("placement:") :] for ref in refs))
