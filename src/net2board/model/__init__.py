"""The Board model — immutable snapshots and value objects (ADR-0001 … ADR-0004).

``Board`` is the sole root aggregate (ADR-0002): it owns the resolved
components, placements, nets, outline, and stackup. Every dataclass is
frozen; every mutation returns a new Board; collections are tuples so
snapshots compare by exact equality — branch, backtrack, and diff without
aliasing or float drift.

On ``components``: ADR-0002 names placements, nets, outline, and stackup as
Board fields. M1 adds the resolved netlist as ``components`` —
``with_placement(ref, ...)`` must find the footprint of a *not yet placed*
ref, while the Board may not retain the BoardSpec (ADR-0007). A component is
a ``Component(ref, footprint)``; placing embeds the footprint in the
``Placement``.

All coordinates are integer nanometres (ADR-0005). Objects are referenced
from outside by string refs ``"<kind>:<id>"`` (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from net2board.geometry import Point, Rectangle, Shape
from net2board.model.constraints import (
    Constraint,
    ConstraintError,
    Criticality,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Preference,
    Proximity,
    Region,
    Requirement,
    UnknownConstraintId,
    UnknownConstraintRef,
    anchor_parts,
)

__all__ = [
    "Board",
    "Component",
    "Constraint",
    "ConstraintError",
    "Criticality",
    "Edge",
    "EdgeMount",
    "Footprint",
    "Group",
    "KeepTogether",
    "Net",
    "Pad",
    "Placement",
    "Preference",
    "Proximity",
    "Region",
    "Requirement",
    "UnknownConstraintId",
    "UnknownConstraintRef",
]

SIDES = ("F.Cu", "B.Cu")
ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class Pad:
    """A footprint's copper landing area, stored for export.

    ``shape_enum`` is the opaque KiCad pad-shape string, carried verbatim to
    the exporter (ADR-0006); pad geometry and its world transform are
    deferred until a milestone adds a pad-clearance check.
    """

    number: str
    local_pos: Point
    size: tuple[int, int]
    drill: int | None
    layers: tuple[str, ...]
    shape_enum: str


@dataclass(frozen=True)
class Footprint:
    """A part's landing pattern embedded in a Placement or Component."""

    entry_name: str
    pads: tuple[Pad, ...]
    courtyard: Shape | None


@dataclass(frozen=True)
class Net:
    """A logical connection: pins as ``(ref, pad_number)`` tuples.

    In M1 a net has no physical realization — copper (traces, vias, zones)
    is out of scope — so the pin set is the whole truth.
    """

    name: str
    pins: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Component:
    """A netlist component resolved against the footprint library."""

    ref: str
    footprint: Footprint


@dataclass(frozen=True)
class Placement:
    """A footprint bound to a board location, identified by its ref.

    ``locked`` is mobility between Boards (ADR-0012): a locked placement
    is one the engine may not move — the part is physically committed to
    its location. Deliberately not a Constraint: every Constraint is a
    checkable condition on one Board; locked is a permission governing
    what tools may do *between* two Boards. The ``with_placement`` upsert
    replaces the whole placement, so a relocation restates the lock.

    Validates itself at construction — rotation one of the 90°
    increments, side one of ``SIDES`` — the same fail-fast posture as
    every relation value: the transform and the checks may assume it,
    so an unvalidated rotation can never silently measure as 0.
    """

    ref: str
    footprint: Footprint
    pos: Point
    rotation: int
    side: str
    locked: bool = False

    def __post_init__(self):
        if self.rotation not in ROTATIONS:
            raise ValueError(
                f"invalid rotation {self.rotation} for {self.ref!r}: must be one "
                f"of {ROTATIONS} (90° increments)"
            )
        if self.side not in SIDES:
            raise ValueError(
                f"invalid side {self.side!r} for {self.ref!r}: must be one of {SIDES}"
            )


@dataclass(frozen=True)
class Board:
    """An immutable snapshot of a circuit's physical realization.

    Alongside the payload, each snapshot carries private derived
    indexes — ``_component_by_ref``, ``_placement_index``,
    ``_constraint_index``, ``_group_index``, and ``_net_index`` — so the
    mutations validate a ref, fetch a footprint, or find an existing
    entry without scanning the board (ADR-0001 as amended by #29).
    They are excluded from ``repr`` and equality (snapshots compare by
    the payload alone). Every construction door is safe by construction:
    the public constructor (``Board(...)``, ``dataclasses.replace``
    included) rebuilds the indexes from the payload and resolves every
    stated constraint's anchors — a dangling anchor raises
    ``UnknownConstraintRef`` here, not downstream as a bare miss — while
    the mutations share the untouched indexes through a private fast
    path, since components and nets never change under the mutations and
    an index survives an in-place upsert untouched (a dict copy happens
    only when an entry is newly appended).

    ``constraints`` and ``groups`` are the placement intent (ADR-0011):
    part of equality and hash — identical geometry with different intent
    is a different value. Intent is authored one upsert per decision
    (``with_constraint``/``without_constraint``/``with_group``), the same
    contract as ``with_placement``.
    """

    components: tuple[Component, ...]
    placements: tuple[Placement, ...]
    nets: tuple[Net, ...]
    outline: Rectangle
    stackup: tuple[str, ...]
    constraints: tuple[Constraint, ...] = ()
    groups: tuple[Group, ...] = ()
    _component_by_ref: dict[str, Component] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _placement_index: dict[str, int] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _constraint_index: dict[str, int] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _group_index: dict[str, int] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _net_index: dict[str, Net] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        object.__setattr__(
            self, "_component_by_ref", {comp.ref: comp for comp in self.components}
        )
        object.__setattr__(
            self,
            "_placement_index",
            {placement.ref: i for i, placement in enumerate(self.placements)},
        )
        object.__setattr__(
            self,
            "_constraint_index",
            {constraint.id: i for i, constraint in enumerate(self.constraints)},
        )
        object.__setattr__(
            self,
            "_group_index",
            {group.id: i for i, group in enumerate(self.groups)},
        )
        object.__setattr__(self, "_net_index", {net.name: net for net in self.nets})
        for constraint in self.constraints:
            self._validate_constraint(constraint)

    def placement(self, ref: str) -> Placement | None:
        """The ref's placement, or ``None`` when unplaced — an O(1) index
        read (ADR-0001 as amended by #29): consumers look a placement up,
        they do not scan ``placements``."""
        at = self._placement_index.get(ref)
        return None if at is None else self.placements[at]

    def component(self, ref: str) -> Component | None:
        """The ref's netlist component, or ``None`` when unknown — the
        footprint lookup behind ``with_placement``, offered to every
        consumer (an unplaced ref still has its footprint here)."""
        return self._component_by_ref.get(ref)

    def with_placement(
        self, ref: str, x: int, y: int, rotation: int, side: str, locked: bool = False
    ) -> Board:
        """Place or relocate ``ref`` — an upsert returning a new Board.

        A new ref is appended; an existing ref is relocated in place, keeping
        its position in the tuple. Unknown refs raise ``ValueError``
        immediately with a clear message (ADR-0003); an invalid rotation or
        side raises from ``Placement``'s own validation, after the ref is
        known good. ``locked`` (ADR-0012) defaults unlocked, leaving M1 call
        sites untouched; the upsert replaces the whole placement, so a
        relocation restates the lock. Ref validation, footprint lookup,
        and relocation site are O(1) index hits; the board-size terms
        left are the copies the spine pays anyway — the placement tuple
        on every call, plus the placement index only when a ref is newly
        appended — which ADR-0001 accepts as cheap.
        """
        component = self._component_by_ref.get(ref)
        if component is None:
            raise ValueError(
                f"unknown ref {ref!r}: not a component of this board's netlist"
            )
        placement = Placement(
            ref=ref,
            footprint=component.footprint,
            pos=Point(x, y),
            rotation=rotation,
            side=side,
            locked=locked,
        )
        placements, placement_index = self._upsert(
            self.placements, ref, self._placement_index, placement
        )
        return self._with(placements=placements, _placement_index=placement_index)

    def with_constraint(self, constraint: Constraint) -> Board:
        """State or restate one Constraint — an upsert by id (ADR-0011).

        A new id is appended; an existing id is replaced in place,
        keeping its position in the tuple — re-stating the identical
        constraint is idempotent (an equal Board), re-stating with
        different content replaces. Intent is validated eagerly: an
        anchor the Board cannot expand — a ref that is not a netlist
        component, a group that is not stated, a net that is not in the
        netlist — raises ``UnknownConstraintRef``; a pad the named
        footprint does not have raises ``ConstraintError`` (ADR-0008).
        Unplaced refs are valid anchors: their constraints are pending,
        not invalid (ADR-0011).
        """
        self._validate_constraint(constraint)
        constraints, constraint_index = self._upsert(
            self.constraints, constraint.id, self._constraint_index, constraint
        )
        return self._with(constraints=constraints, _constraint_index=constraint_index)

    def without_constraint(self, id: str) -> Board:
        """Withdraw the Constraint stated under ``id``.

        An unknown id raises ``UnknownConstraintId`` — withdrawing
        nothing is a caller mistake, not a no-op (ADR-0011's fail-fast
        authoring posture).
        """
        index = self._constraint_index.get(id)
        if index is None:
            raise UnknownConstraintId(id)
        constraints = self.constraints[:index] + self.constraints[index + 1 :]
        constraint_index = {
            constraint.id: i for i, constraint in enumerate(constraints)
        }
        return self._with(constraints=constraints, _constraint_index=constraint_index)

    def with_group(self, group: Group) -> Board:
        """State or restate one Group — an upsert by id, the same
        one-upsert contract as ``with_constraint`` (ADR-0011).

        Every ref must be a netlist component (``UnknownConstraintRef``
        otherwise); unplaced refs are legal members. A Group carries no
        geometry and no promise of its own — restating one rewrites its
        membership wherever a constraint anchors to it, at check time.
        """
        for ref in group.refs:
            self._require_ref(ref)
        groups, group_index = self._upsert(
            self.groups, group.id, self._group_index, group
        )
        return self._with(groups=groups, _group_index=group_index)

    def _upsert(
        self,
        collection: tuple,
        key: str,
        index: dict[str, int],
        item,
    ) -> tuple[tuple, dict[str, int]]:
        """Append or replace in place — the one upsert shape behind every
        mutation (ADR-0003): a new key appends (and copies the index once),
        an existing key replaces keeping its tuple position (index shared)."""
        at = index.get(key)
        if at is None:
            return collection + (item,), {**index, key: len(collection)}
        return collection[:at] + (item,) + collection[at + 1 :], index

    def _with(self, **changes) -> Board:
        """A new Board with the payload changed and every untouched index
        shared — the private fast path behind the mutations. It bypasses
        ``__init__`` (whose door rebuilds the indexes and re-resolves the
        intent) because the mutation has already validated the change and
        carries the O(1)-shared indexes: sharing here is what keeps every
        mutation's cost at the spine's copies (ADR-0001 as amended)."""
        fields = {
            "components": self.components,
            "placements": self.placements,
            "nets": self.nets,
            "outline": self.outline,
            "stackup": self.stackup,
            "constraints": self.constraints,
            "groups": self.groups,
            "_component_by_ref": self._component_by_ref,
            "_placement_index": self._placement_index,
            "_constraint_index": self._constraint_index,
            "_group_index": self._group_index,
            "_net_index": self._net_index,
        }
        fields.update(changes)
        board = object.__new__(Board)
        for name, value in fields.items():
            object.__setattr__(board, name, value)
        return board

    def _require_ref(self, ref: str) -> None:
        if ref not in self._component_by_ref:
            raise UnknownConstraintRef(f"placement:{ref}")

    def _require_pad(self, ref: str, pad_number: str) -> None:
        component = self._component_by_ref.get(ref)
        if component is None:
            raise UnknownConstraintRef(f"placement:{ref}")
        footprint = component.footprint
        if all(pad.number != pad_number for pad in footprint.pads):
            raise ConstraintError(
                f"unknown pad {pad_number!r} on {ref!r}: footprint "
                f"{footprint.entry_name!r} has pads "
                f"{tuple(pad.number for pad in footprint.pads)}"
            )

    def _resolve_anchor(self, anchor: str) -> None:
        kind, name = anchor_parts(anchor)
        if kind == "placement":
            self._require_ref(name)
        elif name not in self._group_index:
            raise UnknownConstraintRef(anchor)

    def _validate_constraint(self, constraint: Constraint) -> None:
        """Resolve the relation's anchors against this Board (ADR-0011)."""
        match constraint.relation:
            case Proximity():
                self._require_ref(constraint.relation.ref_a)
                self._require_ref(constraint.relation.ref_b)
                self._require_pad(constraint.relation.ref_a, constraint.relation.pad_a)
                self._require_pad(constraint.relation.ref_b, constraint.relation.pad_b)
            case Region() | EdgeMount() | KeepTogether():
                self._resolve_anchor(constraint.relation.target)
            case Criticality():
                if constraint.relation.net not in self._net_index:
                    raise UnknownConstraintRef(f"net:{constraint.relation.net}")

    def unplaced_refs(self) -> tuple[str, ...]:
        """Netlist refs with no placement, in netlist order.

        Derived, not stored (ADR-0002). Derived from components — on KiCad
        netlists every component's pins appear in some net, matching the
        ADR's pins-minus-placements derivation, but a hand-built net may
        leave a component pinless and it is still unplaced work.
        """
        placed = {placement.ref for placement in self.placements}
        return tuple(comp.ref for comp in self.components if comp.ref not in placed)

    def unrouted_net_names(self) -> tuple[str, ...]:
        """Every net name — the goal signal, total by design: the engine
        routes nothing and never ingests copper (ADR-0017); routing lands
        in KiCad and changes nothing the Board knows."""
        return tuple(net.name for net in self.nets)
