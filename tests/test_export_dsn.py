"""DSN export unit tests: canonical, byte-deterministic Specctra text.

Every frame convention here is probe-pinned against pcbnew's own DSN
writer and the freerouting-2.4.1 jar (ticket #35 session): x verbatim,
y negated everywhere (placements, image pins, boundary), rotation
verbatim, front-canonical images with the ``back`` token doing the
mirror, ``F.Cu``/``B.Cu`` layer names verbatim, quoted tokens with no
``string_quote`` declaration. The world is the shared hand-built IR
(R1/R2 on ``RES`` thru-hole ``*.Cu`` pads, R3 on ``BIGRES``; nets
``GND`` joining R1-2/R2-2 and ``N1`` holding R1-1 alone).
"""

import pytest

from conftest import OUTLINE, STACKUP, fresh_board, make_spec
from net2board.export import DsnRules, export_dsn
from net2board.ir._sexpr import read_sexpr

R1_PLACED = fresh_board().with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
R2_PLACED = fresh_board().with_placement("R2", 8_000_000, 6_000_000, 0, "F.Cu")


def board_with(entry, pads, nets=()):
    """A one-component board over hand-built IR — the fallback path."""
    from net2board.boardspec import BoardSpec
    from net2board.build import build_board
    from net2board.ir import CompIR, FootprintIR, NetlistIR

    spec = BoardSpec(
        netlist_ir=NetlistIR(
            comps=(CompIR(ref="U1", entry_name=entry),),
            nets=nets,
        ),
        footprint_irs={entry: FootprintIR(entry_name=entry, pads=pads, courtyard=None)},
        outline=OUTLINE,
        stackup=STACKUP,
    )
    return build_board(spec).with_placement("U1", 0, 0, 0, "F.Cu")


class TestPurityAndSurface:
    def test_same_board_same_bytes(self):
        assert export_dsn(R1_PLACED) == export_dsn(R1_PLACED)

    def test_placement_order_does_not_leak_into_the_bytes(self):
        one_order = (
            fresh_board()
            .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
            .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
        )
        other_order = (
            fresh_board()
            .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
            .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        )
        assert export_dsn(one_order) == export_dsn(other_order)

    def test_export_performs_no_disk_io(self, monkeypatch):
        def no_open(*args, **kwargs):
            raise AssertionError("export must not touch the filesystem")

        monkeypatch.setattr("builtins.open", no_open)
        export_dsn(R1_PLACED)

    def test_text_ends_with_closing_paren_and_newline(self):
        assert export_dsn(R1_PLACED).endswith(")\n")


class TestStructure:
    def test_header_resolution_and_layer_block(self):
        text = export_dsn(R1_PLACED)
        assert text.startswith("(pcb net2board\n\t(resolution um 1)\n")
        assert "\t\t(layer F.Cu (type signal))" in text
        assert "\t\t(layer B.Cu (type signal))" in text

    def test_inner_stackup_layers_carry_through(self):
        from conftest import load_tiny_spec
        from net2board.build import build_board

        board = build_board(load_tiny_spec())
        first = board.components[0].ref
        text = export_dsn(board.with_placement(first, 1_000_000, 1_000_000, 0, "F.Cu"))
        assert "\t\t(layer In1.Cu (type signal))" in text
        assert "\t\t(layer In2.Cu (type signal))" in text

    def test_unsupported_stackup_raises(self):
        board = R1_PLACED
        broken = type(board)(
            components=board.components,
            placements=board.placements,
            nets=board.nets,
            outline=board.outline,
            stackup=("F.Cu",),
        )
        with pytest.raises(ValueError, match="stackup"):
            export_dsn(broken)

    def test_boundary_is_a_closed_polygon_with_negated_y(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t\t(path pcb 0 0 0 40000 0 40000 -30000 0 -30000 0 0)" in text

    def test_default_rules_land_in_both_rule_blocks(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t\t(width 250)" in text
        assert "\t\t\t(clearance 250)" in text
        assert "\t\t\t\t(width 250)" in text

    def test_custom_rules_reach_both_rule_blocks_and_the_via(self):
        rules = DsnRules(
            width_nm=300_000, clearance_nm=150_000, via_diameter_nm=1_000_000
        )
        text = export_dsn(R1_PLACED, rules)
        assert "\t\t\t(width 300)" in text
        assert "\t\t\t(clearance 150)" in text
        assert "\t\t(via Via_1000)" in text
        assert "\t\t\t\t(use_via Via_1000)" in text

    def test_nonpositive_rules_raise(self):
        with pytest.raises(ValueError, match="positive"):
            DsnRules(width_nm=0)
        with pytest.raises(ValueError, match="positive"):
            DsnRules(clearance_nm=-1)
        with pytest.raises(ValueError, match="positive"):
            DsnRules(via_diameter_nm=0)

    def test_via_padstack_spans_the_stackup(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t(padstack Via_800" in text
        assert "\t\t\t(shape (circle F.Cu 800))" in text
        assert "\t\t\t(shape (circle B.Cu 800))" in text


class TestPlacementAndImages:
    def test_place_carries_verbatim_x_rotation_and_negated_y(self):
        board = fresh_board().with_placement("R1", 8_123_456, -6_000_000, 90, "F.Cu")
        text = export_dsn(board)
        assert "\t\t\t(place R1 8123 6000 front 90)" in text

    def test_back_side_place_token_and_global_pin_y_rule(self):
        """The probe-pinned back-side contract: image pins negate y
        like every DSN coordinate, front and back alike — that is what
        lands Freerouting's ``back`` placement on the same world our
        ``.kicad_pcb`` export renders (probed with an off-axis-pad
        footprint at rot 90; the y-verbatim variant leaves its nets
        dangling). x is never pre-mirrored, unlike the ``.kicad_pcb``
        exporter which must bake the mirror in."""
        board = fresh_board().with_placement("R1", 0, 0, 0, "B.Cu")
        text = export_dsn(board)
        assert "\t\t\t(place R1 0 0 back 0)" in text
        assert "\t\t\t(pin Pad_oval_1600x1600_F-B 2 7620 0)" in text
        assert "-7620" not in text
        # pad 9, local (-1, +2) mm: y negates on the back image too
        assert "\t\t\t(pin Pad_circle_900x900_F-B 9 -1000 -2000)" in text

    def test_same_entry_on_both_sides_yields_two_images(self):
        board = (
            fresh_board()
            .with_placement("R1", 0, 0, 0, "F.Cu")
            .with_placement("R2", 0, 0, 0, "B.Cu")
        )
        text = export_dsn(board)
        assert "\t\t(component RES" in text
        assert "\t\t(component RES::back" in text

    def test_places_sorted_by_ref_within_their_image(self):
        board = (
            fresh_board()
            .with_placement("R2", 20_000_000, 6_000_000, 0, "F.Cu")
            .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
        )
        text = export_dsn(board)
        assert text.index("(place R1") < text.index("(place R2")

    def test_unplaced_components_have_no_dsn_form(self):
        text = export_dsn(R1_PLACED)
        assert "(place R2" not in text
        assert "(place R3" not in text
        assert "BIGRES" not in text

    def test_pins_carry_ref_pad_atoms_for_placed_refs_only(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t\t(pins R1-2)" in text  # GND's placed pin, unplaced R2 dropped
        assert "R2-2" not in text


class TestLibrary:
    def test_thru_hole_padstack_expands_wildcard_to_stackup(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t(padstack Pad_circle_1600x1600_F-B" in text
        assert "\t\t\t(shape (circle F.Cu 1600))" in text
        assert "\t\t\t(shape (circle B.Cu 1600))" in text

    def test_oval_square_is_the_degenerate_path(self):
        text = export_dsn(R1_PLACED)
        assert "\t\t(padstack Pad_oval_1600x1600_F-B" in text
        assert "\t\t\t(shape (path F.Cu 1600 0 0 0 0))" in text

    def test_oval_long_axis_is_a_centered_segment(self):
        from net2board.geometry import Point
        from net2board.ir import PadIR

        board = board_with(
            "OVAL",
            (
                PadIR(
                    number="1",
                    local_pos=Point(0, 0),
                    size=(2_000_000, 1_000_000),
                    drill=None,
                    layers=("F.Cu",),
                    shape_enum="oval",
                ),
            ),
        )
        text = export_dsn(board)
        assert "\t\t(padstack Pad_oval_2000x1000_F" in text
        assert "\t\t\t(shape (path F.Cu 1000 -500 0 500 0))" in text

    def test_unknown_shape_falls_back_to_bounding_rect(self):
        from net2board.geometry import Point
        from net2board.ir import PadIR

        board = board_with(
            "ODD",
            (
                PadIR(
                    number="1",
                    local_pos=Point(0, 0),
                    size=(1_200_000, 800_000),
                    drill=None,
                    layers=("F.Cu",),
                    shape_enum="roundrect",
                ),
            ),
        )
        text = export_dsn(board)
        assert "\t\t(padstack Pad_rect_1200x800_F" in text
        assert "\t\t\t(shape (rect F.Cu -600 -400 600 400))" in text

    def test_smd_padstack_is_single_layer_and_side_remapped(self):
        from net2board.geometry import Point
        from net2board.ir import PadIR

        pad = PadIR(
            number="1",
            local_pos=Point(0, 0),
            size=(1_200_000, 600_000),
            drill=None,
            layers=("F.Cu", "F.Mask"),
            shape_enum="rect",
        )
        front = board_with("SMD", (pad,))
        text = export_dsn(front)
        assert "\t\t(padstack Pad_rect_1200x600_F" in text
        assert "(shape (rect B.Cu" not in text

        back = front.with_placement("U1", 0, 0, 0, "B.Cu")
        back_text = export_dsn(back)
        assert "\t\t(padstack Pad_rect_1200x600_B" in back_text

    def test_image_pin_y_is_negated(self):
        text = export_dsn(fresh_board().with_placement("R3", 0, 0, 0, "F.Cu"))
        assert "\t\t\t(pin Pad_circle_2000x2000_F-B 2 10000 0)" in text


class TestNetwork:
    def test_nets_sorted_with_sorted_pins(self):
        text = export_dsn(
            fresh_board()
            .with_placement("R1", 0, 0, 0, "F.Cu")
            .with_placement("R2", 0, 0, 0, "F.Cu")
        )
        assert text.index("(net GND") < text.index("(net N1")
        assert "\t\t\t(pins R1-2 R2-2)" in text

    def test_net_left_with_no_placed_pin_is_dropped_everywhere(self):
        text = export_dsn(R2_PLACED)  # N1 holds only R1-1: unplaced
        assert "(net N1" not in text
        assert "\t\t(class DEFAULT GND" in text

    def test_no_nets_means_no_class(self):
        from net2board.geometry import Point
        from net2board.ir import PadIR

        board = board_with(
            "SMD",
            (
                PadIR(
                    number="1",
                    local_pos=Point(0, 0),
                    size=(1_200_000, 600_000),
                    drill=None,
                    layers=("F.Cu",),
                    shape_enum="rect",
                ),
            ),
        )
        assert "(class" not in export_dsn(board)

    def test_empty_net_name_raises(self):
        from net2board.boardspec import BoardSpec
        from net2board.build import build_board
        from net2board.ir import CompIR, NetIR, NetlistIR

        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="R1", entry_name="RES"),),
                nets=(NetIR(name="", nodes=(("R1", "1"),)),),
            ),
            footprint_irs=make_spec().footprint_irs,
            outline=OUTLINE,
            stackup=STACKUP,
        )
        board = build_board(spec).with_placement("R1", 0, 0, 0, "F.Cu")
        with pytest.raises(ValueError, match="empty name"):
            export_dsn(board)

    def test_parens_in_net_name_force_quoting(self):
        from net2board.boardspec import BoardSpec
        from net2board.build import build_board
        from net2board.ir import CompIR, NetIR, NetlistIR

        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="R1", entry_name="RES"),),
                nets=(NetIR(name="Net-(R1-P1)", nodes=(("R1", "1"),)),),
            ),
            footprint_irs=make_spec().footprint_irs,
            outline=OUTLINE,
            stackup=STACKUP,
        )
        board = build_board(spec).with_placement("R1", 0, 0, 0, "F.Cu")
        text = export_dsn(board)
        assert '\t\t(net "Net-(R1-P1)"' in text
        assert '\t\t(class DEFAULT "Net-(R1-P1)"' in text
        assert read_sexpr(text)[0] == "pcb"

    def test_quote_in_name_is_unrepresentable(self):
        from net2board.boardspec import BoardSpec
        from net2board.build import build_board
        from net2board.ir import CompIR, NetIR, NetlistIR

        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="R1", entry_name="RES"),),
                nets=(NetIR(name='bad"name', nodes=(("R1", "1"),)),),
            ),
            footprint_irs=make_spec().footprint_irs,
            outline=OUTLINE,
            stackup=STACKUP,
        )
        board = build_board(spec).with_placement("R1", 0, 0, 0, "F.Cu")
        with pytest.raises(ValueError, match="double quote"):
            export_dsn(board)

    @pytest.mark.parametrize("bad_ref,bad_pad", [("R 1", "1"), ("R1", "A B")])
    def test_unrepresentable_pin_atoms_raise(self, bad_ref, bad_pad):
        from net2board.boardspec import BoardSpec
        from net2board.build import build_board
        from net2board.geometry import Point
        from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR

        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref=bad_ref, entry_name="WEIRD"),),
                nets=(NetIR(name="N1", nodes=((bad_ref, bad_pad),)),),
            ),
            footprint_irs={
                "WEIRD": FootprintIR(
                    entry_name="WEIRD",
                    pads=(
                        PadIR(
                            number=bad_pad,
                            local_pos=Point(0, 0),
                            size=(1_000_000, 1_000_000),
                            drill=None,
                            layers=("F.Cu",),
                            shape_enum="circle",
                        ),
                    ),
                    courtyard=None,
                )
            },
            outline=OUTLINE,
            stackup=STACKUP,
        )
        board = build_board(spec).with_placement(bad_ref, 0, 0, 0, "F.Cu")
        with pytest.raises(ValueError, match="pin id"):
            export_dsn(board)

    def test_stripped_characters_vanish_everywhere(self):
        from net2board.boardspec import BoardSpec
        from net2board.build import build_board
        from net2board.ir import CompIR, NetIR, NetlistIR

        spec = BoardSpec(
            netlist_ir=NetlistIR(
                comps=(CompIR(ref="Rµ1", entry_name="RES"),),
                nets=(NetIR(name="NΩ1", nodes=(("Rµ1", "1"),)),),
            ),
            footprint_irs=make_spec().footprint_irs,
            outline=OUTLINE,
            stackup=STACKUP,
        )
        board = build_board(spec).with_placement("Rµ1", 0, 0, 0, "F.Cu")
        text = export_dsn(board)
        assert "\t\t\t(place R1 0 0 front 0)" in text
        assert "\t\t\t(pins R1-1)" in text
        assert "(net N1" in text
        assert "µ" not in text and "Ω" not in text


class TestUnits:
    def test_um_rounds_half_away_from_zero(self):
        board = fresh_board().with_placement("R1", 1_499_999, -1_499_999, 0, "F.Cu")
        assert "\t\t\t(place R1 1500 1500 front 0)" in export_dsn(board)

        # 1500.499 stays down, 1500.500 rounds up; -2500.499 → 2500 on
        # the negated axis
        board = fresh_board().with_placement("R1", 1_500_499, -2_500_499, 0, "F.Cu")
        assert "\t\t\t(place R1 1500 2500 front 0)" in export_dsn(board)
        board = fresh_board().with_placement("R1", 1_500_500, -2_500_500, 0, "F.Cu")
        assert "\t\t\t(place R1 1501 2501 front 0)" in export_dsn(board)


class TestRoundTrip:
    def test_output_parses_under_read_sexpr(self):
        forms = read_sexpr(export_dsn(R1_PLACED))
        assert isinstance(forms, list)
        assert forms[0] == "pcb"

    def test_carries_every_placed_ref_and_net(self):
        board = (
            fresh_board()
            .with_placement("R1", 8_000_000, 6_000_000, 0, "F.Cu")
            .with_placement("R2", 20_000_000, 6_000_000, 90, "F.Cu")
        )
        forms = read_sexpr(export_dsn(board))
        placed_refs = {placement.ref for placement in board.placements}
        net_names = {net.name for net in board.nets}
        seen_refs, seen_nets = set(), set()

        def walk(form):
            if not isinstance(form, list):
                return
            if form and form[0] == "place":
                seen_refs.add(form[1])
            if form and form[0] == "net" and len(form) > 1:
                seen_nets.add(form[1])
            for item in form:
                walk(item)

        walk(forms)
        assert seen_refs == placed_refs
        assert seen_nets == net_names
