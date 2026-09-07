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

from dataclasses import dataclass

from net2board.geometry import Point, Rectangle, Shape

__all__ = [
    "Board",
    "Component",
    "Footprint",
    "Net",
    "Pad",
    "Placement",
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
    """A footprint bound to a board location, identified by its ref."""

    ref: str
    footprint: Footprint
    pos: Point
    rotation: int
    side: str


@dataclass(frozen=True)
class Board:
    """An immutable snapshot of a circuit's physical realization."""

    components: tuple[Component, ...]
    placements: tuple[Placement, ...]
    nets: tuple[Net, ...]
    outline: Rectangle
    stackup: tuple[str, ...]

    def with_placement(
        self, ref: str, x: int, y: int, rotation: int, side: str
    ) -> Board:
        """Place or relocate ``ref`` — an upsert returning a new Board.

        A new ref is appended; an existing ref is relocated in place, keeping
        its position in the tuple. Unknown refs, rotations outside the 90°
        increments, and sides other than ``"F.Cu"``/``"B.Cu"`` raise
        ``ValueError`` immediately with a clear message (ADR-0003).
        """
        if ref not in {comp.ref for comp in self.components}:
            raise ValueError(
                f"unknown ref {ref!r}: not a component of this board's netlist"
            )
        if rotation not in ROTATIONS:
            raise ValueError(
                f"invalid rotation {rotation} for {ref!r}: must be one of {ROTATIONS} "
                f"(90° increments)"
            )
        if side not in SIDES:
            raise ValueError(
                f"invalid side {side!r} for {ref!r}: must be one of {SIDES}"
            )
        footprint = next(comp.footprint for comp in self.components if comp.ref == ref)
        placement = Placement(
            ref=ref, footprint=footprint, pos=Point(x, y), rotation=rotation, side=side
        )
        if any(existing.ref == ref for existing in self.placements):
            placements = tuple(
                placement if existing.ref == ref else existing
                for existing in self.placements
            )
        else:
            placements = self.placements + (placement,)
        return Board(
            components=self.components,
            placements=placements,
            nets=self.nets,
            outline=self.outline,
            stackup=self.stackup,
        )

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
        """Every net name — the honest goal signal: M1 routes nothing."""
        return tuple(net.name for net in self.nets)
