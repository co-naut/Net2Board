"""The ecc83 acceptance placement table — authored once, imported everywhere.

Fifteen hand-authored rows on a coarse millimetre grid, tuned with
Net2Board's own ``run_drc`` as the verifier (ADR-0010): authoring them is
the M1 dogfooding beat. All placements sit on the front side except
``C1`` — the radial electrolytic's ⌀10.5 mm courtyard has no front home
(the fixture's courtyards together exceed the 40 × 30 mm outline's area),
and a big cap on the back is conventional practice.

Layout at a glance (mm): the valve tube sits centre-board; the four
terminal blocks hang off the left and right edges; mounting holes dot
the board's inner ring; resistors, the film cap, and the beat occupy the
bands above and below the tube and the mid-edge windows.

The detect-and-fix beat: ``BEAT_REF``'s row is swapped for
``BEAT_OVERLAP_ROW`` — the same part placed squarely on its neighbour —
so the intermediate board yields exactly one courtyard violation, and
applying ``BEAT_REF``'s table row is the single-move fix.
"""

from typing import NamedTuple

from net2board.model import Board

__all__ = [
    "BEAT_OVERLAP_ROW",
    "BEAT_REF",
    "PLACEMENTS",
    "PlacementRow",
    "apply_placements",
    "beat_overlap_board",
]


class PlacementRow(NamedTuple):
    ref: str
    x: int
    y: int
    rotation: int
    side: str


PLACEMENTS: tuple[PlacementRow, ...] = (
    PlacementRow("C1", 14_000_000, 21_000_000, 0, "B.Cu"),
    PlacementRow("C2", 16_200_000, 1_500_000, 0, "F.Cu"),
    PlacementRow("P1", 1_000_000, 4_000_000, 0, "F.Cu"),
    PlacementRow("P2", 1_000_000, 25_000_000, 0, "F.Cu"),
    PlacementRow("P3", 39_000_000, 4_000_000, 0, "F.Cu"),
    PlacementRow("P4", 39_000_000, 25_000_000, 0, "F.Cu"),
    PlacementRow("P5", 25_500_000, 1_500_000, 0, "F.Cu"),
    PlacementRow("P6", 33_000_000, 28_000_000, 0, "F.Cu"),
    PlacementRow("P7", 12_000_000, 1_500_000, 0, "F.Cu"),
    PlacementRow("P8", 32_000_000, 1_500_000, 0, "F.Cu"),
    PlacementRow("R1", 11_000_000, 27_500_000, 0, "F.Cu"),
    PlacementRow("R2", 21_000_000, 27_500_000, 0, "F.Cu"),
    PlacementRow("R3", 33_400_000, 14_900_000, 90, "F.Cu"),
    PlacementRow("R4", 500_000, 14_500_000, 0, "F.Cu"),
    PlacementRow("U1", 20_000_000, 15_000_000, 0, "F.Cu"),
)

BEAT_REF = "R2"

BEAT_OVERLAP_ROW = next(row for row in PLACEMENTS if row.ref == "R1")._replace(
    ref=BEAT_REF
)


def apply_placements(board: Board) -> Board:
    """Fold ``with_placement`` over ``PLACEMENTS`` in table order."""
    for row in PLACEMENTS:
        board = board.with_placement(*row)
    return board


def beat_overlap_board(board: Board) -> Board:
    """The table applied with ``BEAT_REF``'s row swapped for the overlap row.

    All 15 refs are placed, in table order, so relocating ``BEAT_REF`` to
    its table row afterwards reproduces ``apply_placements`` exactly.
    """
    for row in PLACEMENTS:
        board = board.with_placement(
            *(BEAT_OVERLAP_ROW if row.ref == BEAT_REF else row)
        )
    return board
