import importlib
import importlib.metadata

import net2board

MODULE_HOMES = (
    "geometry",
    "model",
    "ir",
    "boardspec",
    "build",
    "drc",
    "export",
    "examples",
)


def test_exposes_version():
    assert net2board.__version__ == importlib.metadata.version("net2board")


def test_module_homes_follow_spec_inventory():
    for name in MODULE_HOMES:
        importlib.import_module(f"net2board.{name}")
