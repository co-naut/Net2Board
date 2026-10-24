"""#73 — the human baseline's input: the demo board's placement, read.

The committed ``tinytapeout-demo.kicad_pcb`` (byte-verbatim from the
KiCad 10 demo install, Apache-2.0) holds the human placement M3's
acceptance grilling measures against. The reader is harness-only
(``net2board.examples.tiny_demo_placements`` — never a public importer
face), the transform a pure translation onto the outline-rebased world,
rotation tokens copied mod 360 (the demo writes ``-90``, the model holds
270 — the identical transform). Pinned here: provenance, the charted
facts (150 netlist refs resolved; sides and rotations as found — J11
alone on B.Cu among the netlist parts), and the lossless round trip:
apply → ``export_pcb`` → re-read reproduces the demo's anchors
nanometre-exact.
"""

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import TINY_OUTLINE, load_tiny_spec
from net2board.build import build_board
from net2board.examples.tiny_demo_placements import (
    DEMO_PCB,
    apply_demo_placements,
    demo_placements,
    edge_origin,
)
from net2board.geometry import Point
from net2board.ir._sexpr import read_sexpr
from net2board.model import ROTATIONS

UPSTREAM_DEMO = Path("/usr/share/kicad/demos/tiny_tapeout/tinytapeout-demo.kicad_pcb")
ORIGIN = Point(56_500_000, 53_500_000)
TINY_BOARD = build_board(load_tiny_spec())
ROWS = demo_placements(TINY_BOARD)

ROTATION_HISTOGRAM = {0: 54, 90: 30, 180: 45, 270: 21}


def footprint_placements(text):
    """Every footprint placement of a board text by ref — the reader's
    own conventions mirrored, for byte-exact comparisons."""
    doc = read_sexpr(text)
    placed = {}
    for form in doc:
        if not (isinstance(form, list) and form and form[0] == "footprint"):
            continue
        at = next(k for k in form if isinstance(k, list) and k[:1] == ["at"])
        layer = next(k for k in form if isinstance(k, list) and k[:1] == ["layer"])
        reference = next(
            item[2]
            for item in form
            if isinstance(item, list) and item[:2] == ["property", "Reference"]
        )
        placed[reference] = (
            at[1],
            at[2],
            int(at[3]) if len(at) > 3 else 0,
            layer[1],
        )
    return placed


class TestProvenance:
    def test_committed_file_matches_the_kicad_demo_install(self):
        """Byte-verbatim means byte-verbatim: the commit equals the demo
        the installed KiCad 10.0.6 ships, wherever that install exists."""
        if not UPSTREAM_DEMO.exists():
            pytest.skip("KiCad demo install absent — provenance runs where it ships")
        assert (
            hashlib.sha256(DEMO_PCB.read_bytes()).hexdigest()
            == hashlib.sha256(UPSTREAM_DEMO.read_bytes()).hexdigest()
        )


class TestChartedFacts:
    def test_edge_origin_is_the_outline_bbox_min(self):
        assert edge_origin() == ORIGIN

    def test_every_netlist_ref_is_found_and_nothing_else(self):
        assert {row.ref for row in ROWS} == {comp.ref for comp in TINY_BOARD.components}
        assert len(ROWS) == 150

    def test_sides_as_found(self):
        """The charted 7 B.Cu footprints are file-wide — logos and one
        netlist part; the baseline's own B.Cu row is J11 alone."""
        sides = {row.ref: row.side for row in ROWS}
        assert sides["J11"] == "B.Cu"
        assert sum(1 for side in sides.values() if side == "B.Cu") == 1
        doc = read_sexpr(DEMO_PCB.read_text())
        file_wide = [
            next(k[1] for k in form if isinstance(k, list) and k[:1] == ["layer"])
            for form in doc
            if isinstance(form, list) and form[:1] == ["footprint"]
        ]
        assert file_wide.count("B.Cu") == 7
        assert len(file_wide) == 309

    def test_rotations_as_found(self):
        """The demo writes ``-90`` 21 times; the model holds the identical
        transform as 270. Every other token copies verbatim."""
        assert {row.ref: row.rotation for row in ROWS}["C32"] == 270
        histogram = {}
        for row in ROWS:
            histogram[row.rotation] = histogram.get(row.rotation, 0) + 1
        assert histogram == ROTATION_HISTOGRAM
        assert set(histogram) <= set(ROTATIONS)

    def test_anchors_sit_inside_the_outline(self):
        for row in ROWS:
            assert TINY_OUTLINE.min.x <= row.x <= TINY_OUTLINE.max.x
            assert TINY_OUTLINE.min.y <= row.y <= TINY_OUTLINE.max.y


class TestRoundTrip:
    def test_board_placements_carry_the_rows(self):
        for row in ROWS:
            placement = TINY_BOARD.placement(row.ref)
            assert placement is None
        baseline = apply_demo_placements(TINY_BOARD)
        for row in ROWS:
            placement = baseline.placement(row.ref)
            assert (placement.pos.x, placement.pos.y) == (row.x, row.y)
            assert placement.rotation == row.rotation
            assert placement.side == row.side

    def test_export_pcb_reproduces_the_demo_anchors_exactly(self):
        """The lossless pin: place → export → re-read, translate back by
        the bbox min, and every anchor, rotation, and side equals the
        demo board's own footprint forms — no rounding, no flip."""
        from net2board.export import export_pcb

        def nm(token):
            return int(Decimal(token) * 1_000_000)

        baseline = apply_demo_placements(TINY_BOARD)
        exported = footprint_placements(export_pcb(baseline))
        demo = footprint_placements(DEMO_PCB.read_text())
        assert set(exported) == {row.ref for row in ROWS}
        for row in ROWS:
            x, y, rotation, side = exported[row.ref]
            dx, dy, demo_rotation, demo_side = demo[row.ref]
            assert nm(x) + ORIGIN.x == nm(dx)
            assert nm(y) + ORIGIN.y == nm(dy)
            assert rotation == demo_rotation % 360
            assert side == demo_side


class TestReaderFailLoud:
    def test_a_netlist_ref_absent_from_the_demo_board_raises(self):
        from net2board.model import Board, Component

        ghost = Component(ref="GHOST", footprint=TINY_BOARD.components[0].footprint)
        haunted = Board(
            components=TINY_BOARD.components + (ghost,),
            placements=TINY_BOARD.placements,
            nets=TINY_BOARD.nets,
            outline=TINY_BOARD.outline,
            stackup=TINY_BOARD.stackup,
        )
        with pytest.raises(ValueError, match="GHOST"):
            demo_placements(haunted)
