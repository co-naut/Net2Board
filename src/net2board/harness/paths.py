"""The harness's fixture roots, stated as an interface (#68).

``SessionPaths`` makes the harness's fixture roots parameters instead of
file-position arithmetic: the installed wheel fails with a named error —
not a ``FileNotFoundError`` from a ``parents[3]`` walk — and any caller
can point the faces at their own checkout. Construction validates — a
missing root is the named failure — while the jar is provenance-only:
absent is a reading there, never a failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from net2board.errors import Net2BoardError

REPO_ROOT = Path(__file__).resolve().parents[3]


class SessionFixturesMissing(Net2BoardError):
    """The session's fixture roots do not exist where they were pointed
    (ADR-0008's fail-fast posture, #68). The installed wheel ships no
    fixtures, so the default resolution fails here — with the path named
    and the injection answer stated — never as a bare ``FileNotFoundError``
    from a loader's ``open()`` three frames deeper."""


def freerouting_jar_default() -> Path:
    """The Freerouting jar's env-or-``~/tools`` default — the one pin
    site (#104): ``SessionPaths`` reads it for the provenance sha256 and
    the route tier reads it to route. Version-pinned
    (``freerouting-2.4.1.jar``); a jar bump is this line and a re-probe."""
    return Path(
        os.environ.get("NET2BOARD_FREEROUTING_JAR", "~/tools/freerouting-2.4.1.jar")
    ).expanduser()


@dataclass(frozen=True)
class SessionPaths:
    """The fixture roots every session face reads (#68): the interface
    states its inputs instead of resolving them from file position, so
    the faces run from an installed wheel the moment a caller points
    them at a checkout."""

    ecc83_dir: Path
    tiny_dir: Path
    freerouting_jar: Path | None = None

    def __post_init__(self) -> None:
        for label, path in (
            ("ecc83 netlist", self.ecc83_netlist),
            ("ecc83 footprints", self.ecc83_footprints),
            ("tiny_tapeout netlist", self.tiny_netlist),
            ("tiny_tapeout footprints", self.tiny_footprints),
        ):
            if not path.exists():
                raise SessionFixturesMissing(
                    f"{label} not found at {path} — the wheel ships no "
                    f"fixtures; point SessionPaths at a checkout "
                    f"(SessionPaths.repo_default() resolves the in-repo "
                    f"roots)"
                )

    @property
    def ecc83_netlist(self) -> Path:
        return self.ecc83_dir / "ecc83-pp.net"

    @property
    def ecc83_footprints(self) -> Path:
        return self.ecc83_dir / "footprints.pretty"

    @property
    def ecc83_default_output(self) -> Path:
        return self.ecc83_dir / "net2board-ecc83-session.kicad_pcb"

    @property
    def tiny_netlist(self) -> Path:
        return self.tiny_dir / "tinytapeout.net"

    @property
    def tiny_footprints(self) -> Path:
        return self.tiny_dir / "footprints.pretty"

    @property
    def tiny_default_output(self) -> Path:
        return self.tiny_dir / "net2board-tiny-session.kicad_pcb"

    @classmethod
    def repo_default(cls) -> SessionPaths:
        """The developer path: the committed fixtures beside this package,
        plus the jar's env-or-~/tools default
        (``freerouting_jar_default``). From an installed wheel the roots
        are not there — the named failure, never a hunt."""
        return cls(
            ecc83_dir=REPO_ROOT / "kicad_demo" / "ecc83",
            tiny_dir=REPO_ROOT / "kicad_demo" / "tiny_tapeout",
            freerouting_jar=freerouting_jar_default(),
        )
