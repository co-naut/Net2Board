"""Shared test world: hand-built IR plus the committed ecc83 fixture paths.

The world of ``TWO_RESISTORS`` (deliberately not a real circuit) is the
library-embedder path — boards from IR, no fixture files:

- comps ``R1``, ``R2`` both use entry ``RES``; ``R3`` uses entry ``BIGRES`` —
  three comps so join iteration is observable, two sharing a footprint so
  footprint IR reuse is exercised.
- net ``GND`` joins (R1, pin 2) and (R2, pin 2); net ``N1`` holds (R1, pin 1)
  alone — every net is unrouted in M1, single-pin nets included.
- entry ``RES`` carries an *extra* pad ``"9"`` with no netlist pin anywhere —
  the extra-pad case stays silent by contract (ADR-0008).
- entry ``BIGRES`` carries a circle courtyard whose defining end point is
  off-axis: radius² = 10 × 10¹² nm² but radius = √10 × 10⁶ nm — not an
  integer nm — exercising the exact-integer representation; ``RES`` carries
  a rectangle courtyard.
- outline is a 40 mm x 30 mm board in integer nm.

``ECC83_NETLIST`` / ``ECC83_FP_DIR`` / ``load_ecc83_spec()`` serve the
file-driven loader tests over the committed fixture.
"""

import json
import os
from pathlib import Path

from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.geometry import Circle, Point, Rectangle
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR

REPO_ROOT = Path(__file__).resolve().parent.parent
ECC83_DIR = REPO_ROOT / "kicad_demo" / "ecc83"
ECC83_NETLIST = ECC83_DIR / "ecc83-pp.net"
ECC83_FP_DIR = ECC83_DIR / "footprints.pretty"

RES_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(1_600_000, 1_600_000),
        drill=800_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(7_620_000, 0),
        size=(1_600_000, 1_600_000),
        drill=800_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="oval",
    ),
    PadIR(
        number="9",
        local_pos=Point(-1_000_000, 2_000_000),
        size=(900_000, 900_000),
        drill=500_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
)
BIGRES_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(2_000_000, 2_000_000),
        drill=1_000_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="rect",
    ),
    PadIR(
        number="2",
        local_pos=Point(10_000_000, 0),
        size=(2_000_000, 2_000_000),
        drill=1_000_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
)

RES_COURTYARD = Rectangle(
    min=Point(-1_050_000, -1_500_000), max=Point(8_670_000, 1_500_000)
)
BIGRES_COURTYARD = Circle(
    center=Point(5_000_000, 0),
    radius_sq=10_000_000_000_000,
    end=Point(8_000_000, 1_000_000),
)

OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
STACKUP = ("F.Cu", "B.Cu")

FOOTPRINT_IRS = {
    "RES": FootprintIR(entry_name="RES", pads=RES_PADS, courtyard=RES_COURTYARD),
    "BIGRES": FootprintIR(
        entry_name="BIGRES", pads=BIGRES_PADS, courtyard=BIGRES_COURTYARD
    ),
}

NETLIST_IR = NetlistIR(
    comps=(
        CompIR(ref="R1", entry_name="RES"),
        CompIR(ref="R2", entry_name="RES"),
        CompIR(ref="R3", entry_name="BIGRES"),
    ),
    nets=(
        NetIR(name="GND", nodes=(("R1", "2"), ("R2", "2"))),
        NetIR(name="N1", nodes=(("R1", "1"),)),
    ),
)


def make_spec(netlist_ir=NETLIST_IR, footprint_irs=FOOTPRINT_IRS) -> BoardSpec:
    """A BoardSpec over the shared hand-built IR (overridable per test)."""
    return BoardSpec(
        netlist_ir=netlist_ir,
        footprint_irs=footprint_irs,
        outline=OUTLINE,
        stackup=STACKUP,
    )


def fresh_board():
    """The unplaced Board from the shared IR — one build_board call."""
    return build_board(make_spec())


def load_ecc83_spec() -> BoardSpec:
    """The ecc83 fixture loaded through the file-driven path."""
    return BoardSpec.from_files(ECC83_NETLIST, ECC83_FP_DIR, OUTLINE, STACKUP)


def assert_golden(dir_name: str, name: str, value, regen_hint: str) -> None:
    """Assert ``value`` renders byte-equal to a sorted-keys JSON golden.

    Enums serialize as their values. Regeneration only happens through the
    deliberate ``UPDATE_GOLDENS=1`` path — hand edits are never legitimate
    (ADR-0010).
    """
    rendered = (
        json.dumps(value, indent=2, sort_keys=True, default=lambda o: o.value) + "\n"
    )
    _check_golden(dir_name, name, rendered, regen_hint)


def assert_byte_golden(dir_name: str, name: str, text: str, regen_hint: str) -> None:
    """Assert ``text`` is byte-equal to a raw golden (export output).

    Same contract as ``assert_golden``: regeneration only through
    ``UPDATE_GOLDENS=1`` — a byte-diff is the cheapest, least-gameable
    regression signal for serialized output (ADR-0010).
    """
    _check_golden(dir_name, name, text, regen_hint)


def _check_golden(dir_name: str, name: str, rendered: str, regen_hint: str) -> None:
    golden_dir = Path(__file__).parent / "goldens" / dir_name
    path = golden_dir / name
    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return
    assert path.exists(), f"{name} missing — {regen_hint}"
    assert path.read_text() == rendered, f"{name} stale — {regen_hint}"
