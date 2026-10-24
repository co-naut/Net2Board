"""Intermediate representations — parsed netlist and footprint data (ADR-0007).

The IR is the embedder's construction surface: a ``BoardSpec`` is built from
these types, and ``build_board`` is a pure function over them. IR types carry
no filesystem paths and no positions — a library ``.kicad_mod`` has an
implicit origin of (0, 0), so every pad position is footprint-local.

Circle courtyards keep ``radius_sq`` for exact-integer DRC arithmetic and the
defining ``end`` point for export emission (ADR-0006 as amended by ADR-0010).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from net2board.geometry import Point, Rectangle, Shape

__all__ = [
    "CompIR",
    "FootprintIR",
    "LibpartIR",
    "NetIR",
    "NetlistIR",
    "NodeIR",
    "PadIR",
    "parse_footprint",
    "parse_netlist",
]


@dataclass(frozen=True)
class CompIR:
    """A netlist component: its reference designator and library entry name.

    ``entry_name`` is the bare library entry — the loader's nickname-strip
    rule already applied; the library nickname never reaches the IR.
    """

    ref: str
    entry_name: str


@dataclass(frozen=True)
class LibpartIR:
    """A netlist libpart, reduced to its ``(lib, part)`` lookup key."""

    lib: str
    part: str


class NodeIR(NamedTuple):
    """A net membership: one schematic pin, joined to a pad by string equality.

    A ``NamedTuple`` so nodes compare equal to plain ``(ref, pin)`` tuples —
    the same shape the Board's ``Net.pins`` carry (ADR-0002).
    """

    ref: str
    pin: str


@dataclass(frozen=True)
class NetIR:
    """A named electrical connection over ``NodeIR`` pins."""

    name: str
    nodes: tuple[NodeIR, ...]


@dataclass(frozen=True)
class NetlistIR:
    """The parsed netlist: components, nets, and libpart keys.

    ``libparts`` is minimal — the ``(lib, part)`` key each comp's
    ``libsource`` points at — because per-pin name/type metadata already
    rides on the net nodes and nothing in M1 consumes more. Empty for
    embedder-built IR.
    """

    comps: tuple[CompIR, ...]
    nets: tuple[NetIR, ...]
    libparts: tuple[LibpartIR, ...] = ()


@dataclass(frozen=True)
class PadIR:
    """A footprint pad as export data: local position, size, drill, layers.

    ``shape_enum`` is the opaque KiCad pad-shape string (``circle``, ``rect``,
    ``oval``, ...) — carried to the exporter verbatim, never interpreted as
    geometry (ADR-0006). ``local_rotation`` is the pad's rotation inside the
    footprint frame as the library file states it, degrees, ``[0, 360)``,
    restricted to the 90° multiples at load (ADR-0022) — composed into the
    pad's world ``(at … rot)`` token by the ``.kicad_pcb`` writer (angles
    are library-local; the #73 "KiCad drops the angle" reading was the
    board-absolute demo fixture misread).
    """

    number: str
    local_pos: Point
    size: tuple[int, int]
    drill: int | None
    layers: tuple[str, ...]
    shape_enum: str
    local_rotation: int = 0
    roundrect_rratio: Decimal | None = None
    hull: Rectangle | None = None


@dataclass(frozen=True)
class FootprintIR:
    """A parsed ``.kicad_mod``: pads plus the F.CrtYd courtyard, if any.

    ``courtyard`` is ``None`` when the footprint has no courtyard graphics —
    a valid state that the overlap check skips (ADR-0006).
    ``net_tie_pad_groups`` is the footprint's net-tie groups (KiCad's
    grammar: each quoted string a comma-separated pad-number list); pads
    sharing a group never violate each other (ADR-0022).
    """

    entry_name: str
    pads: tuple[PadIR, ...]
    courtyard: Shape | None
    net_tie_groups: tuple[frozenset[str], ...] = ()


from net2board.ir.parsers import parse_footprint, parse_netlist
