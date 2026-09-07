"""S2 — export golden: byte-exact `.kicad_pcb` over the ecc83 fixture (ADR-0010).

The golden board exercises the whole serializer surface on real parsed IR:
rect and circle courtyards, both sides (the back side bakes the mirror),
all four 90° rotations, multi- and single-pad footprints, connected and
unconnected pads. A byte-diff here means the observable export output
changed; regeneration is always a deliberate ``UPDATE_GOLDENS=1`` run,
never a hand edit.
"""

from conftest import assert_byte_golden, load_ecc83_spec
from net2board.build import build_board
from net2board.export import export_pcb

REGEN_HINT = "regenerate deliberately: UPDATE_GOLDENS=1 uv run pytest tests/test_export_goldens.py"


def golden_fixture_board():
    """The ecc83 board with seven placements covering the export surface.

    Rects: R1 front 0°, R2 front 90°, C2 back 270°, P1 front 180°.
    Circles: C1 front 180° (off-center), U1 front 0° (off-axis end point),
    P5 back 0° (mounting hole, single-pad net).
    """
    return (
        build_board(load_ecc83_spec())
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R2", 20_000_000, 6_000_000, 90, "F.Cu")
        .with_placement("C1", 32_000_000, 6_000_000, 180, "F.Cu")
        .with_placement("C2", 10_000_000, 20_000_000, 270, "B.Cu")
        .with_placement("P1", 20_000_000, 24_000_000, 180, "F.Cu")
        .with_placement("P5", 6_000_000, 26_000_000, 0, "B.Cu")
        .with_placement("U1", 20_000_000, 14_000_000, 0, "F.Cu")
    )


def test_export_golden_byte_exact():
    assert_byte_golden(
        "export",
        "ecc83.kicad_pcb",
        export_pcb(golden_fixture_board()),
        REGEN_HINT,
    )
