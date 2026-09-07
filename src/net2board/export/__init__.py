"""Export — the canonical `.kicad_pcb` serializer (ADR-0010, T3 research).

``export_pcb(board) -> str`` is a pure function: no filesystem, no clock,
no randomness — the same Board always serializes to the same bytes.
Ordering is canonical everywhere it is observable: footprints by reference
designator, pads in library order, net ids assigned by net name sorted
lexicographically with ``(net 0 "")`` reserved for unconnected pads and
never entering the id sequence. Millimetres render in pure integer
arithmetic to exactly six decimals — never scientific notation, and
``-0.000000`` is impossible by construction.

Back-side placements bake the mirror into the emitted footprint-local
coordinates and emit their courtyard graphics on ``B.CrtYd``: KiCad
applies translate + rotate only to footprint-internal geometry on load
and pairs courtyards per graphics layer (both probed on kicad-cli
10.0.6, ADR-0006/0010), so the writer owns the left-right flip and the
side-selecting courtyard layer — together they make the oracle's
courtyard world identical to ``world_courtyard``'s.
"""

from __future__ import annotations

import re

from net2board.geometry import Point, Rectangle, Shape, mirror_local
from net2board.model import Board, Pad, Placement

__all__ = ["export_pcb"]

FILE_FORMAT_VERSION = 20241229
GENERATOR = "net2board"
GENERATOR_VERSION = "1"

_COURTYARD_STROKE_NM = 50_000  # 0.05 mm — KiCad library convention
_OUTLINE_STROKE_NM = 100_000  # 0.10 mm

# The non-copper layers the export can reference — pad mask tokens,
# courtyard graphics, the Edge.Cuts outline — with the file-format ids
# pcbnew itself writes for a 2-layer board.
_USER_LAYERS = (
    (1, "F.Mask"),
    (3, "B.Mask"),
    (25, "Edge.Cuts"),
    (31, "F.CrtYd"),
    (29, "B.CrtYd"),
)

_COPPER_NAME = re.compile(r"\A(?:F\.Cu|In[1-9][0-9]*\.Cu|B\.Cu)\Z")


def export_pcb(board: Board) -> str:
    """Serialize ``board`` to canonical ``.kicad_pcb`` text (KiCad 9/10).

    Placements only — unplaced components have no physical realization to
    write. Every net stays declared at top level (canonical, and what
    pcbnew itself emits) even when no placed pad carries it.
    """
    net_ids = _net_ids(board)
    pad_nets = {
        (ref, pad_number): net.name
        for net in board.nets
        for ref, pad_number in net.pins
    }
    lines = [
        "(kicad_pcb",
        f"\t(version {FILE_FORMAT_VERSION})",
        f'\t(generator "{GENERATOR}")',
        f'\t(generator_version "{GENERATOR_VERSION}")',
    ]
    lines.extend(_layers_block(board))
    lines.append('\t(net 0 "")')
    lines.extend(f'\t(net {net_ids[name]} "{_esc(name)}")' for name in sorted(net_ids))
    lines.extend(_outline_block(board))
    for placement in sorted(board.placements, key=lambda p: p.ref):
        lines.extend(_footprint_block(placement, net_ids, pad_nets))
    lines.append(")")
    return "\n".join(lines) + "\n"


def _net_ids(board: Board) -> dict[str, int]:
    """Net name → file net id: 1..N by lexicographic name (ADR-0010)."""
    names = [net.name for net in board.nets]
    if "" in names:
        raise ValueError(
            "board carries a net with an empty name — that id is reserved "
            "for net 0, the unconnected bucket; the board is malformed"
        )
    return {name: net_id for net_id, name in enumerate(sorted(names), start=1)}


def _layers_block(board: Board) -> list[str]:
    copper = _copper_layers(board.stackup)
    lines = ["\t(layers"]
    lines.extend(
        f'\t\t({2 * index} "{name}" signal)' for index, name in enumerate(copper)
    )
    lines.extend(f'\t\t({layer_id} "{name}" user)' for layer_id, name in _USER_LAYERS)
    lines.append("\t)")
    return lines


def _copper_layers(stackup: tuple[str, ...]) -> tuple[str, ...]:
    """Validate the stackup as a canonical copper sequence, front to back.

    File-format layer ids for copper are the even numbers in stackup order
    (F.Cu = 0, In1.Cu = 2, …, B.Cu = 2·(n−1)) — the scheme pcbnew's own
    2-layer output uses (F.Cu = 0, B.Cu = 2).
    """
    names = tuple(stackup)
    if (
        len(names) < 2
        or names[0] != "F.Cu"
        or names[-1] != "B.Cu"
        or not all(_COPPER_NAME.match(name) for name in names)
    ):
        raise ValueError(
            f"cannot export stackup {stackup!r}: expected a canonical copper "
            f"sequence F.Cu, In<k>.Cu…, B.Cu with at least two layers"
        )
    return names


def _outline_block(board: Board) -> list[str]:
    outline = board.outline
    return [
        "\t(gr_rect",
        f"\t\t(start {_pt(outline.min)})",
        f"\t\t(end {_pt(outline.max)})",
        f"\t\t(stroke (width {_mm(_OUTLINE_STROKE_NM)}) (type solid))",
        '\t\t(layer "Edge.Cuts")',
        "\t)",
    ]


def _footprint_block(
    placement: Placement, net_ids: dict[str, int], pad_nets: dict[tuple[str, str], str]
) -> list[str]:
    lines = [
        f'\t(footprint "Net2Board:{_esc(placement.footprint.entry_name)}"',
        f'\t\t(layer "{placement.side}")',
        f"\t\t(at {_mm(placement.pos.x)} {_mm(placement.pos.y)} {placement.rotation})",
    ]
    lines.extend(_courtyard_block(placement.footprint.courtyard, placement.side))
    for pad in placement.footprint.pads:
        lines.extend(_pad_block(placement.ref, pad, placement.side, net_ids, pad_nets))
    lines.append("\t)")
    return lines


def _courtyard_block(courtyard: Shape | None, side: str) -> list[str]:
    if courtyard is None:
        return []
    stroke = f"(stroke (width {_mm(_COURTYARD_STROKE_NM)}) (type solid))"
    if isinstance(courtyard, Rectangle):
        start, end = _mirrored_rect(courtyard, side)
        form, first = "fp_rect", ("start", start)
    else:
        form = "fp_circle"
        first = ("center", mirror_local(courtyard.center, side))
        end = mirror_local(courtyard.end, side)
    # The courtyard layer follows the placement side: the oracle pairs
    # courtyards per graphics layer (F.CrtYd never interacts with B.CrtYd,
    # probed on kicad-cli 10.0.6), so this is what makes cross-side pairs
    # exempt in the oracle exactly as our DRC exempts them.
    courtyard_layer = "F.CrtYd" if side == "F.Cu" else "B.CrtYd"
    return [
        f"\t\t({form}",
        f"\t\t\t({first[0]} {_pt(first[1])})",
        f"\t\t\t(end {_pt(end)})",
        f"\t\t\t{stroke}",
        f'\t\t\t(layer "{courtyard_layer}")',
        "\t\t)",
    ]


def _pad_block(
    ref: str,
    pad: Pad,
    side: str,
    net_ids: dict[str, int],
    pad_nets: dict[tuple[str, str], str],
) -> list[str]:
    pad_type = "thru_hole" if pad.drill is not None else "smd"
    net_name = pad_nets.get((ref, pad.number), "")
    lines = [
        f'\t\t(pad "{_esc(pad.number)}" {pad_type} {_esc(pad.shape_enum)}',
        f"\t\t\t(at {_pt(mirror_local(pad.local_pos, side))})",
        f"\t\t\t(size {_mm(pad.size[0])} {_mm(pad.size[1])})",
    ]
    if pad.drill is not None:
        lines.append(f"\t\t\t(drill {_mm(pad.drill)})")
    layers = " ".join(f'"{_pad_layer(token, side)}"' for token in pad.layers)
    lines.append(f"\t\t\t(layers {layers})")
    lines.append(f'\t\t\t(net {net_ids.get(net_name, 0)} "{_esc(net_name)}")')
    lines.append("\t\t)")
    return lines


def _pad_layer(token: str, side: str) -> str:
    """Remap F./B. layer prefixes on the back side (KiCad's flip)."""
    if side == "B.Cu" and token[:2] in ("F.", "B."):
        return ("B" if token[0] == "F" else "F") + token[1:]
    return token


def _mirrored_rect(rectangle: Rectangle, side: str) -> tuple[Point, Point]:
    normalized = Rectangle.from_corners(
        mirror_local(rectangle.min, side), mirror_local(rectangle.max, side)
    )
    return normalized.min, normalized.max


def _pt(point: Point) -> str:
    return f"{_mm(point.x)} {_mm(point.y)}"


def _mm(nanometres: int) -> str:
    """Render nm as mm with exactly six decimals, in pure int arithmetic.

    ``1 nm = 0.000001 mm`` exactly, so six decimals are lossless. Sign and
    digits are computed from integers: no float, hence no scientific
    notation and no ``-0.000000``.
    """
    sign = "-" if nanometres < 0 else ""
    magnitude = abs(nanometres)
    return f"{sign}{magnitude // 1_000_000}.{magnitude % 1_000_000:06d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
