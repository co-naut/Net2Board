"""The runnable acceptance face — the M1 spine in one command (ADR-0010).

``uv run python -m net2board.examples.ecc83 [output.kicad_pcb]`` loads
the committed ecc83 fixture, walks the detect-and-fix beat to its clean
single-move fix, and writes the export. The default output lands beside
the fixture (a gitignored path); pass a path to write elsewhere. Exit
code 0 means the acceptance pipeline held.
"""

from __future__ import annotations

import sys
from pathlib import Path

from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import run_drc
from net2board.examples.ecc83_placements import (
    BEAT_OVERLAP_ROW,
    PLACEMENTS,
    beat_overlap_board,
)
from net2board.export import export_pcb
from net2board.geometry import Point, Rectangle

REPO_ROOT = Path(__file__).resolve().parents[3]
ECC83_DIR = REPO_ROOT / "kicad_demo" / "ecc83"
NETLIST_PATH = ECC83_DIR / "ecc83-pp.net"
FOOTPRINT_DIR = ECC83_DIR / "footprints.pretty"
DEFAULT_OUTPUT = ECC83_DIR / "net2board-ecc83.kicad_pcb"
OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
STACKUP = ("F.Cu", "B.Cu")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print(
            "usage: python -m net2board.examples.ecc83 [output.kicad_pcb]",
            file=sys.stderr,
        )
        return 2
    output = Path(args[0]) if args else DEFAULT_OUTPUT

    spec = BoardSpec.from_files(NETLIST_PATH, FOOTPRINT_DIR, OUTLINE, STACKUP)
    board = build_board(spec)
    print(
        f"import: {len(spec.netlist_ir.comps)} components, "
        f"{len(spec.netlist_ir.nets)} nets from {NETLIST_PATH.name}"
    )

    overlapped = beat_overlap_board(board)
    violations = run_drc(overlapped)
    if overlapped.unplaced_refs() or len(violations) != 1:
        print(
            "detect-and-fix beat failed: expected all placed, exactly 1 violation",
            file=sys.stderr,
        )
        return 1
    print(
        f"beat: {violations[0].offending_refs[1]} placed onto "
        f"{violations[0].offending_refs[0]} -> {len(violations)} violation"
    )
    for violation in violations:
        print(f"      {violation.description}")

    table_row = next(row for row in PLACEMENTS if row.ref == BEAT_OVERLAP_ROW.ref)
    final = overlapped.with_placement(*table_row)
    violations = run_drc(final)
    print(
        f"final: {len(final.placements)}/{len(board.components)} placed, "
        f"{len(violations)} violations"
    )
    if final.unplaced_refs() or violations:
        print("final board not clean", file=sys.stderr)
        return 1

    text = export_pcb(final)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    print(f"export: wrote {output} ({len(text.encode())} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
