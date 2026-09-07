"""S2 — build_board: the pure IR → Board join (ADR-0007, ADR-0008)."""

from dataclasses import asdict

import pytest

from conftest import FOOTPRINT_IRS, NETLIST_IR, OUTLINE, STACKUP, make_spec
from net2board.boardspec import BoardSpec, BoardSpecError, PinPadBindingError
from net2board.build import build_board
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import Board


def _contains_instance(value, cls) -> bool:
    if isinstance(value, cls):
        return True
    if isinstance(value, dict):
        return any(_contains_instance(v, cls) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_instance(v, cls) for v in value)
    return False


class TestBuildBoardJoin:
    def test_returns_a_board_with_outline_and_stackup_absorbed(self):
        board = build_board(make_spec())
        assert isinstance(board, Board)
        assert board.outline == OUTLINE
        assert board.stackup == STACKUP

    def test_board_does_not_retain_the_spec(self):
        board = build_board(make_spec())
        serializable = asdict(board)
        assert not _contains_instance(serializable, BoardSpec)

    def test_no_placements_yet(self):
        assert build_board(make_spec()).placements == ()

    def test_each_component_resolves_its_footprint(self):
        board = build_board(make_spec())
        r1 = next(c for c in board.components if c.ref == "R1")
        r3 = next(c for c in board.components if c.ref == "R3")
        assert [p.number for p in r1.footprint.pads] == ["1", "2", "9"]
        assert isinstance(
            r3.footprint.courtyard, type(FOOTPRINT_IRS["BIGRES"].courtyard)
        )

    def test_nets_carry_pin_sets_with_pad_numbers(self):
        board = build_board(make_spec())
        by_name = {n.name: n for n in board.nets}
        assert set(by_name) == {"GND", "N1"}
        assert set(by_name["GND"].pins) == {("R1", "2"), ("R2", "2")}
        assert set(by_name["N1"].pins) == {("R1", "1")}

    def test_extra_pads_stay_silent(self):
        board = build_board(make_spec())
        all_bound_pads = {pin for net in board.nets for pin in net.pins}
        assert ("R1", "9") not in all_bound_pads


class TestPinPadBinding:
    def test_netlist_pin_without_matching_pad_raises_with_carried_fields(self):
        bad_netlist = NetlistIR(
            comps=NETLIST_IR.comps,
            nets=(NetIR(name="BAD", nodes=(("R1", "7"),)),),
        )
        with pytest.raises(PinPadBindingError) as excinfo:
            build_board(make_spec(netlist_ir=bad_netlist))
        assert excinfo.value.ref == "R1"
        assert excinfo.value.pin_number == "7"
        assert "R1" in str(excinfo.value)
        assert "7" in str(excinfo.value)

    def test_pin_pad_binding_error_is_a_boardspec_error(self):
        assert issubclass(PinPadBindingError, BoardSpecError)

    def test_pads_without_netlist_pins_are_not_an_error(self):
        board = build_board(make_spec())  # RES has extra pad "9"
        assert board.nets  # join completed


class TestSpecConsistency:
    def test_node_referencing_unknown_ref_raises(self):
        bad_netlist = NetlistIR(
            comps=NETLIST_IR.comps,
            nets=(NetIR(name="X", nodes=(("NOPE", "1"),)),),
        )
        with pytest.raises(ValueError, match="NOPE"):
            build_board(make_spec(netlist_ir=bad_netlist))

    def test_comp_with_missing_footprint_ir_raises(self):
        bad_netlist = NetlistIR(
            comps=NETLIST_IR.comps + (CompIR(ref="R9", entry_name="GHOST"),),
            nets=NETLIST_IR.nets,
        )
        with pytest.raises(ValueError, match="GHOST"):
            build_board(make_spec(netlist_ir=bad_netlist))


class TestSpecValueSemantics:
    def test_boardspec_is_frozen(self):
        spec = make_spec()
        with pytest.raises(Exception, match="assign"):
            spec.outline = OUTLINE

    def test_replayed_builds_compare_equal(self):
        assert build_board(make_spec()) == build_board(make_spec())


# IR construction sanity (the conftest world itself): fields exist as pinned.
def test_ir_types_carry_pinned_fields():
    comp = NETLIST_IR.comps[0]
    assert (comp.ref, comp.entry_name) == ("R1", "RES")
    net = NETLIST_IR.nets[0]
    assert (net.name, tuple(net.nodes)) == ("GND", (("R1", "2"), ("R2", "2")))
    pad: PadIR = FOOTPRINT_IRS["RES"].pads[0]
    assert (pad.number, pad.shape_enum) == ("1", "circle")
    fp: FootprintIR = FOOTPRINT_IRS["BIGRES"]
    assert fp.courtyard is not None and fp.courtyard.radius_sq == 10_000_000_000_000
