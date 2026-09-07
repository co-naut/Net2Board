"""build_board — the single pure IR → Board join (ADR-0007).

No filesystem access, no state: every Board in the system is born here.
Pin-to-pad binding is string equality of pin number and pad number; a netlist
pin with no matching pad raises ``PinPadBindingError`` (ADR-0008); extra pads
stay silent. The resulting Board does not retain the BoardSpec.
"""

from __future__ import annotations

from net2board.boardspec import BoardSpec, PinPadBindingError
from net2board.ir import FootprintIR, NetIR, PadIR
from net2board.model import Board, Component, Footprint, Net, Pad

__all__ = ["build_board"]


def build_board(spec: BoardSpec) -> Board:
    """Join parsed IR into an unplaced Board snapshot."""
    components = tuple(
        Component(ref=comp.ref, footprint=_build_footprint(spec, comp.entry_name))
        for comp in spec.netlist_ir.comps
    )
    pads_by_ref = {comp.ref: comp.footprint.pads for comp in components}
    nets = tuple(
        Net(name=net.name, pins=tuple(_bind_pins(net, pads_by_ref)))
        for net in spec.netlist_ir.nets
    )
    return Board(
        components=components,
        placements=(),
        nets=nets,
        outline=spec.outline,
        stackup=spec.stackup,
    )


def _build_footprint(spec: BoardSpec, entry_name: str) -> Footprint:
    try:
        footprint_ir = spec.footprint_irs[entry_name]
    except KeyError:
        raise ValueError(
            f"no footprint IR for entry {entry_name!r} — referenced by a netlist comp "
            f"but absent from the BoardSpec"
        ) from None
    return _to_footprint(footprint_ir)


def _to_footprint(footprint_ir: FootprintIR) -> Footprint:
    return Footprint(
        entry_name=footprint_ir.entry_name,
        pads=tuple(_to_pad(pad_ir) for pad_ir in footprint_ir.pads),
        courtyard=footprint_ir.courtyard,
    )


def _to_pad(pad_ir: PadIR) -> Pad:
    return Pad(
        number=pad_ir.number,
        local_pos=pad_ir.local_pos,
        size=pad_ir.size,
        drill=pad_ir.drill,
        layers=pad_ir.layers,
        shape_enum=pad_ir.shape_enum,
    )


def _bind_pins(net: NetIR, pads_by_ref: dict):
    for ref, pin in net.nodes:
        pads = pads_by_ref.get(ref)
        if pads is None:
            raise ValueError(
                f"net {net.name!r} references unknown component ref {ref!r}"
            )
        if not any(pad.number == pin for pad in pads):
            raise PinPadBindingError(ref=ref, pin_number=pin)
        yield (ref, pin)
