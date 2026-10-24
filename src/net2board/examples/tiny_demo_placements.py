"""The demo board's human placement — the M3 human-baseline input (#73).

The KiCad 10 demo ``tinytapeout-demo.kicad_pcb`` (committed byte-verbatim
beside the fixture, Apache-2.0) carries the human placement M3's
acceptance grilling measures against: the solver must route at least as
well as it under identical rules (map #70). This module reads that
placement onto a fixture Board — harness-only, imported by tests and the
session examples, never a public importer face: the ``.kicad_pcb`` reader
exists to measure the human baseline, and a second public format would
reopen the ADR-0006/0015 line the map closed.

The transform is a pure translation, exactness throughout (ADR-0005):
the engine world is the y-down KiCad file frame rebased onto the demo
board's ``Edge.Cuts`` bbox min — the same 104.5 × 81 mm rectangle
``load_tiny_spec`` outlines — so anchor x/y translate by the bbox min,
sides copy verbatim, and rotation tokens copy modulo 360 (the demo
writes ``-90``; the model holds 270 — the identical transform).
``export_pcb`` writes anchors and rotation verbatim, so the round trip
reproduces the demo's placement nanometre-exact — pinned by test, not
asserted here.

Only netlist refs are read: the demo board's other footprints (logos,
stitching vias, fiducial-class parts) carry no netlist presence, are
absent from the fixture's Board by construction, and stay outside the
baseline the solver is judged on.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from net2board.geometry import Point
from net2board.ir._sexpr import read_sexpr
from net2board.ir.parsers import _mm_to_nm
from net2board.model import ROTATIONS, Board

__all__ = [
    "DEMO_PCB",
    "DemoRow",
    "apply_demo_placements",
    "demo_placements",
    "edge_origin",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_PCB = REPO_ROOT / "kicad_demo" / "tiny_tapeout" / "tinytapeout-demo.kicad_pcb"


class DemoRow(NamedTuple):
    """One human placement, in engine world coordinates."""

    ref: str
    x: int
    y: int
    rotation: int
    side: str


def _child(form: list, tag: str) -> list | None:
    for item in form:
        if isinstance(item, list) and item and item[0] == tag:
            return item
    return None


def _edge_origin_points(form: list) -> list[Point]:
    """Every ``Edge.Cuts`` vertex of one drawing form — the points that
    span the outline. Arc mids lie on the arc; circle centers do not lie
    on an outline, so an ``Edge.Cuts`` circle is refused rather than
    silently mis-read."""
    shape = form[0]
    layer = _child(form, "layer")
    if layer is None or layer[1] != "Edge.Cuts":
        return []
    if shape not in ("gr_line", "gr_rect", "gr_arc"):
        raise ValueError(f"unsupported Edge.Cuts shape {shape!r} in the demo board")
    points = []
    for tag in ("start", "end", "mid"):
        at = _child(form, tag)
        if at is not None:
            points.append(Point(_mm_to_nm(at[1]), _mm_to_nm(at[2])))
    return points


def edge_origin(demo_pcb: Path = DEMO_PCB) -> Point:
    """The demo board's ``Edge.Cuts`` bbox min — the rebase origin.

    Every anchor read below subtracts this, so the human placement lands
    in the outline-rebased world ``load_tiny_spec`` defines. The demo's
    rounded corners make the bbox corners arc vertices, exactly the
    104.5 × 81 mm rectangle the fixture states.
    """
    doc = read_sexpr(demo_pcb.read_text())
    if not (isinstance(doc, list) and doc[0] == "kicad_pcb"):
        raise ValueError(f"{demo_pcb} is not a .kicad_pcb document")
    points = [
        point
        for form in doc
        if isinstance(form, list) and form
        for point in _edge_origin_points(form)
    ]
    if not points:
        raise ValueError(f"{demo_pcb} has no Edge.Cuts geometry")
    return Point(min(p.x for p in points), min(p.y for p in points))


def _demo_rows(demo_pcb: Path, refs: frozenset[str]) -> dict[str, DemoRow]:
    """The demo board's netlist-ref placements — file data only. Refs
    outside ``refs`` are skipped unread: the demo's stitching vias share
    references among themselves, and none of them is the baseline's
    concern."""
    doc = read_sexpr(demo_pcb.read_text())
    origin = edge_origin(demo_pcb)
    rows: dict[str, DemoRow] = {}
    for form in doc:
        if not (isinstance(form, list) and form and form[0] == "footprint"):
            continue
        reference = None
        for item in form:
            if isinstance(item, list) and item[:2] == ["property", "Reference"]:
                reference = item[2]
        if reference is None or reference not in refs:
            continue
        if reference in rows:
            raise ValueError(f"reference {reference!r} appears twice in {demo_pcb}")
        layer = _child(form, "layer")
        at = _child(form, "at")
        if at is None or layer is None:
            raise ValueError(
                f"reference {reference!r} lacks layer/at on the demo board"
            )
        rotation = int(at[3]) % 360 if len(at) > 3 else 0
        if rotation not in ROTATIONS:
            raise ValueError(
                f"reference {reference!r} rotation {rotation} is not a 90° increment"
            )
        rows[reference] = DemoRow(
            ref=reference,
            x=_mm_to_nm(at[1]) - origin.x,
            y=_mm_to_nm(at[2]) - origin.y,
            rotation=rotation,
            side=layer[1],
        )
    return rows


def demo_placements(board: Board, demo_pcb: Path = DEMO_PCB) -> tuple[DemoRow, ...]:
    """The human placement for every component ``board`` declares —
    netlist refs only, sides and 90° rotations as found, in the board's
    own component order. A ref the demo board lacks raises: the fixture
    and the demo share one upstream project, and silence would misplace
    the baseline."""
    rows = _demo_rows(demo_pcb, frozenset(comp.ref for comp in board.components))
    placed = []
    for component in board.components:
        row = rows.get(component.ref)
        if row is None:
            raise ValueError(
                f"reference {component.ref!r} is in the netlist but not on the demo board"
            )
        placed.append(row)
    return tuple(placed)


def apply_demo_placements(board: Board, demo_pcb: Path = DEMO_PCB) -> Board:
    """The human baseline board: ``demo_placements`` stated one
    ``with_placement`` per row, the applier ``apply_placements`` mirrors."""
    for row in demo_placements(board, demo_pcb):
        board = board.with_placement(*row)
    return board
