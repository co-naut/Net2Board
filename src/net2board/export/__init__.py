"""Export — the canonical `.kicad_pcb` and DSN serializers.

``export_pcb(board) -> str`` is a pure function: no filesystem, no clock,
no randomness — the same Board always serializes to the same bytes.
Ordering is canonical everywhere it is observable: footprints by reference
designator, pads in library order, net ids assigned by net name sorted
lexicographically with ``(net 0 "")`` reserved for unconnected pads and
never entering the id sequence. Every footprint carries its Reference
identity property (ADR-0017) — the join key SES re-import matches on,
lean by design: rendering stays deferred. Millimetres render in pure
integer arithmetic to exactly six decimals — never scientific notation,
and ``-0.000000`` is impossible by construction.

Back-side placements bake the mirror into the emitted footprint-local
coordinates and emit their courtyard graphics on ``B.CrtYd``: KiCad
applies translate + rotate only to footprint-internal geometry on load
and pairs courtyards per graphics layer (both probed on kicad-cli
10.0.6, ADR-0006/0010), so the writer owns the left-right flip and the
side-selecting courtyard layer — together they make the oracle's
courtyard world identical to ``world_courtyard``'s.

``export_dsn(board, rules) -> str`` is the pure Freerouting handoff
(ADR-0017): Specctra floor only, routing rules a frozen argument and
never Board state. Frame conventions, all probed against pcbnew's own
writer, the pinned freerouting-2.4.1 jar, and ``ImportSpecctraSES``:
x verbatim, every y negated (the DSN frame is y-up) — image pins of
back-side placements included, which is what lands Freerouting's
``back`` placement on the same world our ``.kicad_pcb`` export
renders; rotation verbatim; ``(resolution um 1)`` with µm integers —
self-consistent, never the ``um 10`` divisor trap — and names quoted
only when a bare atom cannot represent them (quoted tokens parse under
Freerouting and ``read_sexpr`` with no ``string_quote`` declaration,
probed). Re-importing the SES needs the caller to drop the session's
placement echo first: ``ImportSpecctraSES`` re-places back-side
components from it and corrupts their world (probed at rot 0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from net2board.geometry import Point, Rectangle, Shape, mirror_local
from net2board.model import Board, Pad, Placement

__all__ = ["DsnRules", "export_dsn", "export_pcb"]

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
    """One footprint block, canonical KiCad 9/10 layout.

    ``locked`` is mobility between Boards (ADR-0012) and serializes as
    pcbnew's own first token line, ``(locked yes)``, before ``(layer …)`` —
    probed on kicad-cli 10.0.6 (parses; ``IsLocked()`` reads back True).
    Unlocked footprints carry no lock token, so the M1 bytes are
    unchanged.
    """
    lines = [
        f'\t(footprint "Net2Board:{_esc(placement.footprint.entry_name)}"',
    ]
    if placement.locked:
        lines.append("\t\t(locked yes)")
    lines.extend(
        [
            f'\t\t(layer "{placement.side}")',
            f"\t\t(at {_mm(placement.pos.x)} {_mm(placement.pos.y)} {placement.rotation})",
        ]
    )
    lines.extend(_reference_property_block(placement.ref, placement.side))
    lines.extend(_courtyard_block(placement.footprint.courtyard, placement.side))
    for pad in placement.footprint.pads:
        lines.extend(_pad_block(placement.ref, pad, placement.side, net_ids, pad_nets))
    lines.append("\t)")
    return lines


def _reference_property_block(ref: str, side: str) -> list[str]:
    """The Reference identity property — refs, not silkscreen rendering.

    ``ImportSpecctraSES`` matches SES items by reference designator
    (#22), and the reader takes the name from this property; its text
    placement (anchor, no effects/uuid) is the leanest form pcbnew
    round-trips. The layer follows the placement side like the courtyard
    graphics layer does.
    """
    silk_layer = "F.SilkS" if side == "F.Cu" else "B.SilkS"
    return [
        f'\t\t(property "Reference" "{_esc(ref)}"',
        "\t\t\t(at 0 0 0)",
        f'\t\t\t(layer "{silk_layer}")',
        "\t\t)",
    ]


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


# ---------------------------------------------------------------------------
# DSN — the pure Freerouting handoff (ADR-0017)
# ---------------------------------------------------------------------------

# The official KiCad plugin strips these before routing (its
# search_n_strip, integrations/KiCad/plugins/router_dsn.py:30); we strip
# at write time so every emitted name is router-safe everywhere it
# appears. Stripping may collide (Rµ1 vs R1) — the caller's names, the
# caller's risk; the alternative is rejecting boards KiCad itself routes.
_DSN_STRIPPED = "ΩµΦ"

# Characters that survive as a bare atom. Conservative superset of what
# refs, net names, and image names actually carry; anything else forces
# quoting. ':' stays bare — pcbnew's own image names use it (::1).
_DSN_BARE = re.compile(r"\A[A-Za-z0-9_.+\-/:#]+\Z")

_DSN_BOARD_NAME = "net2board"
_DSN_CLASS_NAME = "DEFAULT"


@dataclass(frozen=True)
class DsnRules:
    """The frozen routing-rules argument to ``export_dsn`` (ADR-0017).

    One width, one clearance, one via padstack — deliberately not Board
    state: identical copper routed under different rules must not compare
    equal, and per-net widths would mean NetClass, which the M2 map holds
    out of scope. Defaults are the probed research set (minimal DSN that
    routed clean under freerouting-2.4.1); nanometres, like every other
    engine coordinate.
    """

    width_nm: int = 250_000
    clearance_nm: int = 250_000
    via_diameter_nm: int = 800_000

    def __post_init__(self) -> None:
        if min(self.width_nm, self.clearance_nm, self.via_diameter_nm) <= 0:
            raise ValueError(
                f"DsnRules must be positive: width {self.width_nm} nm, "
                f"clearance {self.clearance_nm} nm, via diameter "
                f"{self.via_diameter_nm} nm"
            )


# A frozen singleton — documented defaults, shareable as a default arg.
_DEFAULT_DSN_RULES = DsnRules()


def export_dsn(board: Board, rules: DsnRules = _DEFAULT_DSN_RULES) -> str:
    """Serialize ``board`` to canonical Specctra DSN text for Freerouting.

    Placements only — unplaced components are silently ignored by the
    router (probed), and a net left with no placed pins is dropped rather
    than emitted empty. Serializer only: no jar, no log, no SES — the
    entire engine-side Freerouting surface (ADR-0017).
    """
    copper = _copper_layers(board.stackup)
    placements = sorted(board.placements, key=lambda p: p.ref)
    placed_refs = {placement.ref for placement in placements}
    if "" in {net.name for net in board.nets}:
        raise ValueError(
            "board carries a net with an empty name — the DSN network "
            "block cannot represent it; the board is malformed"
        )
    padstacks: dict[str, list[str]] = {}
    via_name = _via_padstack(padstacks, rules, copper)
    images: dict[str, list[str]] = {}
    component_lines: list[str] = []
    for image_name, image_placements in _grouped_by_image(placements):
        pin_lines = _image_pins(image_placements[0], padstacks, copper)
        images[image_name] = pin_lines
        places = [
            f"\t\t\t(place {_bare_ref(p.ref)} {_um(p.pos.x)} {_um(-p.pos.y)} "
            f"{'front' if p.side == 'F.Cu' else 'back'} {p.rotation})"
            for p in image_placements
        ]
        component_lines.append(
            [f"\t\t(component {_dsn_token(image_name)}", *places, "\t\t)"]
        )
    net_lines, net_tokens = _network_lines(board, placed_refs)
    lines = [
        f"(pcb {_DSN_BOARD_NAME}",
        "\t(resolution um 1)",
        "\t(structure",
        *(f"\t\t(layer {name} (type signal))" for name in copper),
        *_boundary_lines(board.outline),
        f"\t\t(via {via_name})",
        *_rule_lines(rules),
        "\t)",
        "\t(placement",
        *(line for block in component_lines for line in block),
        "\t)",
        "\t(library",
        *(
            line
            for name in sorted(images)
            for line in (
                f"\t\t(image {_dsn_token(name)}",
                *images[name],
                "\t\t)",
            )
        ),
        *(
            line
            for name in sorted(padstacks)
            for line in (
                f"\t\t(padstack {name}",
                *padstacks[name],
                "\t\t)",
            )
        ),
        "\t)",
        "\t(network",
        *net_lines,
        *_class_lines(net_tokens, via_name, rules),
        "\t)",
        ")",
    ]
    return "\n".join(lines) + "\n"


def _um(nanometres: int) -> int:
    """nm → µm integer, half away from zero, in pure int arithmetic.

    DSN carries µm under ``(resolution um 1)``; sub-µm nanometres round
    deterministically (never a float).
    """
    magnitude = abs(nanometres) + 500
    micros = magnitude // 1000
    return -micros if nanometres < 0 else micros


def _sanitize(name: str) -> str:
    return "".join(char for char in name if char not in _DSN_STRIPPED)


def _dsn_token(name: str) -> str:
    """Render a name as a bare atom when possible, else a quoted string.

    Quoted tokens parse under Freerouting and ``read_sexpr`` with no
    ``(string_quote ...)`` declaration (probed on both). A name carrying
    a literal ``"`` is unrepresentable — the DSN string syntax has no
    escape we are willing to invent.
    """
    cleaned = _sanitize(name)
    if '"' in cleaned:
        raise ValueError(
            f"name {name!r} contains a double quote — not representable "
            f"in DSN; rename the entity"
        )
    if _DSN_BARE.match(cleaned):
        return cleaned
    return '"' + cleaned.replace("\\", "\\\\") + '"'


def _bare_ref(ref: str) -> str:
    """A reference designator as it must appear everywhere: bare.

    A pin id is the single atom ``<ref>-<pad>`` (``(pins R1-2 ...)``),
    so a ref that cannot live in a bare atom has no DSN form at all.
    """
    cleaned = _sanitize(ref)
    if not _DSN_BARE.match(cleaned):
        raise ValueError(
            f"reference designator {ref!r} is not representable as a DSN "
            f"pin id after sanitization ({cleaned!r})"
        )
    return cleaned


def _pin_id(ref: str, pad_number: str) -> str:
    pad = _sanitize(pad_number)
    pin_id = f"{_bare_ref(ref)}-{pad}"
    if not _DSN_BARE.match(pin_id):
        raise ValueError(
            f"pad number {pad_number!r} on {ref!r} is not representable "
            f"in a DSN pin id ({pin_id!r})"
        )
    return pin_id


def _pad_copper_layers(pad: Pad, side: str, copper: tuple[str, ...]) -> tuple[str, ...]:
    """The pad's copper layers, side-remapped, in stackup order.

    ``*.Cu`` (and bare ``*``) expand to the whole stackup — the thru-hole
    case; everything else keeps only tokens that name a stackup layer.
    """
    wanted: set[str] = set()
    for token in pad.layers:
        token = _pad_layer(token, side)
        if token in ("*.Cu", "*"):
            return copper
        if token in copper:
            wanted.add(token)
    return tuple(name for name in copper if name in wanted)


def _padstack_name(pad: Pad, layers: tuple[str, ...]) -> tuple[str, str]:
    """The pad's padstack name plus its geometry kind, deduplicated.

    The name derives from the emitted geometry only — a ``roundrect``
    falling back to its bounding rect shares the ``rect`` padstack, which
    is correct: identical copper, identical padstack.
    """
    kind = (
        "circle"
        if pad.shape_enum == "circle"
        else ("oval" if pad.shape_enum == "oval" else "rect")
    )
    sx, sy = _um(pad.size[0]), _um(pad.size[1])
    short = "-".join(name[: -len(".Cu")] for name in layers)
    return f"Pad_{kind}_{sx}x{sy}_{short}", kind


def _padstack_shape_lines(pad: Pad, layers: tuple[str, ...], kind: str) -> list[str]:
    """Shape forms per copper layer — pcbnew's own encodings.

    circle → diameter ``size[0]``; oval → pcbnew's path encoding (width
    is the narrow axis, a centered segment spans the long axis's excess,
    degenerate for squares); anything else → the bounding rect, a
    conservative copper superset (roundrect, custom, trapezoid …).
    """
    sx, sy = _um(pad.size[0]), _um(pad.size[1])
    shapes: list[str] = []
    for layer in layers:
        if kind == "circle":
            shapes.append(f"\t\t\t(shape (circle {layer} {sx}))")
        elif kind == "oval":
            width = min(sx, sy)
            if sx > sy:
                excess = sx - sy
                points = f"{-(excess // 2)} 0 {excess - excess // 2} 0"
            elif sy > sx:
                excess = sy - sx
                points = f"0 {-(excess // 2)} 0 {excess - excess // 2}"
            else:
                points = "0 0 0 0"
            shapes.append(f"\t\t\t(shape (path {layer} {width} {points}))")
        else:
            x0, x1 = -(sx // 2), sx - sx // 2
            y0, y1 = -(sy // 2), sy - sy // 2
            shapes.append(f"\t\t\t(shape (rect {layer} {x0} {y0} {x1} {y1}))")
    return shapes


def _image_pins(
    placement: Placement, padstacks: dict[str, list[str]], copper: tuple[str, ...]
) -> list[str]:
    """Pin lines for an image — footprint-local, front-canonical.

    Every pin negates y like every DSN coordinate, front and back
    alike: Freerouting's ``back`` placement lands the front-canonical
    image exactly on the model's world (probed both ways — a rot-90
    back placement with off-axis pads routes onto the pads our
    ``.kicad_pcb`` export renders, and the y-un-negated variant leaves
    its nets dangling; the acceptance C1, back at rot 0 with y=0 pads,
    is blind to the distinction). Pads without copper layers have
    nothing for a router and are skipped.
    """
    pins: list[str] = []
    for pad in placement.footprint.pads:
        layers = _pad_copper_layers(pad, placement.side, copper)
        if not layers:
            continue
        name, kind = _padstack_name(pad, layers)
        padstacks.setdefault(name, _padstack_shape_lines(pad, layers, kind))
        pins.append(
            f"\t\t\t(pin {name} {_dsn_token(pad.number)} "
            f"{_um(pad.local_pos.x)} {_um(-pad.local_pos.y)})"
        )
    return pins


def _grouped_by_image(
    placements: list[Placement],
) -> list[tuple[str, list[Placement]]]:
    """Group placements into shareable images: (entry name, side).

    An image's pins reference side-remapped padstacks, so the same entry
    on both sides yields two images — the front keeps the bare entry
    name (the common case), the back is suffixed. Pcbnew solves the same
    problem by minting per-instance ``::1``/``::2`` images.
    """
    groups: dict[tuple[str, str], list[Placement]] = {}
    for placement in placements:
        key = (placement.footprint.entry_name, placement.side)
        groups.setdefault(key, []).append(placement)
    named = [
        (
            entry if side == "F.Cu" else f"{entry}::back",
            members,
        )
        for (entry, side), members in groups.items()
    ]
    return sorted(named, key=lambda pair: pair[0])


def _via_padstack(
    padstacks: dict[str, list[str]], rules: DsnRules, copper: tuple[str, ...]
) -> str:
    diameter = _um(rules.via_diameter_nm)
    name = f"Via_{diameter}"
    padstacks[name] = [f"\t\t\t(shape (circle {layer} {diameter}))" for layer in copper]
    return name


def _boundary_lines(outline: Rectangle) -> list[str]:
    """The board outline as Specctra's one hard-required structure block.

    Closed five-point polygon from the min corner; y negated like every
    other DSN coordinate.
    """
    points = (
        (outline.min.x, outline.min.y),
        (outline.max.x, outline.min.y),
        (outline.max.x, outline.max.y),
        (outline.min.x, outline.max.y),
        (outline.min.x, outline.min.y),
    )
    rendered = " ".join(f"{_um(x)} {_um(-y)}" for x, y in points)
    return ["\t\t(boundary", f"\t\t\t(path pcb 0 {rendered})", "\t\t)"]


def _rule_lines(rules: DsnRules) -> list[str]:
    return [
        "\t\t(rule",
        f"\t\t\t(width {_um(rules.width_nm)})",
        f"\t\t\t(clearance {_um(rules.clearance_nm)})",
        "\t\t)",
    ]


def _network_lines(board: Board, placed_refs: set[str]) -> tuple[list[str], list[str]]:
    """Net forms plus their rendered name tokens (the class reuses them).

    Nets sort by name, pins by ``(ref, pad)`` — canonical regardless of
    netlist ordering. A net left with no placed pin is dropped rather
    than emitted empty; its unplaced work has no router-visible form.
    """
    lines: list[str] = []
    tokens: list[str] = []
    for net in sorted(board.nets, key=lambda net: net.name):
        pins = sorted((ref, pad) for ref, pad in net.pins if ref in placed_refs)
        if not pins:
            continue
        token = _dsn_token(net.name)
        tokens.append(token)
        lines.append(f"\t\t(net {token}")
        lines.append(
            "\t\t\t(pins " + " ".join(_pin_id(ref, pad) for ref, pad in pins) + ")"
        )
        lines.append("\t\t)")
    return lines, tokens


def _class_lines(net_tokens: list[str], via_name: str, rules: DsnRules) -> list[str]:
    """The DEFAULT class: how the router learns its via and its rules.

    Structure-level ``(via …)`` declares the padstack; ``use_via`` is
    what makes the router actually reach for it (the ablation: dropping
    either routes nothing), and the class rule is where the width lands
    in the SES wires (probed: 250 in, 250 out).
    """
    if not net_tokens:
        return []
    names = " ".join(net_tokens)
    return [
        f"\t\t(class {_DSN_CLASS_NAME} {names}",
        "\t\t\t(circuit",
        f"\t\t\t\t(use_via {via_name})",
        "\t\t\t)",
        "\t\t\t(rule",
        f"\t\t\t\t(width {_um(rules.width_nm)})",
        f"\t\t\t\t(clearance {_um(rules.clearance_nm)})",
        "\t\t\t)",
        "\t\t)",
    ]
