"""The private, per-call Evaluator — probe/commit, bit-for-bit (ADR-0016).

``solve``'s search and ``legalize``'s repair loop need legality and cost at
10³–10⁵ candidates per step; ``run_drc(board)`` and ``evaluate(board)`` are
full recompute. This module holds the incremental path: a flat, slot-indexed
working state built once from the input Board, serving both consumers under
the one delta vocabulary — set a ref's Pose (ADR-0025; knob off every caller
passes the standing rotation, so the op behaves as a position-set, the side
is caller-fixed, and a position is never withdrawn, since a removal is not
materialisable through ``with_placement``).

The contract is bit-for-bit: after every probe and commit, the Violations
tuple and the Objective equal ``run_drc`` / ``evaluate`` on the equivalent
Board. Nothing here redefines a measure — findings are produced by drc's own
finding builders (the shared bodies the checks and their locality companions
call), hinge terms by the objective's own ``hinge``, the verdict order by
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
  of an O(n) pass: the query gathers the neighbourhood, a strict-AABB pass
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
- **The objective** reads the moved ref's incident NetSpans off per-net
  span records — per direction the best value, its slot, and the best
  value from any other slot — O(1) per incident net (#113); ``commit``
  rebuilds the touched nets' records and is the only writer (ADR-0016,
  amended). The moved slot's centres recompute per probe, linear in its
  pad count, and the incident Preference hinges run through ``hinge``;
  every other term is carried. Criticality scales spans, moves
  nothing. A future ``TERMS`` kind is refused loudly at construction —
  the loud extension door.

``locked`` is carried through candidates verbatim: it is search policy
(ADR-0012), not evaluation state — no check or term reads it, so bit-for-bit
never depends on it. Evaluator instances are single-call objects; they hold
mutable working state and must not be shared across calls or threads.
"""

from __future__ import annotations

import operator
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import chain
from typing import NamedTuple

from net2board import drc
from net2board.drc import (
    Severity,
    Violation,
    _anchored_copper,
    _canonical,
    _courtyard_outside_outline,
    _courtyard_overlap,
    _edge_mount_exemptions,
    _edge_mount_finding,
    _edge_mount_unmet,
    _keep_together_finding,
    _keep_together_unmet,
    _net_map,
    _overhang_finding,
    _overlap_violation,
    _pad_coppers,
    _pad_edge_record,
    _pad_pair_exempt,
    _pad_pair_price,
    _pad_pair_record,
    _pad_pair_records,
    _PadCopper,
    _proximity_finding,
    _proximity_unmet,
    _region_finding,
    _region_unmet,
    _shifted,
    _sort_key,
)
from net2board.geometry import (
    Point,
    Rectangle,
    Shape,
    _apart,
    _clear_of_outline,
    _grown,
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
from net2board.model.constraints import constraint_ref, net_ref
from net2board.objective import (
    TERMS,
    Objective,
    ObjectiveTerm,
    TermKind,
    TermStatus,
    criticality_weights,
    evaluate,
    hinge,
)

__all__: tuple[str, ...] = ()
"""Nothing is exported: the Evaluator never crosses a call or the public
API (ADR-0016). ``solve``, ``legalize``, and their results live one module
up — ``net2board.solver``'s public surface."""


class Pose(NamedTuple):
    """The one operation's argument (ADR-0025): where a ref stands —
    position and rotation as one immutable value. Search-internal: the
    public API keeps speaking ``Point`` and full ``Placement``s, and knob
    off every Pose carries its ref's standing rotation."""

    x: int
    y: int
    rotation: int

    @classmethod
    def of(cls, placement: Placement) -> Pose:
        """A placement read back as the one operation's argument — the
        committed state in Pose form."""
        return cls(placement.pos.x, placement.pos.y, placement.rotation)


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
    pad_pair_tests: int


@dataclass(frozen=True)
class _MovePlan:
    """Everything one Pose-set touches — probe reads it, commit writes
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
    pad_pair_tests: int


class _MoveView(Mapping):
    """The placements as they stand with one candidate applied — zero-copy
    and lazy (ADR-0016, amended by #64): a real ``Mapping`` over the base
    with the candidate substituted (#66 — the widened builder signatures
    promise ``Mapping``, and the Evaluator's own view keeps that promise).
    The one value that differs from the committed state is the candidate
    itself."""

    __slots__ = ("_base", "_candidate", "_moved")

    def __init__(
        self, base: dict[str, Placement], moved: str, candidate: Placement
    ) -> None:
        self._base = base
        self._moved = moved
        self._candidate = candidate

    def __getitem__(self, ref: str) -> Placement:
        if ref == self._moved:
            return self._candidate
        return self._base[ref]

    def __iter__(self) -> Iterator[str]:
        if self._moved in self._base:
            return iter(self._base)
        return chain(self._base, (self._moved,))

    def __len__(self) -> int:
        return len(self._base) + (self._moved not in self._base)


def _pad_index(footprint: Footprint) -> dict[str, int]:
    """First pad wins, matching ``pad_world_center``'s first-match rule."""
    index: dict[str, int] = {}
    for position, pad in enumerate(footprint.pads):
        index.setdefault(pad.number, position)
    return index


class _Extreme(NamedTuple):
    """One span direction's top values from distinct slots (#113):
    ``value`` and the ``slot`` holding it, plus the best ``other`` value
    and its slot. The runner-up comes from a different slot by
    construction, so excluding one slot reads ``other`` when it holds the
    best and ``value`` otherwise — the whole trick that makes the
    sliding-window-minimum machinery ADR-0016 rejected unnecessary."""

    value: int | None
    slot: int | None
    other: int | None
    other_slot: int | None


class _SpanRecord(NamedTuple):
    """One net's maintained span state (#113): four direction extremes,
    the placed/unplaced pin counts, and the pads each slot holds on the
    net. A probe asks for the net's extremes with one slot replaced —
    best-or-runner-up answers in O(1) — instead of walking every pin
    (the #106 profile: ~89 points per term, 29% of a passive probe).
    Frozen: ``commit`` replaces the record whole, never edits one — the
    writer rule (ADR-0016, amended)."""

    min_x: _Extreme
    max_x: _Extreme
    min_y: _Extreme
    max_y: _Extreme
    placed: int
    unplaced: int
    slot_pads: Mapping[int, tuple[int, ...]]


class _TopTwo:
    """The working top-two-from-distinct-slots accumulator (#113): the
    best ``value`` and the ``slot`` holding it, plus the best ``other``
    value from any *different* slot. ``offer`` folds one point in;
    ``extreme`` freezes the result. ``better`` is the direction's
    comparison (``<`` for a min, ``>`` for a max): the same slot improving
    its own best never demotes the runner-up; a new slot takes the
    runner-up only on a better value, then promotes past the best."""

    __slots__ = ("other", "other_slot", "slot", "value")

    def __init__(self) -> None:
        self.value: int | None = None
        self.slot: int | None = None
        self.other: int | None = None
        self.other_slot: int | None = None

    def offer(self, value: int, slot: int, better) -> None:
        if self.slot == slot:
            if better(value, self.value):
                self.value = value
            return
        if self.other_slot is None or better(value, self.other):
            self.other, self.other_slot = value, slot
        if self.slot is None or better(self.other, self.value):
            self.value, self.slot, self.other, self.other_slot = (
                self.other,
                self.other_slot,
                self.value,
                self.slot,
            )

    def extreme(self) -> _Extreme:
        return _Extreme(self.value, self.slot, self.other, self.other_slot)


def _build_span_record(
    pins: tuple[tuple[int | None, int], ...],
    centers: list[tuple[Point, ...] | None],
) -> _SpanRecord:
    """One net's record, walked fresh off the pin list — the entry build
    and ``commit``'s refresh share this one body (#113)."""
    min_x, max_x = _TopTwo(), _TopTwo()
    min_y, max_y = _TopTwo(), _TopTwo()
    slot_pads: dict[int, list[int]] = {}
    placed = 0
    unplaced = 0
    for slot, pad_index in pins:
        if slot is None:
            unplaced += 1
            continue
        # every resolved slot's pads, placed or not — a None → point
        # probe needs the unplaced slot's own pads to fold in (#113)
        slot_pads.setdefault(slot, []).append(pad_index)
        cached = centers[slot]
        if cached is None:
            unplaced += 1
            continue
        placed += 1
        point = cached[pad_index]
        min_x.offer(point.x, slot, operator.lt)
        max_x.offer(point.x, slot, operator.gt)
        min_y.offer(point.y, slot, operator.lt)
        max_y.offer(point.y, slot, operator.gt)
    return _SpanRecord(
        min_x=min_x.extreme(),
        max_x=max_x.extreme(),
        min_y=min_y.extreme(),
        max_y=max_y.extreme(),
        placed=placed,
        unplaced=unplaced,
        slot_pads={slot: tuple(pads) for slot, pads in slot_pads.items()},
    )


_OUTER_SIDES = ("F.Cu", "B.Cu")
"""The stackup's outer copper layers — the copper grids, one per side
(ADR-0016, amended by #82). Inner layers need no grid: only ``*.Cu`` pads
carry inner copper, and those share both outer layers too."""


class _OriginFootprint(NamedTuple):
    """A footprint resolved at the world origin, unturned, front side —
    the duck-typed pose ``_anchored_copper`` reads (``.footprint``,
    ``.rotation``, ``.side``) for board-derived constants that need no
    placement. Capsule extents are swap-invariant under 90° turns and
    mirror-invariant across sides, so rotation 0 / F.Cu prices the cell
    size and the intra-footprint table for every pose."""

    footprint: Footprint
    rotation: int = 0
    side: str = "F.Cu"


def _pad_key(refs: tuple[str, ...], pad_indices: tuple[int, ...]) -> tuple:
    """A pad finding's storage key: the bare placement refs in canonical
    order, then the pads' indices within their parts. Locations cannot
    join: two pads of one part may share a site (duplicate numbers, the
    JP1–8/U2/U6 repeat), and #91's locations-keyed store collapsed their
    findings into one (#92) — the indices are the pair's identity, which
    also makes the key move-invariant."""
    return (
        None,
        tuple(ref.removeprefix("placement:") for ref in refs),
        pad_indices,
    )


class _ProbeBoard:
    """The finding builders' duck-typed Board (#92): the input Board's
    immutable state with ``placements`` read live off the Evaluator's
    committed state — the candidate substituted through the ``_MoveView``.
    The stand-in for ``_foreign_changed``'s materialised candidate Board:
    ``_describe`` and the gap re-derivations read identical inputs off
    identical state, so the native findings are byte-for-byte the foreign
    ones with no ``with_placement`` in the loop (the strict pin). Since
    #111 the ref lookups answer O(1) off the view too — ``placement`` is
    the ``Board.placement`` the wording builders call."""

    __slots__ = ("_board", "_view")

    def __init__(self, board: Board, view: _MoveView) -> None:
        self._board = board
        self._view = view

    def placement(self, ref: str) -> Placement | None:
        """The ref's placement as the move leaves it (#111): an O(1) read
        off the ``_MoveView``, the candidate substituted for the moved ref,
        ``None`` when unplaced — ``Board.placement``'s contract."""
        return self._view.get(ref)

    @property
    def placements(self):
        return self._view.values()

    @property
    def design_rules(self):
        return self._board.design_rules

    @property
    def outline(self):
        return self._board.outline

    @property
    def constraints(self):
        return self._board.constraints

    @property
    def groups(self):
        return self._board.groups

    @property
    def stackup(self):
        return self._board.stackup

    @property
    def nets(self):
        return self._board.nets


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
        self._criticality = criticality_weights(board)
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
        # unplace and never change side; a rotation change re-buckets like
        # a move (ADR-0025) — that is the whole invalidation story.
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
        # pad iteration dies here (ADR-0016). A pin of a ref outside the netlist
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
        # the per-net span records (#113, ADR-0016 amended): the entry
        # build; commit — the only writer after this — rebuilds the moved
        # slot's nets' entries through the same body
        self._span_records: list[_SpanRecord] = [
            _build_span_record(pins, self._centers) for pins in self._net_pins
        ]
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
                members = board.anchor_members(relation.target)
            for ref in members:
                slot = self._slot_of.get(ref)
                if slot is not None:
                    slot_constraints[slot].append(index)
        self._slot_constraints: tuple[tuple[int, ...], ...] = tuple(
            tuple(anchors) for anchors in slot_constraints
        )
        # the ADR-0022 pad checks are Evaluator-native (#92; ADR-0016
        # amended by #82): handled here, never through _foreign_changed's
        # per-probe Board materialisation — drc's pure companions over the
        # shared builders stay the bit-for-bit reference. The rules, the
        # net map, and the stackup are Board-immutable, read once.
        self._stackup = board.stackup
        self._clearance_nm = board.design_rules.clearance_nm
        self._edge_clearance_nm = board.design_rules.edge_clearance_nm
        self._net_by_pin = _net_map(board)
        self._shorting_index = self._index_of(drc._shorting_items)
        self._clearance_index = self._index_of(drc._clearance)
        self._edge_index = self._index_of(drc._copper_edge_clearance)
        self._pad_indices = frozenset(
            index
            for index in (
                self._shorting_index,
                self._clearance_index,
                self._edge_index,
            )
            if index is not None
        )
        self._pair_live = (
            self._shorting_index is not None or self._clearance_index is not None
        )
        # the copper-grid pair, one grid per outer side (#82): the
        # courtyard grid's machinery — commit the only writer,
        # generation-mark dedup, row-major query — over each slot's
        # copper box grown by the rule; *.Cu slots enter both grids;
        # inner layers need no grid. A rotation re-buckets like a move.
        self._copper_cells: dict[str, dict[tuple[int, int], list[int]]] = {}
        self._slot_copper_keys: list[dict[str, tuple[tuple[int, int], ...]] | None] = [
            None
        ] * len(self._refs)
        self._slot_coppers: list[tuple] = [()] * len(self._refs)
        self._slot_grown: list[Rectangle | None] = [None] * len(self._refs)
        self._intra: list[tuple[tuple[int, int, str, int], ...]] = [()] * len(
            self._refs
        )
        self._copper_cell = 1
        if self._pair_live:
            self._copper_cell = self._pad_grid_cell()
            for slot in range(len(self._refs)):
                self._intra[slot] = self._intra_pairs(slot)
            for slot, placement in enumerate(self._slots):
                if placement is not None:
                    self._pad_grid_insert(slot, placement)
        # check positions + keyed finding storage, one dict per check
        self._overlap_index = self._index_of(_courtyard_overlap)
        self._containment_index = self._index_of(_courtyard_outside_outline)
        self._proximity_index = self._index_of(_proximity_unmet)
        self._region_index = self._index_of(_region_unmet)
        self._edge_mount_index = self._index_of(_edge_mount_unmet)
        self._keep_together_index = self._index_of(_keep_together_unmet)
        self._handled: frozenset[int] = (
            frozenset(
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
            | self._pad_indices
        )
        self._findings: list[dict[tuple, Violation | None]] = [{} for _ in drc.CHECKS]
        for index, check in enumerate(drc.CHECKS):
            if index in self._pad_indices:
                continue  # seeded below from the shared records, pad-index keyed (#92)
            for raw in check(board):
                violation = _canonical(raw, board)
                self._findings[index][self._key_of(violation)] = violation
        if self._pad_indices:
            self._seed_pad_findings(board)
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

    def probe(self, ref: str, pose: Pose) -> Probe:
        """One candidate Pose's verdict and cost delta, without mutating
        state (ADR-0016): rejections — the common case — cost no
        bookkeeping."""
        plan = self._plan(ref, pose)
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
            pad_pair_tests=plan.pad_pair_tests,
        )

    def probe_key(self, ref: str, pose: Pose) -> tuple[int, int]:
        """The counted verdict (ADR-0016, amended by #64): the candidate's
        ranking key ``(error_count, total_nm)`` — the same legality notion
        the full probe reports, computed from the same merged findings and
        term deltas, but never materialized: no sorted findings tuple, no
        term-store copy, no Objective assembly. ``solve``'s inner loop
        reads only this; ``run_drc`` stays the authority, and the final
        ``legalize`` phase stays full-cost."""
        plan = self._plan(ref, pose)
        return (
            self._count_errors(self._errors, plan.changed_findings),
            plan.preview_total,
        )

    def commit(self, ref: str, pose: Pose) -> None:
        """Set ``ref``'s Pose — the sole mutation, one slot at a time
        (ADR-0016; the Pose typed by ADR-0025). Unknown refs raise
        ``ValueError``."""
        plan = self._plan(ref, pose)
        slot = plan.slot
        self._errors = self._count_errors(self._errors, plan.changed_findings)
        # the grids' remove halves read the slots' *old* buckets — they
        # run before the placement swaps, so the invalidation reads no
        # state it is about to overwrite
        if self._slot_cells[slot] is not None:
            self._index_remove(slot)
        if self._pair_live:
            self._pad_grid_remove(slot)
        self._slots[slot] = plan.candidate
        self._courtyards[slot] = plan.candidate_yard
        if plan.candidate_yard is not None:
            self._index_insert(slot, plan.candidate)
        if self._pair_live:
            self._pad_grid_insert(slot, plan.candidate)
        self._centers[slot] = plan.candidate_centers
        for net_index in self._slot_nets[slot]:
            self._span_records[net_index] = _build_span_record(
                self._net_pins[net_index], self._centers
            )
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

    def placement(self, ref: str) -> Placement | None:
        """The ref's placement as it stands — committed state, updated by
        ``commit`` — or ``None`` when unplaced or unknown (#67). The one
        live read: consumers look placements up here instead of keeping a
        mirror in lockstep with ``commit`` (the #63 drift class, made
        structurally impossible)."""
        return self._placements.get(ref)

    def position(self, ref: str) -> Point | None:
        """The ref's committed position, or ``None`` when unplaced or
        unknown (#67) — the position half of the Pose the candidate
        families build from."""
        placement = self._placements.get(ref)
        return None if placement is None else placement.pos

    def pose(self, ref: str) -> Pose | None:
        """The ref's committed Pose, or ``None`` when unplaced or unknown
        (#67) — the one operation's argument read back off the live state
        (ADR-0025)."""
        placement = self._placements.get(ref)
        return None if placement is None else Pose.of(placement)

    def pad_pairs(self, ref_a: str, ref_b: str) -> tuple[tuple[int, int], ...]:
        """The two refs' violating pad pairs — the shared builder's records
        read back off the stored findings (#95): ``(index_a, index_b)``
        tuples, the pad indices aligned to the refs as passed (the keys
        carry bare refs, ``_pad_key``'s convention). The repair
        vocabulary's obligation (ADR-0012 as amended by #83): legalize
        never re-prices a pair or re-implements the exemptions — it reads
        the checker's own verdict, the same one the probe verifies
        against. Empty when the pair checks are not registered; a pair is
        a short or a clearance, never both, so the two stores never
        repeat one."""
        if not self._pair_live:
            return ()
        wanted = tuple(sorted((ref_a, ref_b)))
        first_is_a = wanted[0] == ref_a
        pairs: list[tuple[int, int]] = []
        for index in (self._shorting_index, self._clearance_index):
            if index is None:
                continue
            for key, violation in self._findings[index].items():
                if violation is None or key[1] != wanted:
                    continue
                index_a, index_b = key[2]
                pairs.append((index_a, index_b) if first_is_a else (index_b, index_a))
        return tuple(pairs)

    def _stored_keys(
        self, index: int, head: str | None, refs: tuple[str, ...]
    ) -> list[tuple]:
        """The stored keys of check ``index`` for this head and refs — the
        drop half's address of a finding. Keys carry the finding's
        locations since #91 (several pad pairs share a placement pair), so
        a moved finding's old key is read off the store, not rebuilt."""
        return [
            key for key in self._findings[index] if key[0] == head and key[1] == refs
        ]

    def _plan(self, ref: str, pose: Pose) -> _MovePlan:
        slot = self._slot_of.get(ref)
        if slot is None:
            raise ValueError(
                f"unknown ref {ref!r}: not a component of this board's netlist"
            )
        current = self._slots[slot]
        candidate = Placement(
            ref=ref,
            footprint=self._footprints[slot],
            pos=Point(pose.x, pose.y),
            rotation=pose.rotation,
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
        pad_pair_tests = self._pad_changed(slot, ref, candidate, view, changed)
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
            pad_pair_tests=pad_pair_tests,
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

    def _pad_grid_cell(self) -> int:
        """The copper grids' cell size (#82): the sorted-middle grown
        extent over every footprint's copper box — the rule added on
        every side — floored at 1 nm, never so small that the largest
        box spans more than ~64 cells per axis. Copper-bearing
        footprints only, so courtyard-less parts are covered by
        construction; extents are swap-invariant under turns and
        mirror-invariant across sides, so origin resolution at rotation
        0 / F.Cu is board-derived and pose-free."""
        extents: list[int] = []
        for footprint in self._footprints:
            box = _anchored_copper(_OriginFootprint(footprint), self._stackup).box
            if box is None:
                continue
            extents.append(box.max.x - box.min.x + 2 * self._clearance_nm)
            extents.append(box.max.y - box.min.y + 2 * self._clearance_nm)
        if not extents:
            return 1
        median = sorted(extents)[len(extents) // 2]
        largest = max(extents)
        return max(1, median, -(-largest // 64))

    def _intra_pairs(self, slot: int) -> tuple[tuple[int, int, str, int], ...]:
        """The slot's intra-footprint pair verdicts, found once at entry
        (#92): placement-invariant — a rigid transform preserves capsule
        distances exactly and the layer mask wholesale — so origin
        resolution at rotation 0 prices every pose, and probes rewrite
        locations only. The counted verdict reads their number as a
        constant."""
        footprint = self._footprints[slot]
        ref = self._refs[slot]
        pads = _anchored_copper(_OriginFootprint(footprint), self._stackup).pads
        coppers = [
            _PadCopper(
                index=index,
                number=pad.number,
                pad=pad,
                capsule=capsule,
                center=center,
                layers=layers,
                net=self._net_by_pin.get((ref, pad.number)),
            )
            for index, pad, capsule, center, layers in pads
        ]
        pairs: list[tuple[int, int, str, int]] = []
        for position, pa in enumerate(coppers):
            for pb in coppers[position + 1 :]:
                if _pad_pair_exempt(pa, pb, footprint):
                    continue
                priced = _pad_pair_price(pa.capsule, pb.capsule, self._clearance_nm)
                if priced is not None:
                    pairs.append((pa.index, pb.index, *priced))
        return tuple(pairs)

    def _pad_grid_insert(self, slot: int, placement: Placement) -> None:
        """Bucket a committed slot into every cell its grown copper box
        covers, per outer side its pads resolve onto; the world capsules
        and the grown box cache beside the keys — commit is the only
        writer (#82)."""
        entry = _anchored_copper(placement, self._stackup)
        if entry.box is None:
            self._slot_coppers[slot] = ()
            self._slot_grown[slot] = None
            self._slot_copper_keys[slot] = None
            return
        coppers = _pad_coppers(placement, self._net_by_pin, self._stackup)
        self._slot_coppers[slot] = coppers
        grown = _grown(_shifted(entry.box, placement.pos), self._clearance_nm)
        self._slot_grown[slot] = grown
        sides = frozenset(layer for copper in coppers for layer in copper.layers) & set(
            _OUTER_SIDES
        )
        keys_per_side: dict[str, tuple[tuple[int, int], ...]] = {}
        for side in _OUTER_SIDES:
            if side not in sides:
                continue
            cells = self._copper_cells.setdefault(side, {})
            keys = tuple(
                (cx, cy)
                for cx in range(
                    grown.min.x // self._copper_cell,
                    grown.max.x // self._copper_cell + 1,
                )
                for cy in range(
                    grown.min.y // self._copper_cell,
                    grown.max.y // self._copper_cell + 1,
                )
            )
            for key in keys:
                cells.setdefault(key, []).append(slot)
            keys_per_side[side] = keys
        self._slot_copper_keys[slot] = keys_per_side

    def _pad_grid_remove(self, slot: int) -> None:
        """Drop a slot's old copper buckets — the invalidation half of
        commit; the stored per-side keys address the buckets, so no
        current state is read."""
        keys_per_side = self._slot_copper_keys[slot]
        if keys_per_side is None:
            return
        for side, keys in keys_per_side.items():
            cells = self._copper_cells[side]
            for key in keys:
                bucket = cells[key]
                bucket.remove(slot)
                if not bucket:
                    del cells[key]
        self._slot_copper_keys[slot] = None

    def _copper_query(self, box: Rectangle, sides: frozenset[str]) -> list[int]:
        """The committed slots whose cells the grown box covers, each
        once — the courtyard grid's row-major walk, one generation mark
        across every queried side so a ``*.Cu`` slot straddling both
        grids gathers once (#82). The mark and generation are probe
        scratch: no committed state changes, so "probe reads; commit is
        the only writer" (ADR-0016) holds where it matters."""
        self._generation += 1
        generation = self._generation
        gathered: list[int] = []
        for side in _OUTER_SIDES:
            if side not in sides:
                continue
            cells = self._copper_cells.get(side)
            if not cells:
                continue
            for cx in range(
                box.min.x // self._copper_cell, box.max.x // self._copper_cell + 1
            ):
                for cy in range(
                    box.min.y // self._copper_cell, box.max.y // self._copper_cell + 1
                ):
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
                violation = _overlap_violation(candidate, other, self._board)
                changed.append((index, self._key_of(violation), violation))
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
            for key in self._stored_keys(containment, None, (ref,)):
                changed.append((containment, key, None))
            finding = _overhang_finding(
                candidate, self._outline, self._exempt, self._board
            )
            if finding is not None:
                changed.append((containment, self._key_of(finding), finding))
        for constraint_index in self._slot_constraints[slot]:
            constraint = self._constraints[constraint_index]
            if not isinstance(constraint.kind, Requirement):
                continue  # Preferences never produce Violations (ADR-0011)
            relation = constraint.relation
            match relation:
                case Proximity():
                    if self._proximity_index is not None:
                        pair = tuple(sorted((relation.ref_a, relation.ref_b)))
                        for key in self._stored_keys(
                            self._proximity_index, constraint.id, pair
                        ):
                            changed.append((self._proximity_index, key, None))
                        finding = _proximity_finding(constraint, view, self._board)
                        if finding is not None:
                            changed.append(
                                (
                                    self._proximity_index,
                                    self._key_of(finding),
                                    finding,
                                )
                            )
                case Region():
                    if self._region_index is not None:
                        for key in self._stored_keys(
                            self._region_index, constraint.id, (ref,)
                        ):
                            changed.append((self._region_index, key, None))
                        finding = _region_finding(constraint, ref, view, self._board)
                        if finding is not None:
                            changed.append(
                                (self._region_index, self._key_of(finding), finding)
                            )
                case EdgeMount():
                    if self._edge_mount_index is not None:
                        for key in self._stored_keys(
                            self._edge_mount_index, constraint.id, (ref,)
                        ):
                            changed.append((self._edge_mount_index, key, None))
                        finding = _edge_mount_finding(
                            constraint, relation, candidate, self._board
                        )
                        if finding is not None:
                            changed.append(
                                (self._edge_mount_index, self._key_of(finding), finding)
                            )
                case KeepTogether():
                    if self._keep_together_index is not None:
                        members = tuple(
                            sorted(self._board.anchor_members(relation.target))
                        )
                        for key in self._stored_keys(
                            self._keep_together_index, constraint.id, members
                        ):
                            changed.append((self._keep_together_index, key, None))
                        finding = _keep_together_finding(constraint, view, self._board)
                        if finding is not None:
                            changed.append(
                                (
                                    self._keep_together_index,
                                    self._key_of(finding),
                                    finding,
                                )
                            )

    def _seed_pad_findings(self, board: Board) -> None:
        """The pad checks' entry findings (#92): the pair checks read the
        shared records — one pass serves both (#98) — and the edge check
        walks the shared per-pad builder, so the stored verdicts are
        drc's own objects and the keys carry the pad indices the delta
        rebuilds."""
        if self._pair_live:
            for record in _pad_pair_records(board):
                index = (
                    self._shorting_index
                    if record.kind == "short"
                    else self._clearance_index
                )
                if index is None:
                    continue
                self._findings[index][_pad_key(record.refs, record.pad_indices)] = (
                    record.violation
                )
        if self._edge_index is None:
            return
        for placement in board.placements:
            for copper in _pad_coppers(placement, self._net_by_pin, self._stackup):
                record = _pad_edge_record(placement, copper, self._exempt, board)
                if record is not None:
                    self._findings[self._edge_index][
                        _pad_key(record.violation.offending_refs, (record.pad_index,))
                    ] = record.violation

    def _pad_changed(
        self,
        slot: int,
        ref: str,
        candidate: Placement,
        view: _MoveView,
        changed: list[tuple[int, tuple, Violation | None]],
    ) -> int:
        """The pad checks' native delta (#92; ADR-0016 amended by #82):
        drop the moved ref's findings, then re-price its pads against the
        copper grids' gathered neighbourhood — the mover's world capsules
        built per probe, the neighbours' cached per committed slot, the
        footprint's origin capsules cached in drc — and rewrite the
        intra-footprint findings' locations. Returns the number of exact
        pad-pair tests: density-bounded by the grid, never n (the
        complexity assertion)."""
        if not self._pad_indices:
            return 0
        for index in self._pad_indices:
            self._drop_ref_findings(index, ref, changed)
        tests = 0
        board_view: _ProbeBoard | None = None

        def findings_board() -> _ProbeBoard:
            """The description builders' Board view, built only when a
            finding actually asks."""
            nonlocal board_view
            if board_view is None:
                board_view = _ProbeBoard(self._board, view)
            return board_view

        coppers = _pad_coppers(candidate, self._net_by_pin, self._stackup)
        if self._edge_index is not None and coppers:
            box = _shifted(
                _anchored_copper(candidate, self._stackup).box, candidate.pos
            )
            rule, outline = self._edge_clearance_nm, self._outline
            # the mover's pads only — the neighbour field never enters the
            # edge pre-filter (#98, the geometry helper's one definition)
            if not _clear_of_outline(box, outline, rule):
                for copper in coppers:
                    c = copper.capsule
                    r = c.radius
                    if (
                        c.core.min.x - r - outline.min.x >= rule
                        and outline.max.x - c.core.max.x - r >= rule
                        and c.core.min.y - r - outline.min.y >= rule
                        and outline.max.y - c.core.max.y - r >= rule
                    ):
                        continue
                    record = _pad_edge_record(
                        candidate, copper, self._exempt, findings_board()
                    )
                    if record is not None:
                        changed.append(
                            (
                                self._edge_index,
                                _pad_key(
                                    record.violation.offending_refs,
                                    (record.pad_index,),
                                ),
                                record.violation,
                            )
                        )
        if self._pair_live and coppers:
            grown = _grown(
                _shifted(_anchored_copper(candidate, self._stackup).box, candidate.pos),
                self._clearance_nm,
            )
            sides = frozenset(
                layer for copper in coppers for layer in copper.layers
            ) & set(_OUTER_SIDES)
            for other_slot in self._copper_query(grown, sides):
                if other_slot == slot:
                    continue
                other_grown = self._slot_grown[other_slot]
                if other_grown is None or _apart(other_grown, grown):
                    continue
                other = self._slots[other_slot]
                other_coppers = self._slot_coppers[other_slot]
                cl = self._clearance_nm
                ogmin, ogmax = other_grown.min, other_grown.max
                for pa in coppers:
                    ca = pa.capsule
                    ra = ca.radius
                    amin, amax = ca.core.min, ca.core.max
                    if (
                        amin.x - ra >= ogmax.x
                        or ogmin.x >= amax.x + ra
                        or amin.y - ra >= ogmax.y
                        or ogmin.y >= amax.y + ra
                    ):
                        continue
                    for pb in other_coppers:
                        cb = pb.capsule
                        lim = ra + cb.radius + cl
                        bmin, bmax = cb.core.min, cb.core.max
                        if (
                            amin.x - bmax.x >= lim
                            or bmin.x - amax.x >= lim
                            or amin.y - bmax.y >= lim
                            or bmin.y - amax.y >= lim
                        ):
                            continue
                        # between placements: no net-tie half, the pair
                        # exemption stops at layer mask and same net
                        if _pad_pair_exempt(pa, pb, None):
                            continue
                        tests += 1
                        priced = _pad_pair_price(pa.capsule, pb.capsule, cl)
                        if priced is None:
                            continue
                        kind, gap = priced
                        index = (
                            self._shorting_index
                            if kind == "short"
                            else self._clearance_index
                        )
                        if index is None:
                            continue
                        record = _pad_pair_record(
                            candidate,
                            other,
                            pa,
                            pb,
                            kind,
                            gap,
                            findings_board(),
                        )
                        changed.append(
                            (
                                index,
                                _pad_key(record.refs, record.pad_indices),
                                record.violation,
                            )
                        )
            # intra-footprint pairs: the verdicts were found once at
            # entry — placement-invariant — so a probe rewrites their
            # locations only, and the counted verdict reads their number
            # as a constant
            intra = self._intra[slot]
            if intra:
                by_index = {copper.index: copper for copper in coppers}
                for ia, ib, kind, gap in intra:
                    index = (
                        self._shorting_index
                        if kind == "short"
                        else self._clearance_index
                    )
                    if index is None:
                        continue
                    record = _pad_pair_record(
                        candidate,
                        candidate,
                        by_index[ia],
                        by_index[ib],
                        kind,
                        gap,
                        findings_board(),
                    )
                    changed.append(
                        (
                            index,
                            _pad_key(record.refs, record.pad_indices),
                            record.violation,
                        )
                    )
        return tests

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

        for index, check in enumerate(drc.CHECKS):
            if index in self._handled:
                continue
            if candidate_board is None:
                candidate_board = materialise()
            companion = drc.COMPANIONS.get(check)
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
        cached pad centres) and incident Preference hinges (``hinge``, the
        objective's own body). Everything else is carried."""
        changed: list[tuple[str, ObjectiveTerm]] = []
        for net_index in self._slot_nets[slot]:
            changed.append(
                (
                    net_ref(self._net_names[net_index]),
                    self._net_span_term(net_index, slot, centers),
                )
            )
        for constraint_index in self._slot_constraints[slot]:
            constraint = self._constraints[constraint_index]
            if not isinstance(constraint.kind, Preference):
                continue
            if isinstance(constraint.relation, Criticality):
                continue
            raw, status = hinge(constraint.relation, view, self._board)
            term_ref = constraint_ref(constraint.id)
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
        """One net's term, read off the maintained record (#113): the
        moved slot's candidate centres fold into best-or-runner-up
        extremes — O(1) per incident net, the mirror of the objective's
        per-net body over cached centres (ADR-0016, amended: the record
        replaces the O(degree) walk, and ``commit`` rebuilds it — the
        only writer). An unplaced pin marks the net PARTIAL."""
        record = self._span_records[net_index]
        pads = record.slot_pads.get(moved_slot, ())
        xs = [moved_centers[pad].x for pad in pads]
        ys = [moved_centers[pad].y for pad in pads]
        if self._centers[moved_slot] is None:
            # None → point: the moved slot's pads leave the unplaced count
            placed = record.placed + len(pads)
            unplaced = record.unplaced - len(pads)
        else:
            placed, unplaced = record.placed, record.unplaced
        if placed <= 1:
            span = 0
        else:
            span = (
                self._extreme(record.max_x, moved_slot, xs, max)
                - self._extreme(record.min_x, moved_slot, xs, min)
                + self._extreme(record.max_y, moved_slot, ys, max)
                - self._extreme(record.min_y, moved_slot, ys, min)
            )
        name = self._net_names[net_index]
        weight = self._criticality.get(name, 1)
        return ObjectiveTerm(
            kind=TermKind.NET_SPAN,
            ref=net_ref(name),
            weight=weight,
            raw_nm=span,
            weighted_nm=weight * span,
            status=TermStatus.PARTIAL if unplaced else TermStatus.ACTIVE,
        )

    @staticmethod
    def _extreme(direction: _Extreme, moved_slot: int, coords: list[int], pick) -> int:
        """One direction's extreme with the moved slot's committed points
        excluded and its candidate coordinates folded in: the best value
        when another slot holds it, the runner-up otherwise — never a walk
        of the pins (#113). Only called with two or more placed points, so
        the direction always has a value: the coordinates alone when no
        other slot is placed (the whole net rides the candidate), else the
        base beside them."""
        base = direction.other if direction.slot == moved_slot else direction.value
        if base is None:
            return pick(coords)
        return pick((base, *coords))

    @staticmethod
    def _index_of(check) -> int | None:
        return next(
            (
                index
                for index, registered in enumerate(drc.CHECKS)
                if registered is check
            ),
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
        (ADR-0016), shared by the overlap pass and the foreign companions."""
        for key, stored in self._findings[index].items():
            if stored is not None and ref in key[1]:
                changed.append((index, key, None))

    @staticmethod
    def _key_of(violation: Violation) -> tuple[str | None, tuple[str, ...], tuple]:
        """A finding's storage key: the leading ``constraint:<id>`` when
        present (ADR-0009's variable-arity convention), then the bare
        placement refs in canonical order, then the locations — the
        locations disambiguate the pad-copper checks' several findings
        between the same two placements (and an intra-footprint pair's
        repeated ref, #91); pad numbers repeat, the pad world centres do
        not."""
        refs = violation.offending_refs
        if refs[0].startswith("constraint:"):
            return (
                refs[0][len("constraint:") :],
                tuple(ref[len("placement:") :] for ref in refs[1:]),
                violation.locations,
            )
        return (
            None,
            tuple(ref[len("placement:") :] for ref in refs),
            violation.locations,
        )
