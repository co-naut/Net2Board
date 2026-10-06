"""Slow tier — the kicad-cli oracle cross-check on the searched
tiny_tapeout board (ADR-0010's assertion-5 posture, extended by ADR-0018;
#49's acceptance: the oracle agrees with ``run_drc``).

The board is the tiny session's headline export (``tiny_session_record``
— the same deterministic record the acceptance face replays), handed to
the installed ``kicad-cli pcb drc`` (JSON, ``--severity-all``). The
agreement claim is per-type on the checks we model, never totals: the
oracle also emits pad-clearance, silk, mask, hole, and library-table
facts M2 deliberately does not model (ADR-0006's courtyard proxy) — on
the searched board those carry real pad-proximity findings, posted to the
ADR-0013 review (#47) as review-trigger evidence rather than gated here.

The ratsnest reference set is the nets with two or more pads on *placed*
refs — the reach-descending visit order (#52) places every ref, so this
is every netlist-level multi-pad net. The oracle's ratsnest is asserted
as a **subset** of it, never equal: a net whose placed pads touch
merges into one copper island and forms no unconnected pair, and pad
contact is a fact outside M2's model (ADR-0006's courtyard proxy) —
#52's searched board carries one, MUX_SEL (the NT1 net-tie pad against
U4's pin-1 TSSOP pad, centers 0.264 mm apart).
"""

import json
import re
import shutil
import subprocess

import pytest

from net2board.drc import ViolationType, run_drc

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("kicad-cli") is None,
        reason="kicad-cli is not installed — the slow tier is a local "
        "sign-off step, run deliberately with `pytest -m slow` (ADR-0010)",
    ),
]

_ORACLE_OVERLAP_TYPE = ViolationType.COURTYARDS_OVERLAP.value
_DRC_COMMAND = ("kicad-cli", "pcb", "drc", "--format", "json", "--severity-all")

_NET_TOKEN = re.compile(r"\[([^]]+)\]")

_KICAD_NET_ESCAPES = (("{slash}", "/"),)
"""Net names escape ``/`` as ``{slash}`` — the tiny fixture's netlist
stores ``SDO{slash}out1`` while the oracle's DRC JSON reports
``SDO/out1`` (probed on 10.0.6). Both sides are normalized through the
same mapping; an unmapped escape shows up as a coverage failure rather
than a silent pass."""


def _unescape_net_name(name: str) -> str:
    for escaped, literal in _KICAD_NET_ESCAPES:
        name = name.replace(escaped, literal)
    return name


@pytest.fixture(scope="session")
def tiny_oracle_report(tmp_path_factory, tiny_session_record):
    """The oracle's report over the searched export, run once: exit 0 is
    the parse-acceptance claim, asserted against the subprocess itself —
    exit 3 means the board never loaded (pinned in TestOracleContract)."""
    work = tmp_path_factory.mktemp("tiny-oracle")
    board_path = work / "tiny_session.kicad_pcb"
    board_path.write_text(tiny_session_record.export)
    report_path = work / "tiny_session.drc.json"
    result = subprocess.run(
        (*_DRC_COMMAND, "-o", str(report_path), str(board_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"kicad-cli exited {result.returncode} (3 = parse failure)\n"
        f"stdout:\n{result.stdout}stderr:\n{result.stderr}"
    )
    return json.loads(report_path.read_text())


def oracle_violation_count(report, violation_type):
    """How many oracle violations carry ``violation_type`` — per type only."""
    return sum(
        1 for violation in report["violations"] if violation["type"] == violation_type
    )


def ratsnest_net_names(report):
    """Every net name the oracle's ratsnest references — the bracketed
    token is the only naming the JSON carries (probed in #18); an item
    without one fails loudly."""
    names = set()
    for entry in report["unconnected_items"]:
        for item in entry["items"]:
            match = _NET_TOKEN.search(item["description"])
            assert match, (
                f"oracle pad item carries no [net] token: "
                f"{item['description']!r} — check the installed kicad-cli's "
                f"JSON description format"
            )
            names.add(_unescape_net_name(match.group(1)))
    return names


def placed_multi_pad_net_names(board):
    """The nets that can form an unconnected pair on this board: two or
    more pads on placed refs — normalized through the same name mapping
    as the oracle's report. A superset of the oracle's ratsnest: same-net
    pads that touch are one island, a contact M2 does not model."""
    placed = {placement.ref for placement in board.placements}
    return {
        _unescape_net_name(net.name)
        for net in board.nets
        if sum(1 for ref, _pad in net.pins if ref in placed) >= 2
    }


class TestSearchedTinyBoardAgainstTheOracle:
    def test_ratsnest_is_present(self, tiny_session_record, tiny_oracle_report):
        assert len(tiny_oracle_report["unconnected_items"]) > 0

    def test_ratsnest_names_only_placed_multi_pad_nets(
        self, tiny_session_record, tiny_oracle_report
    ):
        """The subset direction is the coverage claim: the oracle never
        reports an unconnected pair on a net that cannot form one. The
        reverse can legitimately differ — same-net pads that touch are
        one island, and contact is outside the model (module docstring)."""
        board = tiny_session_record.headline.board
        assert ratsnest_net_names(tiny_oracle_report) <= (
            placed_multi_pad_net_names(board)
        )

    def test_outline_is_valid(self, tiny_oracle_report):
        assert oracle_violation_count(tiny_oracle_report, "invalid_outline") == 0

    def test_no_courtyards_overlap_on_either_side(
        self, tiny_session_record, tiny_oracle_report
    ):
        """The searched board is overlap-clean by construction (the probe
        rejects error candidates) — and the oracle agrees, per type."""
        board = tiny_session_record.headline.board
        ours = sum(
            1
            for violation in run_drc(board)
            if violation.type is ViolationType.COURTYARDS_OVERLAP
        )
        assert ours == 0
        assert oracle_violation_count(tiny_oracle_report, _ORACLE_OVERLAP_TYPE) == 0

    def test_containment_clean_with_no_oracle_analogue(
        self, tiny_session_record, tiny_oracle_report
    ):
        """ADR-0018: the oracle has no containment check to pair with —
        ours counts 0 on the searched board, and the oracle never emits
        the type. Per-type only, no parity claim."""
        board = tiny_session_record.headline.board
        ours = sum(
            1
            for violation in run_drc(board)
            if violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        )
        assert ours == 0
        assert (
            oracle_violation_count(
                tiny_oracle_report, ViolationType.COURTYARD_OUTSIDE_OUTLINE.value
            )
            == 0
        )

    def test_no_error_findings_of_our_own(self, tiny_session_record):
        """``run_drc`` — the authority on what is wrong — is clean on the
        searched board: the acceptance face's containment beat, asserted
        here against the same record the oracle saw."""
        assert run_drc(tiny_session_record.headline.board) == ()
