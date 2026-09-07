"""KiCad ``.net`` and ``.kicad_mod`` parsers → IR (ADR-0007).

Faithful syntax→IR translation over the s-expression reader: components,
nets, and libpart keys from the netlist; pads and the ``F.CrtYd`` courtyard
from each footprint. Millimetres convert to integer nanometres exactly via
``Decimal`` — a coordinate off the nanometre grid raises rather than rounds
(ADR-0005). Semantic load failures raise the structured hierarchy from
``boardspec`` (ADR-0008): a comp with no footprint token is a
``FootprintResolveError``; a CrtYd graphic outside the M1 vocabulary
(axis-aligned rectangle, circle) is an ``UnsupportedCourtyardShape``.

Two tolerances keep the whole committed library parseable: pad-local
rotation angles and oval-drill widths are dropped (the spec-pinned ``PadIR``
carries position and round-drill diameter only). No footprint referenced by
the M1 netlist uses either; unreferenced library alternatives (e.g.
``Valve_ECC-83-2``) do. Everything else unrepresentable — drill offsets,
off-grid coordinates — still raises.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from net2board.boardspec import FootprintResolveError, UnsupportedCourtyardShape
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
    entry_name = form[1]
    pads = tuple(
        _parse_pad(pad_form, entry_name) for pad_form in _children(form, "pad")
    )
    courtyard = _parse_courtyard(form, entry_name)
    return FootprintIR(entry_name=entry_name, pads=pads, courtyard=courtyard)


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


def _parse_pad(pad_form: list, entry_name: str) -> PadIR:
    if (
        len(pad_form) < 4
        or not isinstance(pad_form[1], str)
        or not isinstance(pad_form[3], str)
    ):
        raise ValueError(f"malformed pad in footprint {entry_name!r}")
    number, shape_enum = pad_form[1], pad_form[3]
    at_form = _child(pad_form, "at")
    size_form = _child(pad_form, "size")
    drill_form = _child(pad_form, "drill", required=False)
    layers_form = _child(pad_form, "layers")
    return PadIR(
        number=number,
        local_pos=Point(_mm_to_nm(at_form[1]), _mm_to_nm(at_form[2])),
        size=(_mm_to_nm(size_form[1]), _mm_to_nm(size_form[2])),
        drill=_parse_drill(drill_form, number, entry_name),
        layers=tuple(token for token in layers_form[1:] if isinstance(token, str)),
        shape_enum=shape_enum,
    )


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
    if len(items) == 4 and all(item[0] == "fp_line" for item in items):
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
    endpoint_counts: Counter[Point] = Counter()
    xs: set[int] = set()
    ys: set[int] = set()
    for line in items:
        start = _point(line, "start")
        end = _point(line, "end")
        if start.x != end.x and start.y != end.y:
            raise UnsupportedCourtyardShape(entry_name=entry_name)
        endpoint_counts[start] += 1
        endpoint_counts[end] += 1
        xs.update((start.x, end.x))
        ys.update((start.y, end.y))
    if (
        len(endpoint_counts) == 4
        and set(endpoint_counts.values()) == {2}
        and len(xs) == 2
        and len(ys) == 2
    ):
        return Rectangle(min=Point(min(xs), min(ys)), max=Point(max(xs), max(ys)))
    raise UnsupportedCourtyardShape(entry_name=entry_name)


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
