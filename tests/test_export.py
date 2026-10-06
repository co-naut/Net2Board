"""S3 — export unit tests: canonical, byte-deterministic text (ADR-0010).

Every assertion is over the serialized text — external behavior only. The
world is the shared hand-built IR from conftest (R1/R2 on ``RES``, R3 on
``BIGRES``; nets ``GND`` and ``N1``), so net ids are pinned as
``GND → 1``, ``N1 → 2`` by lexicographic order, and the extra pad ``"9"``
on ``RES`` exercises the unconnected ``(net 0 "")`` path.
"""

import pytest

from conftest import OUTLINE, STACKUP, fresh_board, make_spec
from net2board import export
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.export import export_pcb
from net2board.geometry import Point
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR

R1_PLACED = fresh_board().with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")


def test_surface_is_exactly_the_export_functions():
    assert export.__all__ == ["DsnRules", "export_dsn", "export_pcb"]


def test_version_header_is_first_child():
    assert export_pcb(R1_PLACED).startswith(
        "(kicad_pcb\n"
        "\t(version 20241229)\n"
        '\t(generator "net2board")\n'
        '\t(generator_version "1")\n'
    )


def test_same_board_same_bytes():
    assert export_pcb(R1_PLACED) == export_pcb(R1_PLACED)


def test_placement_order_does_not_leak_into_the_bytes():
    one_order = (
        fresh_board()
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R3", 8_000_000, 20_000_000, 0, "F.Cu")
    )
    other_order = (
        fresh_board()
        .with_placement("R3", 8_000_000, 20_000_000, 0, "F.Cu")
        .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
    )
    assert export_pcb(one_order) == export_pcb(other_order)


def test_export_performs_no_disk_io(monkeypatch):
    def no_open(*args, **kwargs):
        raise AssertionError("export must not touch the filesystem")

    monkeypatch.setattr("builtins.open", no_open)
    export_pcb(R1_PLACED)


def test_layers_block_carries_stackup_and_user_table():
    text = export_pcb(R1_PLACED)
    assert '\t\t(0 "F.Cu" signal)' in text
    assert '\t\t(2 "B.Cu" signal)' in text
    assert '\t\t(25 "Edge.Cuts" user)' in text
    assert '\t\t(31 "F.CrtYd" user)' in text


def test_unsupported_stackup_raises():
    with pytest.raises(ValueError, match="stackup"):
        export_pcb(_board_with_stackup(("F.Cu",)))
    with pytest.raises(ValueError, match="stackup"):
        export_pcb(_board_with_stackup(("F.Cu", "X.Cu", "B.Cu")))


def _board_with_stackup(stackup):
    return build_board(
        BoardSpec(
            netlist_ir=make_spec().netlist_ir,
            footprint_irs=make_spec().footprint_irs,
            outline=OUTLINE,
            stackup=stackup,
        )
    ).with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")


def test_nets_declared_lexicographically_with_net0_first():
    text = export_pcb(R1_PLACED)
    assert '\t(net 0 "")' in text
    assert '\t(net 1 "GND")' in text
    assert '\t(net 2 "N1")' in text
    assert "(net 3 " not in text
    assert text.index('\t(net 0 "")') < text.index('\t(net 1 "GND")')
    assert text.index('\t(net 1 "GND")') < text.index('\t(net 2 "N1")')


def test_nets_stay_declared_when_no_placed_pad_uses_them():
    board = fresh_board().with_placement("R3", 8_000_000, 20_000_000, 0, "F.Cu")
    text = export_pcb(board)
    assert '\t(net 1 "GND")' in text
    assert '\t(net 2 "N1")' in text


def test_empty_named_net_colliding_with_net0_raises():
    spec = BoardSpec(
        netlist_ir=NetlistIR(
            comps=(CompIR(ref="R1", entry_name="RES"),),
            nets=(NetIR(name="", nodes=(("R1", "1"),)),),
        ),
        footprint_irs=make_spec().footprint_irs,
        outline=OUTLINE,
        stackup=STACKUP,
    )
    board = build_board(spec).with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
    with pytest.raises(ValueError, match="empty name"):
        export_pcb(board)


def test_footprints_sorted_by_reference_designator():
    board = (
        fresh_board()
        .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R3", 8_000_000, 20_000_000, 0, "F.Cu")
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
    )
    text = export_pcb(board)
    assert (
        text.index(
            '\t(footprint "Net2Board:RES"\n\t\t(layer "F.Cu")\n\t\t(at 8.000000 6.000000 0)'
        )
        < text.index(
            '\t(footprint "Net2Board:RES"\n\t\t(layer "F.Cu")\n\t\t(at 20.000000 6.000000 0)'
        )
        < text.index('\t(footprint "Net2Board:BIGRES"')
    )


def test_unplaced_components_are_not_emitted():
    text = export_pcb(R1_PLACED)
    assert text.count("\t(footprint ") == 1
    assert "BIGRES" not in text


def test_outline_emitted_as_edge_cuts_rect():
    text = export_pcb(R1_PLACED)
    assert "\t(gr_rect" in text
    assert "\t\t(start 0.000000 0.000000)" in text
    assert "\t\t(end 40.000000 30.000000)" in text
    assert '\t\t(layer "Edge.Cuts")' in text


def test_reference_property_names_every_footprint():
    """The identity property — the SES re-import join key (ADR-0017)."""
    board = (
        fresh_board()
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        .with_placement("R3", 8_000_000, 20_000_000, 0, "B.Cu")
    )
    text = export_pcb(board)
    assert (
        '\t\t(property "Reference" "R1"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")'
        in text
    )
    assert (
        '\t\t(property "Reference" "R3"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "B.SilkS")'
        in text
    )


def test_rect_courtyard_emitted_on_front_courtyard_layer():
    text = export_pcb(R1_PLACED)
    assert "\t\t(fp_rect" in text
    assert "\t\t\t(start -1.050000 -1.500000)" in text
    assert "\t\t\t(end 8.670000 1.500000)" in text
    assert '\t\t\t(layer "F.CrtYd")' in text


def test_circle_courtyard_carries_its_defining_end_point():
    board = fresh_board().with_placement("R3", 0, 0, 0, "F.Cu")
    text = export_pcb(board)
    assert "\t\t(fp_circle" in text
    assert "\t\t\t(center 5.000000 0.000000)" in text
    assert "\t\t\t(end 8.000000 1.000000)" in text


def test_pads_in_library_order_with_net_assignments():
    text = export_pcb(R1_PLACED)
    pad_1 = text.index('\t\t(pad "1" thru_hole circle')
    pad_2 = text.index('\t\t(pad "2" thru_hole oval')
    pad_9 = text.index('\t\t(pad "9" thru_hole circle')
    assert pad_1 < pad_2 < pad_9
    r1_block = text[pad_1 : text.index("\t)", pad_9)]
    assert "\t\t\t(at 0.000000 0.000000)" in r1_block
    assert "\t\t\t(size 1.600000 1.600000)" in r1_block
    assert "\t\t\t(drill 0.800000)" in r1_block
    assert '\t\t\t(layers "*.Cu" "*.Mask")' in r1_block
    assert '\t\t\t(net 2 "N1")' in r1_block
    assert '\t\t\t(net 1 "GND")' in r1_block
    assert '\t\t\t(net 0 "")' in r1_block


def test_mm_rendering_six_decimals_negative_and_zero():
    board = fresh_board().with_placement("R1", -1_500_000, 0, 0, "F.Cu")
    text = export_pcb(board)
    assert "\t\t(at -1.500000 0.000000 0)" in text
    assert "-0.000000" not in text
    assert "e-" not in text and "E-" not in text


def test_back_side_mirror_is_baked_into_emitted_locals():
    board = fresh_board().with_placement("R1", 0, 0, 0, "B.Cu")
    text = export_pcb(board)
    assert '\t\t(layer "B.Cu")' in text
    assert "\t\t\t(at -7.620000 0.000000)" in text
    assert "\t\t\t(start -8.670000 -1.500000)" in text
    assert "\t\t\t(end 1.050000 1.500000)" in text


def test_courtyard_layer_follows_placement_side():
    front = export_pcb(R1_PLACED)
    assert '\t\t\t(layer "F.CrtYd")' in front
    back = export_pcb(fresh_board().with_placement("R1", 0, 0, 0, "B.Cu"))
    assert '\t\t\t(layer "B.CrtYd")' in back
    assert '(layer "F.CrtYd")' not in back


def test_back_side_circle_mirrors_center_and_end():
    board = fresh_board().with_placement("R3", 0, 0, 0, "B.Cu")
    text = export_pcb(board)
    assert "\t\t\t(center -5.000000 0.000000)" in text
    assert "\t\t\t(end -8.000000 1.000000)" in text


def test_rotation_emitted_in_at_form():
    board = fresh_board().with_placement("R1", 8_000_000, 6_000_000, 90, "F.Cu")
    assert "\t\t(at 8.000000 6.000000 90)" in export_pcb(board)


def test_smd_pad_without_drill_and_layer_remap_on_back_side():
    smd_ir = FootprintIR(
        entry_name="SMDTEST",
        pads=(
            PadIR(
                number="1",
                local_pos=Point(1_000_000, -1_000_000),
                size=(1_200_000, 600_000),
                drill=None,
                layers=("F.Cu", "F.Mask"),
                shape_enum="rect",
            ),
        ),
        courtyard=None,
    )
    spec = BoardSpec(
        netlist_ir=NetlistIR(comps=(CompIR(ref="S1", entry_name="SMDTEST"),), nets=()),
        footprint_irs={"SMDTEST": smd_ir},
        outline=OUTLINE,
        stackup=STACKUP,
    )
    board = build_board(spec).with_placement("S1", 5_000_000, 5_000_000, 0, "F.Cu")
    text = export_pcb(board)
    assert '\t\t(pad "1" smd rect' in text
    assert "(drill" not in text
    assert '\t\t\t(layers "F.Cu" "F.Mask")' in text

    back = board.with_placement("S1", 5_000_000, 5_000_000, 0, "B.Cu")
    back_text = export_pcb(back)
    assert '\t\t\t(layers "B.Cu" "B.Mask")' in back_text
    assert "\t\t\t(at -1.000000 -1.000000)" in back_text


def test_text_ends_with_closing_paren_and_newline():
    assert export_pcb(R1_PLACED).endswith(")\n")


def test_unlocked_footprints_carry_no_lock_token():
    assert "locked" not in export_pcb(R1_PLACED)


def test_locked_footprint_emits_the_lock_token():
    """``(locked yes)`` in pcbnew 10's own layout — first token line
    inside the footprint block, before ``(layer …)``. Probed on
    kicad-cli 10.0.6: parses, and ``IsLocked()`` reads back True.
    """
    board = fresh_board().with_placement(
        "R1", 8_000_000, 6_000_000, 0, "F.Cu", locked=True
    )
    text = export_pcb(board)
    assert '\t(footprint "Net2Board:RES"\n\t\t(locked yes)\n\t\t(layer "F.Cu")' in text


def test_only_the_locked_footprint_is_marked():
    board = (
        fresh_board()
        .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu", locked=True)
        .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
    )
    text = export_pcb(board)
    assert text.count("\t\t(locked yes)") == 1
    r1_opens = text.index(
        '\t(footprint "Net2Board:RES"\n\t\t(layer "F.Cu")\n\t\t(at 8.000000 6.000000 0)'
    )
    r2_opens = text.index(
        '\t(footprint "Net2Board:RES"\n'
        "\t\t(locked yes)\n"
        '\t\t(layer "F.Cu")\n'
        "\t\t(at 20.000000 6.000000 0)"
    )
    assert r1_opens < r2_opens
