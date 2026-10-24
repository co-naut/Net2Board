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

import pytest

from conftest import load_tiny_spec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.examples.tiny_demo_placements import apply_demo_placements
from net2board.export import export_pcb
from net2board.harness.oracle import (
    oracle_violation_count,
    ratsnest_net_names,
    unescape_net_name,
)
from net2board.solver import legalize

pytestmark = [
    pytest.mark.slow,
]

_ORACLE_OVERLAP_TYPE = ViolationType.COURTYARDS_OVERLAP.value


@pytest.fixture(scope="session")
def tiny_oracle_report(tiny_session_record, oracle_report):
    """The oracle's report over the searched export, run once: the
    parse-acceptance claim is the adapter's ``PARSED`` status — exit 3
    means the board never loaded (pinned in TestOracleContract)."""
    return oracle_report(tiny_session_record.export, "tiny_session")


def placed_multi_pad_net_names(board):
    """The nets that can form an unconnected pair on this board: two or
    more pads on placed refs — normalized through the same name mapping
    as the oracle's report. A superset of the oracle's ratsnest: same-net
    pads that touch are one island, a contact M2 does not model."""
    placed = {placement.ref for placement in board.placements}
    return {
        unescape_net_name(net.name)
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


_PAD_COPPER_TYPES = (
    ViolationType.SHORTING_ITEMS,
    ViolationType.CLEARANCE,
    ViolationType.COPPER_EDGE_CLEARANCE,
)


class TestPadCopperParity:
    """#91's parity receipt (ADR-0022): per-type parity on pre-route
    boards — pads are the only copper there. The demo placement reads
    0/0/2 on both sides (the C12 pair at 0.49 mm actual); the
    deliberately shorted variant — C1 co-placed onto C2, SMD pads so the
    granularities coincide — pins the shorting_items half with the
    counts equal."""

    @staticmethod
    def _ours_by_type(board):
        run = run_drc(board)
        return {
            check.value: sum(1 for v in run if v.type is check)
            for check in _PAD_COPPER_TYPES
        }

    @staticmethod
    def _oracle_by_type(report):
        return {
            check.value: oracle_violation_count(report, check.value)
            for check in _PAD_COPPER_TYPES
        }

    @pytest.fixture(scope="class")
    def demo_board(self):
        return apply_demo_placements(build_board(load_tiny_spec()))

    @pytest.fixture(scope="class")
    def shorted_board(self, demo_board):
        c2 = demo_board.placement("C2")
        return demo_board.with_placement("C1", c2.pos.x, c2.pos.y, c2.rotation, c2.side)

    @pytest.fixture(scope="class")
    def demo_oracle_by_type(self, demo_board, oracle_report):
        return self._oracle_by_type(oracle_report(export_pcb(demo_board), "demo"))

    @pytest.fixture(scope="class")
    def shorted_oracle_by_type(self, shorted_board, oracle_report):
        return self._oracle_by_type(oracle_report(export_pcb(shorted_board), "shorted"))

    def test_demo_placement_reads_0_0_2_on_both_sides(
        self, demo_board, demo_oracle_by_type
    ):
        assert self._ours_by_type(demo_board) == {
            "shorting_items": 0,
            "clearance": 0,
            "copper_edge_clearance": 2,
        }
        assert self._ours_by_type(demo_board) == demo_oracle_by_type

    def test_shorted_variant_parity(self, shorted_board, shorted_oracle_by_type):
        ours = self._ours_by_type(shorted_board)
        assert ours["shorting_items"] == 3
        assert ours == shorted_oracle_by_type


class TestLegalizeRepairsCopper:
    """#95's slow receipt: the repair vocabulary ends the tiny demo
    board — and its deliberately shorted variant — at the oracle's own
    0/0/0 on the three copper types. The board's authored facts stay:
    the edge-hanging connectors keep their containment and courtyard
    findings (unrepairable by translation, named), and every pad-copper
    finding is gone — the C12 pair's 0.49 mm edge gap pulled inside the
    shrunk outline, the co-placed short pushed out past full clearance.
    ``run_drc`` defines legal for the copper set; the oracle is the why
    (ADR-0010)."""

    @pytest.fixture(scope="class")
    def legal_demo_board(self):
        # the C12 pair's 0.49 mm edge gap pulls in by exactly 11 000 nm —
        # the 10 nm deficit plus the 1 µm margin; the edge-hanging
        # connectors keep their authored containment and courtyard facts
        return legalize(apply_demo_placements(build_board(load_tiny_spec()))).board

    @pytest.fixture(scope="class")
    def legal_shorted_board(self, legal_demo_board):
        c2 = legal_demo_board.placement("C2")
        co_placed = legal_demo_board.with_placement(
            "C1", c2.pos.x, c2.pos.y, c2.rotation, c2.side
        )
        return legalize(co_placed).board

    @staticmethod
    def _zeros(board):
        run = run_drc(board)
        counts = {
            check.value: sum(1 for v in run if v.type is check)
            for check in _PAD_COPPER_TYPES
        }
        assert counts == {check.value: 0 for check in _PAD_COPPER_TYPES}
        return counts

    def test_legalized_demo_reads_0_0_0_at_the_oracle(
        self, legal_demo_board, oracle_report
    ):
        self._zeros(legal_demo_board)
        report = oracle_report(export_pcb(legal_demo_board), "legal-demo")
        assert {
            check.value: oracle_violation_count(report, check.value)
            for check in _PAD_COPPER_TYPES
        } == {check.value: 0 for check in _PAD_COPPER_TYPES}

    def test_legalized_shorted_reads_0_0_0_at_the_oracle(
        self, legal_shorted_board, oracle_report
    ):
        self._zeros(legal_shorted_board)
        report = oracle_report(export_pcb(legal_shorted_board), "legal-shorted")
        assert {
            check.value: oracle_violation_count(report, check.value)
            for check in _PAD_COPPER_TYPES
        } == {check.value: 0 for check in _PAD_COPPER_TYPES}
