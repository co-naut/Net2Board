"""BoardSpec — the parsed input to ``build_board`` (ADR-0007, ADR-0008).

Carries IR, not filesystem paths: ``BoardSpec.from_files`` owns the
filesystem — it parses the KiCad netlist, resolves each component's
footprint by stripping the library nickname and matching a bare
``<entry_name>.kicad_mod`` in the configured library directory, and parses
each referenced footprint to ``FootprintIR``.

The structured binding-failure hierarchy rooted at ``BoardSpecError`` is
pinned here: the loader raises ``FootprintNotFound``,
``FootprintResolveError``, and ``UnsupportedCourtyardShape`` (the last is
raised by the ``ir`` parsers acting as the loader's parse phase, ADR-0008);
``build_board`` raises ``PinPadBindingError``. The root itself extends the
library-wide ``Net2BoardError`` (ADR-0013's reconciliation) — structural
only, the phase split of ADR-0008 is untouched.

The error classes below are defined before the ``ir`` import on purpose:
``net2board.ir`` imports the parsers at its tail, the parsers import these
classes from here, and this module's ``from_files`` imports the parsers
lazily inside the method body — the one deferred import that breaks the
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from net2board.errors import Net2BoardError
from net2board.geometry import Rectangle

__all__ = [
    "BoardSpec",
    "BoardSpecError",
    "FootprintNotFound",
    "FootprintResolveError",
    "PinPadBindingError",
    "UnsupportedCourtyardShape",
]


class BoardSpecError(Net2BoardError):
    """Root of the spec-binding failure surface (ADR-0008)."""


@dataclass(frozen=True)
class FootprintResolveError(BoardSpecError):
    """A component's footprint reference cannot be resolved.

    Loader-phase failure: the comp carries no ``(footprint …)`` token, or a
    resolved library file declares a different entry name than the one
    matched (``entry_name`` carries the matched entry in that case). The
    offending ``ref`` is always carried so the caller can act
    programmatically.
    """

    ref: str
    entry_name: str | None = None

    def __str__(self) -> str:
        if self.entry_name is None:
            return f"cannot resolve a footprint for component {self.ref!r}"
        return (
            f"resolved library entry {self.entry_name!r} for component "
            f"{self.ref!r} declares a different name"
        )


@dataclass(frozen=True)
class FootprintNotFound(BoardSpecError):
    """No ``<entry_name>.kicad_mod`` in the configured library directory."""

    ref: str
    entry_name: str

    def __str__(self) -> str:
        return (
            f"footprint {self.entry_name!r} for component {self.ref!r} not found "
            f"in the footprint library directory"
        )


@dataclass(frozen=True)
class UnsupportedCourtyardShape(BoardSpecError):
    """A CrtYd graphic outside the M1 courtyard vocabulary.

    M1 supports axis-aligned rectangles and circles only (ADR-0006); an
    unsupported shape raises at load time rather than degrading to a
    false-clean DRC run.
    """

    entry_name: str

    def __str__(self) -> str:
        return (
            f"footprint {self.entry_name!r} has an unsupported F.CrtYd courtyard "
            f"shape (M1: axis-aligned rectangles and circles only)"
        )


@dataclass(frozen=True)
class PinPadBindingError(BoardSpecError):
    """A netlist pin has no pad with a matching number on its footprint.

    Join-time failure raised by ``build_board``: the offending ``ref`` and
    ``pin_number`` are carried so the caller can act programmatically.
    """

    ref: str
    pin_number: str

    def __str__(self) -> str:
        return f"netlist pin {self.pin_number!r} of {self.ref!r} has no matching footprint pad"


from net2board.ir import FootprintIR, NetlistIR


@dataclass(frozen=True)
class BoardSpec:
    """The input to ``build_board``: parsed IR, outline, and stackup.

    ``footprint_irs`` is keyed by bare library entry name (the loader's
    nickname-strip rule); ``stackup`` is canonical layer names. The resulting
    Board absorbs outline and stackup and does not retain this spec.
    """

    netlist_ir: NetlistIR
    footprint_irs: dict[str, FootprintIR]
    outline: Rectangle
    stackup: tuple[str, ...]

    @classmethod
    def from_files(
        cls,
        netlist_path: Path | str,
        footprint_lib_dir: Path | str,
        outline: Rectangle,
        stackup: tuple[str, ...],
    ) -> BoardSpec:
        """Load a netlist and its footprint library directory into a spec.

        The parsers are imported lazily here — they import this module's
        error hierarchy at load time (see the module docstring). Each
        component's footprint is resolved by stripping the library nickname
        and matching a bare ``<entry_name>.kicad_mod`` in
        ``footprint_lib_dir``; only entries referenced by some component are
        parsed. Malformed files raise ``ValueError`` carrying the file name;
        semantic failures raise the structured hierarchy above.
        """
        from net2board.ir import parse_footprint, parse_netlist

        netlist_path = Path(netlist_path)
        lib_dir = Path(footprint_lib_dir)
        try:
            netlist_ir = parse_netlist(netlist_path.read_text())
        except ValueError as exc:
            raise ValueError(f"{netlist_path.name}: {exc}") from None
        footprint_irs: dict[str, FootprintIR] = {}
        for comp in netlist_ir.comps:
            if comp.entry_name in footprint_irs:
                continue
            path = lib_dir / f"{comp.entry_name}.kicad_mod"
            if not path.is_file():
                raise FootprintNotFound(ref=comp.ref, entry_name=comp.entry_name)
            try:
                footprint_ir = parse_footprint(path.read_text())
            except ValueError as exc:
                raise ValueError(f"{path.name}: {exc}") from None
            if footprint_ir.entry_name != comp.entry_name:
                raise FootprintResolveError(ref=comp.ref, entry_name=comp.entry_name)
            footprint_irs[comp.entry_name] = footprint_ir
        return cls(
            netlist_ir=netlist_ir,
            footprint_irs=footprint_irs,
            outline=outline,
            stackup=stackup,
        )
