"""S3 — IR goldens: ``asdict`` → sorted-keys JSON, regen only via UPDATE_GOLDENS=1.

The goldens pin the loader's whole output on the ecc83 fixture: ``NetlistIR``
and the parsed ``FootprintIR`` set. A diff here means the importer's IR
changed — regeneration is always a deliberate ``UPDATE_GOLDENS=1`` run,
never a hand edit.
"""

from dataclasses import asdict

from conftest import assert_golden, load_ecc83_spec

REGEN_HINT = "regenerate deliberately: UPDATE_GOLDENS=1 uv run pytest tests/test_import_goldens.py"


def test_netlist_ir_golden():
    assert_golden(
        "import", "netlist_ir.json", asdict(load_ecc83_spec().netlist_ir), REGEN_HINT
    )


def test_footprint_irs_golden():
    spec = load_ecc83_spec()
    assert_golden(
        "import",
        "footprint_irs.json",
        {name: asdict(fp) for name, fp in sorted(spec.footprint_irs.items())},
        REGEN_HINT,
    )
