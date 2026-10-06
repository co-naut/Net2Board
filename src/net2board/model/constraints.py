"""Placement intent — the relation vocabulary and its kinds (ADR-0011, ADR-0014).

Constraints and Groups are Board state: an orchestrator states relations
one decision at a time and the Board carries them through every snapshot
(ADR-0011). This module is the value layer — what intent *is*; the Board
layer (``with_constraint``/``without_constraint``/``with_group``) is the
authoring surface that resolves intent against a concrete netlist.

One relation vocabulary serves both hardness kinds: a **Requirement** is
pass/fail and is never a cost term; a **Preference** only ranks among
Requirement-legal boards. **Criticality** is the recorded exception —
Preference-only (ADR-0014): it names a net whose NetSpan its weight
scales, and a hard span bound is deliberately deferred.

Anchors name their subject in ref form — ``placement:<ref>`` or
``group:<id>`` (ADR-0011) — so a Group drops in wherever a single
reference would do and no two id namespaces can collide. Grammar is
validated here; *resolution* (does the ref/group/net exist on this
Board?) happens when the mutation lands the intent, where a fail-fast
error can name the board it failed against.

Authoring mistakes raise the ``ConstraintError`` tree under
``Net2BoardError`` (ADR-0008's structured-exception posture, ADR-0013's
reconciliation). Malformed *file* syntax stays outside the tree as bare
``ValueError`` by design (ADR-0008), as do ``with_placement``'s M1
validation errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from net2board.errors import Net2BoardError
from net2board.geometry import Rectangle

__all__ = [
    "Constraint",
    "ConstraintError",
    "Criticality",
    "Edge",
    "EdgeMount",
    "Group",
    "KeepTogether",
    "Preference",
    "Proximity",
    "Region",
    "Requirement",
    "UnknownConstraintId",
    "UnknownConstraintRef",
]

_PLACEMENT_PREFIX = "placement:"
_GROUP_PREFIX = "group:"


class ConstraintError(Net2BoardError):
    """Root of the placement-intent authoring failure surface (ADR-0008, ADR-0011).

    Raised for malformed intent (bad anchor grammar, a negative bound,
    Criticality stated as a Requirement) and for intent that names
    something the Board does not have (an unknown ref, group, net, or
    pad). Every case is actionable: fix the stated intent, not the
    geometry.
    """


@dataclass(frozen=True)
class UnknownConstraintId(ConstraintError):
    """A mutation names a constraint id that is not on the Board."""

    id: str

    def __str__(self) -> str:
        return f"unknown constraint id {self.id!r}: not stated on this board"


@dataclass(frozen=True)
class UnknownConstraintRef(ConstraintError):
    """A relation's anchor names something the Board cannot expand.

    ``anchor`` carries the ref form exactly as stated —
    ``placement:<ref>`` for a ref that is not a netlist component,
    ``group:<id>`` for a group that is not on the Board, ``net:<name>``
    for a net that is not in the netlist.
    """

    anchor: str

    def __str__(self) -> str:
        return f"unknown anchor {self.anchor!r}: the board does not name it"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConstraintError(message)


def _non_empty_str(value, name: str) -> None:
    _require(
        isinstance(value, str) and bool(value),
        f"{name} must be a non-empty string, got {value!r}",
    )


def _non_negative_int(value, name: str) -> None:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{name} must be a non-negative integer (nm), got {value!r}",
    )


def anchor_parts(anchor) -> tuple[str, str]:
    """Split a ref-form anchor into ``("placement" | "group", name)``.

    The one anchor-grammar site (ADR-0011): ``placement:<ref>`` or
    ``group:<id>``, the name non-empty. Anything else is a
    ``ConstraintError`` naming the expected forms.
    """
    if isinstance(anchor, str):
        for kind, prefix in (
            ("placement", _PLACEMENT_PREFIX),
            ("group", _GROUP_PREFIX),
        ):
            if anchor.startswith(prefix) and len(anchor) > len(prefix):
                return kind, anchor[len(prefix) :]
    raise ConstraintError(
        f"malformed anchor {anchor!r}: expected 'placement:<ref>' or 'group:<id>'"
    )


class Edge(Enum):
    """An edge of the rectangular board frame, in display terms.

    The board frame is y-down (ADR-0014's transform), so ``TOP`` is the
    minimum-y edge and ``BOTTOM`` the maximum-y edge; ``LEFT``/``RIGHT``
    are the minimum/maximum-x edges. EdgeMount names its edge with this —
    one EdgeMount per edge per ref (ADR-0018's stacking rule).
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


@dataclass(frozen=True)
class Proximity:
    """Two named pads within a stated distance (ADR-0011).

    Pad center to pad center, Euclidean in integer nm, bound inclusive;
    either side satisfies by default, ``same_side`` is the knob. The
    decoupling-cap relation.
    """

    ref_a: str
    pad_a: str
    ref_b: str
    pad_b: str
    bound_nm: int
    same_side: bool = False

    def __post_init__(self):
        for name in ("ref_a", "pad_a", "ref_b", "pad_b"):
            _non_empty_str(getattr(self, name), name)
        _non_negative_int(self.bound_nm, "bound_nm")


@dataclass(frozen=True)
class Region:
    """Every target courtyard inside a stated rectangle (ADR-0011).

    Closed containment — boundary contact is inside. Inclusive:
    non-members are not excluded, the region gathers, it does not fence.
    """

    target: str
    rect: Rectangle

    def __post_init__(self):
        anchor_parts(self.target)
        rect = self.rect
        _require(
            rect.min.x <= rect.max.x and rect.min.y <= rect.max.y,
            f"Region rect must have min <= max on both axes, got "
            f"({rect.min.x}, {rect.min.y})..({rect.max.x}, {rect.max.y})",
        )


@dataclass(frozen=True)
class EdgeMount:
    """A courtyard face on a stated outline edge (ADR-0011, ADR-0018).

    ``max_overhang_nm`` is the permitted distance past the edge —
    ``None`` means the face sits flush. Rectangle outlines only; a
    Requirement EdgeMount's stated edge is the sole exemption from
    outline containment (a Preference exempts nothing), and corners
    stack one EdgeMount per edge, uniqueness being per constraint id.
    """

    target: str
    edge: Edge
    max_overhang_nm: int | None = None

    def __post_init__(self):
        anchor_parts(self.target)
        _require(
            isinstance(self.edge, Edge),
            f"edge must be an Edge, got {self.edge!r}",
        )
        if self.max_overhang_nm is not None:
            _non_negative_int(self.max_overhang_nm, "max_overhang_nm")


@dataclass(frozen=True)
class KeepTogether:
    """A group's courtyards within a stated extent, at any position (ADR-0011).

    The union bounding box must fit ``max_width_nm`` × ``max_height_nm``
    — a floating bound with no stated location. The target is a group
    anchor by definition: a floating extent over a Group.
    """

    target: str
    max_width_nm: int
    max_height_nm: int

    def __post_init__(self):
        kind, _ = anchor_parts(self.target)
        _require(
            kind == "group",
            f"KeepTogether target must be a group anchor 'group:<id>', "
            f"got {self.target!r}",
        )
        for name in ("max_width_nm", "max_height_nm"):
            value = getattr(self, name)
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value > 0,
                f"{name} must be a positive integer (nm), got {value!r}",
            )


@dataclass(frozen=True)
class Criticality:
    """A net whose NetSpan its Constraint weight scales (ADR-0014).

    The one Preference-only relation — ``Constraint.__post_init__``
    rejects it as a Requirement. Weight 0 ignores the net: the lever for
    plane nets such as GND. Deliberately no Requirement form; a hard
    span bound is deferred.
    """

    net: str

    def __post_init__(self):
        _non_empty_str(self.net, "net")


@dataclass(frozen=True)
class Requirement:
    """The hard kind of Constraint (ADR-0011): pass/fail, checked exactly,
    reported as a Violation, never downgraded to a cost term."""


@dataclass(frozen=True)
class Preference:
    """The soft kind of Constraint (ADR-0011, ADR-0014): ranks among
    Requirement-legal boards; its weight is an integer multiplier on
    nanometres of shortfall."""


_RELATIONS = (Proximity, Region, EdgeMount, KeepTogether, Criticality)
_RELATION_NAMES = tuple(relation.__name__ for relation in _RELATIONS)


@dataclass(frozen=True)
class Constraint:
    """One named relation about where parts go (ADR-0011).

    ``kind`` picks the hardness — ``Requirement()`` or ``Preference()``;
    ``relation`` is one of the five relations above; ``weight`` is an
    optional integer multiplier (default 1, 0 legal, negative rejected —
    ADR-0014): shortfall multiplier for a Preference, NetSpan scaler for
    a Criticality, carried but unused for a Requirement, which is
    checked regardless. ``id`` is the orchestrator's handle — surfaced
    as ``constraint:<id>``, unique on the Board, upserted by the
    mutations.
    """

    id: str
    kind: Requirement | Preference
    relation: Proximity | Region | EdgeMount | KeepTogether | Criticality
    weight: int = 1

    def __post_init__(self):
        _non_empty_str(self.id, "id")
        _require(
            isinstance(self.kind, (Requirement, Preference)),
            f"kind must be Requirement() or Preference(), got {self.kind!r}",
        )
        _require(
            isinstance(self.relation, _RELATIONS),
            f"relation must be one of {_RELATION_NAMES}, got {self.relation!r}",
        )
        _non_negative_int(self.weight, "weight")
        if isinstance(self.relation, Criticality) and isinstance(
            self.kind, Requirement
        ):
            raise ConstraintError(
                f"constraint {self.id!r}: Criticality is Preference-only "
                f"(ADR-0014) — state it as a Preference"
            )


@dataclass(frozen=True)
class Group:
    """A named set of placement references (ADR-0011).

    Carries no geometry and no promise of its own: usable as a
    Constraint's target wherever a single reference would do, expanded
    at check time. ``refs`` are stored canonically sorted — ref order is
    not part of intent, so two statements of the same set are the same
    Group.
    """

    id: str
    refs: tuple[str, ...]

    def __post_init__(self):
        _non_empty_str(self.id, "id")
        _require(
            bool(self.refs),
            f"group {self.id!r} has no refs: a Group is a named set of "
            f"placement references",
        )
        for ref in self.refs:
            _non_empty_str(ref, "group ref")
        _require(
            len(set(self.refs)) == len(self.refs),
            f"group {self.id!r} states a ref twice: {self.refs!r}",
        )
        object.__setattr__(self, "refs", tuple(sorted(self.refs)))
