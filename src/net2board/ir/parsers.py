"""KiCad ``.net`` and ``.kicad_mod`` parsers → IR (ADR-0007).

Faithful syntax→IR translation over the s-expression reader: components,
nets, and libpart keys from the netlist; pads and the ``F.CrtYd`` courtyard
from each footprint. Millimetres convert to integer nanometres exactly via
``Decimal`` — a coordinate off the nanometre grid raises rather than rounds
(ADR-0005). Semantic load failures raise the structured hierarchy from
``boardspec`` (ADR-0008): a comp with no footprint token is a
``FootprintResolveError``; a CrtYd graphic outside the parse vocabulary
(circle, rect, axis-aligned line polyline) is an ``UnsupportedCourtyardShape``.

Courtyard parsing meets real KiCad libraries where they are (ADR-0015): a
line-polyline courtyard — modern libraries draw the rectangle *with a pin-1
notch*, 12–20 ``fp_line``s — reduces to its axis-aligned bounding rectangle,
a conservative superset of the upstream keep-out (never smaller, so never a
legalization under-estimate; the exporter emits the same rectangle, keeping
the ``kicad-cli`` oracle in agreement by construction). The model's
``Shape = Rectangle | Circle`` vocabulary is unchanged (ADR-0006). A
footprint's internal name may carry a library nickname prefix
(``"Lib:Entry"``); it is stripped exactly as netlist comp footprints are
(the loader's nickname-strip rule).

Two tolerances keep every netlist-referenced footprint parseable: oval-drill
widths are dropped (the spec-pinned ``PadIR`` carries round-drill
diameters only), and — since #73 — pad ``local_rotation`` is carried
rather than dropped. Pad local angles are library-local (ADR-0022: the
fixture's SOIC-8 "270° pads" were the demo board's U7 instance angle,
board-absolute, baked into a library file); an angle off the 90°
multiples raises at load — the ADR-0015 posture — rather than checking
pad geometry silently wrong. The raise retires one committed library
file: the unreferenced ``Valve_ECC-83-2`` alternative (a valve socket
with 18°–342° pads) no longer parses. Everything else unrepresentable —
drill offsets, off-grid coordinates, diagonal or non-line CrtYd
graphics — still raises.
"""

from __future__ import annotations

from decimal import Decimal
from math import isqrt

from net2board.boardspec import (
    FootprintResolveError,
    UnsupportedCourtyardShape,
    UnsupportedPadPrimitive,
    UnsupportedPadRotation,
    UnsupportedPadType,
)
from net2board.geometry import Circle, Point, Rectangle, Shape
from net2board.ir import (
    CompIR,
    FootprintIR,
    LibpartIR,
    NetIR,
    NetlistIR,
    NodeIR,
    PadIR,
)
from net2board.ir._sexpr import read_sexpr

__all__ = ["parse_footprint", "parse_netlist"]

_COURTYARD_LAYER = "F.CrtYd"
_COURTYARD_TAGS = frozenset(
    {"fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_ellipse", "fp_curve"}
)


def parse_netlist(text: str) -> NetlistIR:
    """Parse a KiCad s-expression netlist (``.net``)."""
    export = _top_form(text, "export")
    comps_form = _child(export, "components")
    comps = tuple(_parse_comp(comp_form) for comp_form in _children(comps_form, "comp"))
    nets = tuple(
        _parse_net(net_form) for net_form in _children(_child(export, "nets"), "net")
    )
    libparts_form = _child(export, "libparts", required=False)
    libparts = (
        tuple(
            LibpartIR(
                lib=_string(libpart_form, "lib"), part=_string(libpart_form, "part")
            )
            for libpart_form in _children(libparts_form, "libpart")
        )
        if libparts_form is not None
        else ()
    )
    return NetlistIR(comps=comps, nets=nets, libparts=libparts)


def parse_footprint(text: str) -> FootprintIR:
    """Parse a KiCad library footprint file (``.kicad_mod``)."""
    form = _top_form(text, "footprint")
    if len(form) < 2 or not isinstance(form[1], str):
        raise ValueError("footprint form has no name string")
    entry_name = form[1].rsplit(":", 1)[-1]
    pads = tuple(
        _parse_pad(pad_form, entry_name) for pad_form in _children(form, "pad")
    )
    courtyard = _parse_courtyard(form, entry_name)
    return FootprintIR(
        entry_name=entry_name,
        pads=pads,
        courtyard=courtyard,
        net_tie_groups=_parse_net_tie_groups(form),
    )


_NET_TIE_TAG = "net_tie_pad_groups"


def _parse_net_tie_groups(form: list) -> tuple[frozenset[str], ...]:
    """The net-tie pad groups, KiCad's grammar (the s-expression format
    reference): a space-separated list of quoted strings, each a
    comma-separated pad-number list — ``"1, 2" "A,B"`` is two groups.
    Pads in one group never violate each other (ADR-0022's second
    exemption)."""
    token = _child(form, _NET_TIE_TAG, required=False)
    if token is None:
        return ()
    groups = []
    for child in token[1:]:
        if not isinstance(child, str) or not child:
            raise ValueError(f"malformed ({_NET_TIE_TAG} …) entry")
        groups.append(frozenset(name.strip() for name in child.split(",")))
    return tuple(groups)


def _parse_comp(comp_form: list) -> CompIR:
    ref = _string(comp_form, "ref")
    raw_footprint = _string(comp_form, "footprint", required=False)
    if raw_footprint is None:
        raise FootprintResolveError(ref=ref)
    return CompIR(ref=ref, entry_name=raw_footprint.rsplit(":", 1)[-1])


def _parse_net(net_form: list) -> NetIR:
    nodes = tuple(
        NodeIR(ref=_string(node_form, "ref"), pin=_string(node_form, "pin"))
        for node_form in _children(net_form, "node")
    )
    return NetIR(name=_string(net_form, "name"), nodes=nodes)


_PAD_TYPES = ("smd", "thru_hole")
_HULL_PRIMITIVES = ("gr_circle", "gr_rect", "gr_poly", "gr_line")


def _parse_pad(pad_form: list, entry_name: str) -> PadIR:
    if (
        len(pad_form) < 4
        or not isinstance(pad_form[1], str)
        or not isinstance(pad_form[2], str)
        or not isinstance(pad_form[3], str)
    ):
        raise ValueError(f"malformed pad in footprint {entry_name!r}")
    number, pad_type, shape_enum = pad_form[1], pad_form[2], pad_form[3]
    if pad_type not in _PAD_TYPES:
        raise UnsupportedPadType(
            entry_name=entry_name, pad_number=number, pad_type=pad_type
        )
    at_form = _child(pad_form, "at")
    size_form = _child(pad_form, "size")
    drill_form = _child(pad_form, "drill", required=False)
    layers_form = _child(pad_form, "layers")
    local_rotation = int(at_form[3]) % 360 if len(at_form) > 3 else 0
    if local_rotation % 90 != 0:
        raise UnsupportedPadRotation(
            entry_name=entry_name, pad_number=number, angle=local_rotation
        )
    return PadIR(
        number=number,
        local_pos=Point(_mm_to_nm(at_form[1]), _mm_to_nm(at_form[2])),
        size=(_mm_to_nm(size_form[1]), _mm_to_nm(size_form[2])),
        drill=_parse_drill(drill_form, number, entry_name),
        layers=tuple(token for token in layers_form[1:] if isinstance(token, str)),
        shape_enum=shape_enum,
        local_rotation=local_rotation,
        roundrect_rratio=_parse_roundrect_ratio(pad_form),
        hull=_parse_pad_hull(pad_form, number, shape_enum, entry_name),
    )


def _parse_roundrect_ratio(pad_form: list) -> Decimal | None:
    """The corner ratio as the library states it — ``None`` when absent
    (the capsule defaults it to KiCad's own 0.25)."""
    token = _child(pad_form, "roundrect_rratio", required=False)
    if token is None:
        return None
    try:
        return Decimal(token[1])
    except ArithmeticError:
        raise ValueError(f"malformed (roundrect_rratio …) form: {token!r}") from None


def _parse_pad_hull(
    pad_form: list, number: str, shape_enum: str, entry_name: str
) -> Rectangle | None:
    """A ``custom`` pad's copper extent: the pad-local bounding rectangle
    of its primitives (ADR-0022 — the anchor box alone under-covers; the
    JP solder jumpers' real copper reaches ±0.75 mm where the anchor
    reaches ±0.25). Stroke widths grow the hull, non-integer radii ceil —
    never under-cover; an unparseable primitive kind raises at load."""
    if shape_enum != "custom":
        return None
    primitives = _child(pad_form, "primitives", required=False)
    if primitives is None:
        return None
    boxes = [
        _hull_box(primitive, number, entry_name)
        for primitive in primitives[1:]
        if isinstance(primitive, list) and primitive
    ]
    if not boxes:
        return None
    min_x = min(box.min.x for box in boxes)
    min_y = min(box.min.y for box in boxes)
    max_x = max(box.max.x for box in boxes)
    max_y = max(box.max.y for box in boxes)
    return Rectangle(min=Point(min_x, min_y), max=Point(max_x, max_y))


def _hull_box(primitive: list, number: str, entry_name: str) -> Rectangle:
    """One primitive's pad-local bounding rectangle, grown by half its
    stroke width when the primitive carries one — the stroke is copper
    too, and a stroke never leaves the vertex box grown by half its
    width (the never-under-cover claim)."""
    kind = primitive[0]
    if kind not in _HULL_PRIMITIVES:
        raise UnsupportedPadPrimitive(
            entry_name=entry_name, pad_number=number, primitive=kind
        )
    if kind == "gr_circle":
        center = _point(primitive, "center")
        end = _point(primitive, "end")
        radius_sq = (end.x - center.x) ** 2 + (end.y - center.y) ** 2
        radius = _ceil_isqrt(radius_sq)
        box = Rectangle(
            min=Point(center.x - radius, center.y - radius),
            max=Point(center.x + radius, center.y + radius),
        )
    elif kind == "gr_rect":
        box = Rectangle.from_corners(
            _point(primitive, "start"), _point(primitive, "end")
        )
    elif kind == "gr_poly":
        pts_form = _child(primitive, "pts")
        points = [
            Point(_mm_to_nm(pts[1]), _mm_to_nm(pts[2]))
            for pts in _children(pts_form, "xy")
        ]
        if not points:
            raise ValueError(
                f"pad {number!r} of footprint {entry_name!r}: a gr_poly "
                f"primitive without points"
            )
        box = Rectangle(
            min=Point(min(p.x for p in points), min(p.y for p in points)),
            max=Point(max(p.x for p in points), max(p.y for p in points)),
        )
    else:
        box = Rectangle.from_corners(
            _point(primitive, "start"), _point(primitive, "end")
        )
    width_form = _child(primitive, "width", required=False)
    if width_form is None:
        return box
    half_stroke = _ceil_half(_mm_to_nm(width_form[1]))
    return Rectangle(
        min=Point(box.min.x - half_stroke, box.min.y - half_stroke),
        max=Point(box.max.x + half_stroke, box.max.y + half_stroke),
    )


def _ceil_isqrt(square: int) -> int:
    """⌈√square⌉ — the smallest integer radius covering the circle."""
    root = isqrt(square)
    return root if root * root == square else root + 1


def _ceil_half(value: int) -> int:
    """⌈value/2⌉ — a half-nanometre stroke half-width ceils."""
    return (value + 1) // 2


def _parse_drill(drill_form: list | None, number: str, entry_name: str) -> int | None:
    if drill_form is None:
        return None
    atoms = [token for token in drill_form[1:] if not isinstance(token, list)]
    diameters = [token for token in atoms if token != "oval"]
    if not diameters:
        raise ValueError(f"malformed (drill …) form in footprint {entry_name!r}")
    if _child(drill_form, "offset", required=False) is not None:
        raise ValueError(
            f"pad {number!r} of footprint {entry_name!r}: drill offsets are not "
            f"representable in M1"
        )
    return _mm_to_nm(diameters[0])


def _parse_courtyard(form: list, entry_name: str) -> Shape | None:
    items = [
        child
        for child in form[1:]
        if isinstance(child, list)
        and child
        and child[0] in _COURTYARD_TAGS
        and _string(child, "layer", required=False) == _COURTYARD_LAYER
    ]
    if not items:
        return None
    if len(items) == 1 and items[0][0] == "fp_circle":
        return _circle_from(items[0])
    if len(items) == 1 and items[0][0] == "fp_rect":
        return _rect_from_corners(_point(items[0], "start"), _point(items[0], "end"))
    if len(items) >= 4 and all(item[0] == "fp_line" for item in items):
        return _rect_from_lines(items, entry_name)
    raise UnsupportedCourtyardShape(entry_name=entry_name)


def _circle_from(circle_form: list) -> Circle:
    center = _point(circle_form, "center")
    end = _point(circle_form, "end")
    return Circle(
        center=center,
        radius_sq=(end.x - center.x) ** 2 + (end.y - center.y) ** 2,
        end=end,
    )


def _rect_from_corners(a: Point, b: Point) -> Rectangle:
    return Rectangle.from_corners(a, b)


def _rect_from_lines(items: list, entry_name: str) -> Rectangle:
    """The axis-aligned bounding rectangle of a line-polyline courtyard.

    Real KiCad libraries draw courtyards as a rectangle with a pin-1 notch
    (12–20 ``fp_line``s); four closed lines are the special case the M1
    fixture used. The hull is a conservative superset of the upstream
    keep-out (ADR-0015): every segment must be axis-aligned — a diagonal
    one cannot be bounded exactly by an axis-aligned pair and raises.
    """
    xs: set[int] = set()
    ys: set[int] = set()
    for line in items:
        start = _point(line, "start")
        end = _point(line, "end")
        if start.x != end.x and start.y != end.y:
            raise UnsupportedCourtyardShape(entry_name=entry_name)
        xs.update((start.x, end.x))
        ys.update((start.y, end.y))
    return Rectangle(min=Point(min(xs), min(ys)), max=Point(max(xs), max(ys)))


def _point(form: list, tag: str) -> Point:
    coordinates = _child(form, tag)
    return Point(_mm_to_nm(coordinates[1]), _mm_to_nm(coordinates[2]))


def _mm_to_nm(token: str) -> int:
    try:
        nanometres = Decimal(token) * 1_000_000
    except ArithmeticError:
        raise ValueError(f"coordinate {token!r} is not a number") from None
    if not nanometres.is_finite():
        raise ValueError(f"coordinate {token!r} is not a finite number")
    if nanometres != nanometres.to_integral_value():
        raise ValueError(
            f"coordinate {token!r} does not land on an exact integer nanometre"
        )
    return int(nanometres)


def _top_form(text: str, tag: str) -> list:
    form = read_sexpr(text)
    if not (isinstance(form, list) and form and form[0] == tag):
        raise ValueError(f"expected a top-level ({tag} …) form")
    return form


def _child(form: list, tag: str, *, required: bool = True) -> list | None:
    for child in form[1:]:
        if isinstance(child, list) and child and child[0] == tag:
            return child
    if required:
        raise ValueError(f"missing ({tag} …) form")
    return None


def _children(form: list, tag: str) -> list[list]:
    return [
        child
        for child in form[1:]
        if isinstance(child, list) and child and child[0] == tag
    ]


def _string(form: list, tag: str, *, required: bool = True) -> str | None:
    child = _child(form, tag, required=required)
    if child is None:
        return None
    if len(child) < 2 or not isinstance(child[1], str):
        raise ValueError(f"malformed ({tag} …) form")
    return child[1]
