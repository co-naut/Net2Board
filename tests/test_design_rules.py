"""DesignRules — the two clearance numbers are Board state (ADR-0020).

``clearance_nm`` (copper to copper) and ``edge_clearance_nm`` (copper to
board edge), integer nanometres, both ``> 0``, defaulting to KiCad's
fresh-project defaults (0.2 / 0.5 mm) so an undeclared board is judged at
our numbers unassisted. A ``Board`` field joining equality — identical
geometry with different rules is a different Board, because legality is
defined by reference to ``run_drc(board)`` (GLOSSARY.md "Legal").
"""

import pytest

from conftest import fresh_board, make_spec
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.model import DesignRules


class TestTheValueObject:
    def test_defaults_are_kicads_fresh_project_numbers(self):
        rules = DesignRules()
        assert rules.clearance_nm == 200_000
        assert rules.edge_clearance_nm == 500_000

    @pytest.mark.parametrize("field", ["clearance_nm", "edge_clearance_nm"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_nonpositive_numbers_raise(self, field, value):
        with pytest.raises(ValueError, match="positive"):
            DesignRules(**{field: value})

    def test_same_numbers_compare_equal(self):
        assert DesignRules() == DesignRules(200_000, 500_000)
        assert DesignRules(clearance_nm=300_000) != DesignRules()


class TestTheBoardField:
    def test_a_built_board_carries_the_defaults(self):
        assert fresh_board().design_rules == DesignRules()

    def test_rules_join_board_equality(self):
        board = fresh_board()
        moved = build_board(
            BoardSpec(
                netlist_ir=make_spec().netlist_ir,
                footprint_irs=make_spec().footprint_irs,
                outline=make_spec().outline,
                stackup=make_spec().stackup,
                design_rules=DesignRules(edge_clearance_nm=300_000),
            )
        )
        assert board != moved

    def test_a_placement_upsert_keeps_the_rules(self):
        board = build_board(
            BoardSpec(
                netlist_ir=make_spec().netlist_ir,
                footprint_irs=make_spec().footprint_irs,
                outline=make_spec().outline,
                stackup=make_spec().stackup,
                design_rules=DesignRules(clearance_nm=300_000),
            )
        ).with_placement("R1", 0, 0, 0, "F.Cu")
        assert board.design_rules.clearance_nm == 300_000


class TestTheSpecDoor:
    def test_the_spec_carries_rules_through_build(self):
        spec = make_spec()
        rules = DesignRules(clearance_nm=250_000)
        board = build_board(
            BoardSpec(
                netlist_ir=spec.netlist_ir,
                footprint_irs=spec.footprint_irs,
                outline=spec.outline,
                stackup=spec.stackup,
                design_rules=rules,
            )
        )
        assert board.design_rules == rules

    def test_from_files_takes_the_rules(self, tmp_path):
        from conftest import ECC83_FP_DIR, ECC83_NETLIST, OUTLINE, STACKUP

        board = build_board(
            BoardSpec.from_files(
                ECC83_NETLIST,
                ECC83_FP_DIR,
                OUTLINE,
                STACKUP,
                design_rules=DesignRules(clearance_nm=230_000),
            )
        )
        assert board.design_rules == DesignRules(
            clearance_nm=230_000, edge_clearance_nm=500_000
        )
