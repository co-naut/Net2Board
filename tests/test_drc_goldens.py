"""S8 — run_drc golden on an overlapped fixture board (ADR-0009, ADR-0010).

The golden pins the whole deterministic output — struct fields, canonical
ref order, pinned descriptions, final sort — over an ecc83 fixture board
with two overlapping pairs (rect-rect and circle-rect). A diff here means
the DRC's observable output changed; regeneration is always a deliberate
``UPDATE_GOLDENS=1`` run, never a hand edit.
"""

from dataclasses import asdict

from conftest import assert_golden, load_ecc83_spec
from net2board.build import build_board
from net2board.drc import run_drc

REGEN_HINT = (
    "regenerate deliberately: UPDATE_GOLDENS=1 uv run pytest tests/test_drc_goldens.py"
)


def overlapped_fixture_board():
    """The ecc83 board with two deliberate overlaps: R1+R2 co-placed
    (rect-rect) and C1+C2 co-placed (circle-rect), far enough apart that
    the pairs don't interact — exactly two violations."""
    return (
        build_board(load_ecc83_spec())
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R2", 8_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("C1", 30_000_000, 22_000_000, 0, "F.Cu")
        .with_placement("C2", 30_000_000, 22_000_000, 0, "F.Cu")
    )


def test_run_drc_golden():
    violations = run_drc(overlapped_fixture_board())
    assert [v.offending_refs for v in violations] == [
        ("placement:C1", "placement:C2"),
        ("placement:R1", "placement:R2"),
    ]
    assert_golden(
        "drc",
        "run_drc.json",
        [asdict(violation) for violation in violations],
        REGEN_HINT,
    )
