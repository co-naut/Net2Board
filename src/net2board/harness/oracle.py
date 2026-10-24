"""The oracle seam (#88; docs/agents/kicad-cli.md): the engine never
shells out — the purity test bans ``subprocess`` outside this module —
so the harness speaks to KiCad through exactly the ``KiCadOracle``
surface and nothing else. A future IPC adapter (KiCad 11+) fills the
same slot; until then ``KicadCliAdapter`` is the only inhabitant and the
seam is hygiene, not necessity. DRC stays file-based ``kicad-cli pcb
drc`` either way: no IPC API has a run-DRC job at any version.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from net2board.export import export_pcb
from net2board.model import Board

_DRC_COMMAND = ("kicad-cli", "pcb", "drc", "--format", "json", "--severity-all")
"""The one-command oracle (docs/agents/kicad-cli.md). Exit codes: 0 = the
board parsed and was checked — violations or not, a clean board exits 0;
3 = parse failure, no JSON written; 5 = violations found, only under
``--exit-code-violations``, which this command never passes."""

_ORACLE_VERSION_COMMAND = ("kicad-cli", "version")


class OracleStatus(Enum):
    """The typed exit-code contract (#88) — KiCad's ``ApiResponseStatus``
    move: value-or-status, never raise-or-count. Raising is the caller's
    decision; the adapter only reports what the subprocess did."""

    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class OracleReport:
    """One oracle run's answer. A ``PARSED`` status carries the parsed
    JSON report; every failure carries the subprocess's raw exit and
    output, so the caller's message keeps the full diagnostics. A
    malformed JSON report still raises ``json.JSONDecodeError`` from the
    parse — a report-format change must stop the sign-off, never pass
    silently."""

    status: OracleStatus
    report: dict | None
    returncode: int
    stdout: str
    stderr: str


class KiCadOracle(Protocol):
    """The oracle seam (#88; docs/research/kicad-ipc-api.md, §Modeling
    lessons 4 & 6): the engine never shells out — the purity test bans
    ``subprocess`` outside this module — so the harness speaks to KiCad
    through exactly this surface and nothing else. A future IPC adapter
    (KiCad 11+) fills the same slot; until then ``KicadCliAdapter`` is
    the only inhabitant and the seam is hygiene, not necessity. DRC stays
    file-based ``kicad-cli pcb drc`` either way: no IPC API has a run-DRC
    job at any version."""

    def version(self) -> str:
        """The installed oracle's version string, or ``"absent"``."""
        ...

    def drc_report(self, board_text: str, name: str, workdir: Path) -> OracleReport:
        """Run DRC over one board's text — ``<name>.kicad_pcb`` and
        ``<name>.drc.json`` written side by side in ``workdir`` — and map
        the exit code into an ``OracleReport``."""
        ...


class KicadCliAdapter:
    """The file-based ``kicad-cli`` oracle — the only DRC channel any
    KiCad version has. Same command, same JSON parsing as the call sites
    this surface replaces; only the exit-code handling is new, and it is
    a mapping, not a policy."""

    def version(self) -> str:
        result = subprocess.run(
            _ORACLE_VERSION_COMMAND, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else "absent"

    def drc_report(self, board_text: str, name: str, workdir: Path) -> OracleReport:
        board_path = workdir / f"{name}.kicad_pcb"
        board_path.write_text(board_text)
        report_path = workdir / f"{name}.drc.json"
        result = subprocess.run(
            (*_DRC_COMMAND, "-o", str(report_path), str(board_path)),
            capture_output=True,
            text=True,
            check=False,
        )
        status: OracleStatus
        report: dict | None
        if result.returncode == 0:
            status, report = OracleStatus.PARSED, json.loads(report_path.read_text())
        elif result.returncode == 3:
            status, report = OracleStatus.PARSE_FAILED, None
        else:
            status, report = OracleStatus.UNEXPECTED, None
        return OracleReport(
            status=status,
            report=report,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


KICAD_CLI = KicadCliAdapter()
"""The harness's oracle — the seam's one inhabitant until a KiCad 11 IPC
adapter exists to fill it."""


def kicad_drc_candidate(board: Board, workdir: Path) -> int:
    """One wrapper candidate, evaluated the wrapper way: export the whole
    board, one real oracle run, parse the violation count. This is the
    per-candidate work the derived baseline prices in — an engine that
    shelled to the oracle for every candidate would pay exactly this,
    times every candidate. A board the oracle cannot parse is a raised
    error, never a cost-free reading."""
    outcome = KICAD_CLI.drc_report(export_pcb(board), "candidate", workdir)
    if outcome.status is not OracleStatus.PARSED:
        raise RuntimeError(
            f"kicad-cli exited {outcome.returncode} on a wrapper candidate\n"
            f"stdout:\n{outcome.stdout}stderr:\n{outcome.stderr}"
        )
    return len(outcome.report["violations"])


_NET_TOKEN = re.compile(r"\[([^]]+)\]")

_KICAD_NET_ESCAPES = (("{slash}", "/"),)
"""Net names escape ``/`` as ``{slash}`` — the tiny fixture's netlist
stores ``SDO{slash}out1`` while the oracle's DRC JSON reports
``SDO/out1`` (probed under ``ORACLE_TARGET``). Both sides are normalized
through the same mapping; an unmapped escape shows up as a coverage
failure rather than a silent pass."""


def unescape_net_name(name: str) -> str:
    """Undo KiCad's net-name escapes (``{slash}`` → ``/`` today)."""
    for escaped, literal in _KICAD_NET_ESCAPES:
        name = name.replace(escaped, literal)
    return name


def oracle_violation_count(report: dict, violation_type: str) -> int:
    """How many oracle violations carry ``violation_type`` — per type only."""
    return sum(
        1 for violation in report["violations"] if violation["type"] == violation_type
    )


def ratsnest_net_names(report: dict) -> set[str]:
    """Every net name the oracle's ratsnest references.

    The oracle embeds each pad's net in its item description and carries
    no structured net field, so the bracketed token is the only naming
    available (probed under ``ORACLE_TARGET``, #18). An item without one
    fails loudly — a report-format change must stop the sign-off, not
    silently pass it.
    """
    names = set()
    for entry in report["unconnected_items"]:
        for item in entry["items"]:
            match = _NET_TOKEN.search(item["description"])
            assert match, (
                f"oracle pad item carries no [net] token: "
                f"{item['description']!r} — check the installed kicad-cli's "
                f"JSON description format"
            )
            names.add(unescape_net_name(match.group(1)))
    return names
