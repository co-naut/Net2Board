"""The library-wide error root: Net2BoardError reconciles the phase roots (ADR-0008, ADR-0013).

``BoardSpecError`` keeps ownership of the spec-binding surface (ADR-0008);
the shared root exists so the later solve surface (``SolveError``, ADR-0013)
joins the same tree instead of forking a second one. The M1 surface —
subclass set, carried fields, messages — is pinned unchanged by
``test_import_errors`` and ``test_build_board``; this file pins only the
reconciliation.
"""

import pytest

import net2board
from net2board.boardspec import (
    BoardSpecError,
    FootprintNotFound,
    FootprintResolveError,
    PinPadBindingError,
    UnsupportedCourtyardShape,
)
from net2board.errors import Net2BoardError

SPEC_ERRORS = (
    BoardSpecError,
    FootprintNotFound,
    FootprintResolveError,
    UnsupportedCourtyardShape,
    PinPadBindingError,
)


def test_root_is_exported_from_the_package():
    assert net2board.Net2BoardError is Net2BoardError


def test_root_is_an_exception():
    assert issubclass(Net2BoardError, Exception)


def test_root_is_not_the_spec_root():
    assert Net2BoardError is not BoardSpecError


def test_root_catches_a_raised_spec_error():
    with pytest.raises(Net2BoardError):
        raise BoardSpecError("spec binding failed")


class TestSpecHierarchyUnderTheRoot:
    def test_spec_root_extends_the_shared_root(self):
        assert issubclass(BoardSpecError, Net2BoardError)

    def test_every_spec_error_extends_the_shared_root(self):
        for exc in SPEC_ERRORS:
            assert issubclass(exc, Net2BoardError)
