"""DSN export golden: byte-exact Specctra over the ecc83 fixture (#35).

The acceptance board exercises the whole DSN surface on real parsed IR:
thru-hole ``*.Cu`` padstack expansion, the back-side C1 (front-canonical
image + ``back`` token), all four rotations, quoted ``Net-(…)`` names,
multi- and single-pad footprints. A byte-diff here means the router
handoff changed; regeneration is always a deliberate
``UPDATE_GOLDENS=1`` run, never a hand edit.
"""

from conftest import assert_byte_golden, load_ecc83_spec
from net2board.build import build_board
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_dsn

REGEN_HINT = (
    "regenerate deliberately: "
    "UPDATE_GOLDENS=1 uv run pytest tests/test_export_dsn_goldens.py"
)


def test_export_dsn_golden_byte_exact():
    board = apply_placements(build_board(load_ecc83_spec()))
    assert_byte_golden(
        "export",
        "ecc83_acceptance.dsn",
        export_dsn(board),
        REGEN_HINT,
    )
