"""The pad-copper checks — ``shorting_items``, ``clearance``,
``copper_edge_clearance`` (ADR-0022, #91).

One geometry, KiCad's split: different-net capsules that intersect short,
a gap strictly below ``DesignRules.clearance_nm`` (touching included) is a
clearance, pad copper within ``edge_clearance_nm`` of an outline edge line
is an edge finding. Exemptions: same-net pairs, net-tie pad groups, a
Requirement EdgeMount's stated edge. Intra-footprint pairs are checked
(KiCad checks them), one finding per pad pair (or pad), locations at the
pad world centres, ``offending_refs`` staying placement-form — repeated
for an intra-footprint pair. The rule is strict: a gap equal to the rule
is clean.

The fixture pads are 1 mm squares; a pad's face sits half a width from
its centre, so a gap of g between two front pads on one row means
centres 1 mm + g apart.
"""

from collections import Counter

from net2board.drc import (
    CHECKS,
    COMPANIONS,
    Severity,
    ViolationType,
    _pad_at,
    _pad_pair_exempt,
    _pad_pair_price,
    _PadCopper,
    run_drc,
)
from net2board.geometry import Capsule, Edge, Point, Rectangle, world_point
from net2board.model import (
    Board,
    Component,
    Constraint,
    DesignRules,
    EdgeMount,
    Footprint,
    Net,
    Pad,
    Placement,
    Preference,
    Requirement,
)

MM = 1_000_000
OUTLINE = Rectangle(min=Point(0, 0), max=Point(40 * MM, 30 * MM))


def rect_pad(number="1", x=0, y=0, w=MM, h=MM, layers=("F.Cu",), **kw):
    return Pad(
        number=number,
        local_pos=Point(x, y),
        size=(w, h),
        drill=None,
        layers=layers,
        shape_enum="rect",
        **kw,
    )


def footprint(*pads, net_tie_groups=()):
    return Footprint(
        entry_name="T", pads=pads, courtyard=None, net_tie_groups=net_tie_groups
    )


_DEFAULT_RULES = DesignRules()


def board_of(
    *placed, nets=(), stackup=("F.Cu", "B.Cu"), rules=_DEFAULT_RULES, constraints=()
):
    """placed: (ref, footprint, x, y, rotation, side) tuples."""
    components = tuple(Component(ref=ref, footprint=fp) for ref, fp, *_ in placed)
    placements = tuple(
        Placement(ref=ref, footprint=fp, pos=Point(x, y), rotation=rot, side=side)
        for ref, fp, x, y, rot, side in placed
    )
    return Board(
        components=components,
        placements=placements,
        nets=tuple(Net(name=name, pins=pins) for name, pins in nets),
        outline=OUTLINE,
        stackup=stackup,
        design_rules=rules,
        constraints=constraints,
    )


def pad_types(board):
    return [violation.type for violation in run_drc(board)]


def sort_key(violation):
    return (
        violation.type.value,
        violation.offending_refs,
        tuple((point.x, point.y) for point in violation.locations),
    )


def pair_board(row_offset, *, nets, **kw):
    """Two one-pad parts on one row, centres 1 mm + ``row_offset`` apart."""
    return board_of(
        ("A", footprint(rect_pad()), 5 * MM, 5 * MM, 0, "F.Cu"),
        ("B", footprint(rect_pad()), 5 * MM + MM + row_offset, 5 * MM, 0, "F.Cu"),
        nets=nets,
        **kw,
    )


TWO_NETS = (("n1", (("A", "1"),)), ("n2", (("B", "1"),)))


class TestShorting:
    def test_overlapping_different_net_pads_short(self):
        board = pair_board(-500_000, nets=TWO_NETS)  # centres 0.5 mm apart
        shorts = [v for v in run_drc(board) if v.type is ViolationType.SHORTING_ITEMS]
        assert len(shorts) == 1
        finding = shorts[0]
        assert finding.severity is Severity.ERROR
        assert finding.offending_refs == ("placement:A", "placement:B")
        assert finding.locations == (
            Point(5 * MM, 5 * MM),
            Point(5 * MM + 500_000, 5 * MM),
        )
        assert "pad 1" in finding.description

    def test_touching_pads_are_clearance_not_short(self):
        assert pad_types(pair_board(0, nets=TWO_NETS)) == [ViolationType.CLEARANCE]

    def test_same_net_pairs_are_exempt(self):
        board = pair_board(-500_000, nets=(("n1", (("A", "1"), ("B", "1"))),))
        assert pad_types(board) == []

    def test_netless_pads_count_as_their_own_net(self):
        # two fiducial-style pads, no nets at all — checked against
        # everything, each other included
        assert pad_types(pair_board(-500_000, nets=())) == [
            ViolationType.SHORTING_ITEMS
        ]


class TestClearance:
    def test_a_gap_below_the_rule_is_a_finding(self):
        board = pair_board(200_000 - 1, nets=TWO_NETS)
        assert pad_types(board) == [ViolationType.CLEARANCE]

    def test_a_gap_equal_to_the_rule_is_clean(self):
        assert pad_types(pair_board(200_000, nets=TWO_NETS)) == []

    def test_the_description_names_the_pads_and_the_measured_gap(self):
        board = pair_board(110_000, nets=TWO_NETS)
        finding = run_drc(board)[0]
        assert finding.type is ViolationType.CLEARANCE
        assert "placement:A pad 1" in finding.description
        assert "placement:B pad 1" in finding.description
        assert "110000 nm" in finding.description
        assert "200000 nm" in finding.description

    def test_cross_side_pads_are_not_checked(self):
        board = board_of(
            ("A", footprint(rect_pad()), 5 * MM, 5 * MM, 0, "F.Cu"),
            ("B", footprint(rect_pad()), 5 * MM + 500_000, 5 * MM, 0, "B.Cu"),
            nets=TWO_NETS,
        )
        assert pad_types(board) == []

    def test_paste_only_pads_are_skipped(self):
        board = board_of(
            (
                "A",
                footprint(rect_pad(layers=("F.Paste", "F.Mask"))),
                5 * MM,
                5 * MM,
                0,
                "F.Cu",
            ),
            ("B", footprint(rect_pad()), 5 * MM + 500_000, 5 * MM, 0, "F.Cu"),
            nets=TWO_NETS,
        )
        assert pad_types(board) == []

    def test_star_cu_spans_every_stackup_layer(self):
        # a *.Cu (THT-style) pad on the front meets an F.Cu pad on the
        # back — B.Cu is shared on the four-copper stackup
        board = board_of(
            ("A", footprint(rect_pad(layers=("*.Cu",))), 5 * MM, 5 * MM, 0, "F.Cu"),
            ("B", footprint(rect_pad()), 5 * MM + 500_000, 5 * MM, 0, "B.Cu"),
            stackup=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
            nets=TWO_NETS,
        )
        assert pad_types(board) == [ViolationType.SHORTING_ITEMS]

    def test_front_prefix_swaps_on_the_back(self):
        # both pads state F.Cu; the back-side placement's pad is B.Cu
        # copper — no shared layer on the two-copper stackup
        board = board_of(
            ("A", footprint(rect_pad()), 5 * MM, 5 * MM, 0, "F.Cu"),
            ("B", footprint(rect_pad()), 5 * MM + 500_000, 5 * MM, 0, "B.Cu"),
            nets=TWO_NETS,
        )
        assert pad_types(board) == []


class TestIntraFootprintPairs:
    def test_same_footprint_different_net_pads_are_checked(self):
        a = (
            "A",
            footprint(rect_pad("1"), rect_pad("2", x=500_000)),
            5 * MM,
            5 * MM,
            0,
            "F.Cu",
        )
        board = board_of(a, nets=(("n1", (("A", "1"),)), ("n2", (("A", "2"),))))
        shorts = [v for v in run_drc(board) if v.type is ViolationType.SHORTING_ITEMS]
        assert len(shorts) == 1
        # the repeated ref — the pair lives inside one footprint
        assert shorts[0].offending_refs == ("placement:A", "placement:A")
        assert shorts[0].locations == (
            Point(5 * MM, 5 * MM),
            Point(5 * MM + 500_000, 5 * MM),
        )

    def test_net_tie_groups_exempt_their_own_pairs(self):
        group = (frozenset({"1", "2"}),)
        a = (
            "A",
            footprint(rect_pad("1"), rect_pad("2", x=500_000), net_tie_groups=group),
            5 * MM,
            5 * MM,
            0,
            "F.Cu",
        )
        board = board_of(a, nets=(("n1", (("A", "1"),)), ("n2", (("A", "2"),))))
        assert pad_types(board) == []

    def test_a_group_does_not_exempt_other_pairs(self):
        group = (frozenset({"1", "2"}),)
        pads = (
            rect_pad("1"),
            rect_pad("2", x=500_000),
            rect_pad("3", x=1 * MM),  # not in the group — different net
        )
        a = ("A", footprint(*pads, net_tie_groups=group), 5 * MM, 5 * MM, 0, "F.Cu")
        board = board_of(
            a,
            nets=(
                ("n1", (("A", "1"),)),
                ("n2", (("A", "2"),)),
                ("n3", (("A", "3"),)),
            ),
        )
        shorts = [v for v in run_drc(board) if v.type is ViolationType.SHORTING_ITEMS]
        assert {v.locations for v in shorts} == {
            (Point(5 * MM + 500_000, 5 * MM), Point(5 * MM + 1 * MM, 5 * MM))
        }


class TestTheVerdict:
    """The one pad-pair verdict (#103): ``_pad_pair_exempt`` and
    ``_pad_pair_price`` are the rule's single home — the pair pass, the
    Evaluator's narrow phase, and the intra-footprint entry pricing all
    read them. Table-tested directly; the seeded walks keep proving
    neighbour discovery. The rule is strict: a gap equal to the rule is
    clean."""

    COPPER = Capsule(
        core=Rectangle(min=Point(-500_000, -500_000), max=Point(500_000, 500_000)),
        radius=0,
    )

    @classmethod
    def copper(cls, number="1", *, net=None, layers=("F.Cu",), x=0):
        return _PadCopper(
            index=0,
            number=number,
            pad=rect_pad(number),
            capsule=Capsule(
                core=Rectangle(
                    min=Point(x - 500_000, -500_000),
                    max=Point(x + 500_000, 500_000),
                ),
                radius=0,
            ),
            center=Point(x, 0),
            layers=frozenset(layers),
            net=net,
        )

    def test_overlapping_capsules_short_at_zero(self):
        assert _pad_pair_price(
            self.COPPER, self.copper(x=300_000).capsule, 200_000
        ) == (
            "short",
            0,
        )

    def test_touching_capsules_price_a_zero_gap_clearance(self):
        assert _pad_pair_price(self.COPPER, self.copper(x=MM).capsule, 200_000) == (
            "clearance",
            0,
        )

    def test_a_gap_equal_to_the_rule_is_clean(self):
        assert (
            _pad_pair_price(self.COPPER, self.copper(x=MM + 200_000).capsule, 200_000)
            is None
        )

    def test_a_gap_one_nm_below_the_rule_is_a_clearance(self):
        assert _pad_pair_price(
            self.COPPER, self.copper(x=MM + 199_999).capsule, 200_000
        ) == ("clearance", 199_999)

    def test_a_gap_past_the_rule_is_clean(self):
        assert (
            _pad_pair_price(self.COPPER, self.copper(x=2 * MM).capsule, 200_000) is None
        )

    def test_no_shared_copper_layer_is_exempt(self):
        pa = self.copper(layers=("F.Cu",))
        pb = self.copper(layers=("B.Cu",))
        assert _pad_pair_exempt(pa, pb, None)

    def test_same_net_is_exempt(self):
        pa = self.copper(net="n1")
        pb = self.copper(net="n1")
        assert _pad_pair_exempt(pa, pb, None)

    def test_netless_pads_exempt_nothing(self):
        assert not _pad_pair_exempt(self.copper(), self.copper(number="2"), None)

    def test_net_tie_exempts_only_intra_footprint(self):
        tied = footprint(rect_pad("1"), rect_pad("2"), net_tie_groups=(("1", "2"),))
        pa = self.copper("1", net="n1")
        pb = self.copper("2", net="n2")
        assert _pad_pair_exempt(pa, pb, tied)
        assert not _pad_pair_exempt(pa, pb, None)

    def test_unrelated_pads_are_not_exempt(self):
        assert not _pad_pair_exempt(
            self.copper("1", net="n1"), self.copper("2", net="n2"), None
        )


class TestOneFindingPerPadPair:
    def test_several_pairs_between_the_same_two_placements(self):
        pads = (rect_pad("1"), rect_pad("2", y=2 * MM))
        board = board_of(
            ("A", footprint(*pads), 5 * MM, 5 * MM, 0, "F.Cu"),
            ("B", footprint(*pads), 5 * MM + 500_000, 5 * MM, 0, "F.Cu"),
            nets=(
                ("n1", (("A", "1"),)),
                ("n2", (("A", "2"),)),
                ("n3", (("B", "1"),)),
                ("n4", (("B", "2"),)),
            ),
        )
        shorts = [v for v in run_drc(board) if v.type is ViolationType.SHORTING_ITEMS]
        assert len(shorts) == 2
        assert {v.locations[0].y for v in shorts} == {5 * MM, 7 * MM}


class TestCopperEdgeClearance:
    def test_a_pad_within_the_edge_rule_is_a_finding(self):
        # pad face 0.3 mm from the left edge, rule 0.5 mm
        board = board_of(("A", footprint(rect_pad()), 800_000, 5 * MM, 0, "F.Cu"))
        edges = [
            v for v in run_drc(board) if v.type is ViolationType.COPPER_EDGE_CLEARANCE
        ]
        assert len(edges) == 1
        assert edges[0].offending_refs == ("placement:A",)
        assert edges[0].locations == (Point(800_000, 5 * MM),)
        assert edges[0].severity is Severity.ERROR

    def test_a_pad_exactly_at_the_rule_is_clean(self):
        board = board_of(("A", footprint(rect_pad()), 1 * MM, 5 * MM, 0, "F.Cu"))
        assert run_drc(board) == ()

    def test_crossing_counts_as_a_zero_gap(self):
        # face 0.2 mm past the left edge
        board = board_of(("A", footprint(rect_pad()), 300_000, 5 * MM, 0, "F.Cu"))
        edges = [
            v for v in run_drc(board) if v.type is ViolationType.COPPER_EDGE_CLEARANCE
        ]
        assert len(edges) == 1
        assert "0 nm" in edges[0].description

    def test_a_pad_wholly_off_the_board_stays_silent(self):
        board = board_of(("A", footprint(rect_pad()), -3 * MM, 5 * MM, 0, "F.Cu"))
        assert run_drc(board) == ()

    def test_the_description_names_the_pad_and_the_gap(self):
        board = board_of(("A", footprint(rect_pad()), 800_000, 5 * MM, 0, "F.Cu"))
        finding = run_drc(board)[0]
        assert "placement:A pad 1" in finding.description
        assert "300000 nm" in finding.description
        assert "500000 nm" in finding.description


class TestEdgeMountExemption:
    def _mounted(self, kind, x=300_000, y=5 * MM, pad=None):
        # a generous overhang keeps the mount itself met — the copper
        # exemption is the stated edge's alone, regardless of the bound
        mount = EdgeMount(target="placement:A", edge=Edge.LEFT, max_overhang_nm=1 * MM)
        return board_of(
            ("A", footprint(pad or rect_pad()), x, y, 0, "F.Cu"),
            constraints=(Constraint(id="mount", kind=kind, relation=mount),),
        )

    def test_a_requirement_mount_excuses_its_stated_edge(self):
        assert pad_types(self._mounted(Requirement())) == []

    def test_a_preference_mount_exempts_nothing(self):
        assert pad_types(self._mounted(Preference())) == [
            ViolationType.COPPER_EDGE_CLEARANCE
        ]

    def test_an_unstated_edge_is_not_excused_one_finding_per_pad(self):
        # the pad sits in the corner, crossing the stated LEFT edge and
        # the unstated TOP edge — the mount excuses LEFT only, and the
        # pad still gets exactly one edge finding (single-reporting)
        assert pad_types(self._mounted(Requirement(), x=300_000, y=300_000)) == [
            ViolationType.COPPER_EDGE_CLEARANCE
        ]


class TestTheReceiptBoard:
    """#91's receipt pins the tiny_tapeout demo placement: ours reads
    0/0/2 — the two edge findings the C12 pair, at the 0.49 mm gap the
    receipt records (kicad-cli reads the same; the oracle half of the
    pin lives in the slow tier, test_oracle_tinytapeout.py)."""

    def test_demo_placement_reads_0_0_2_with_c12_at_the_receipts_gap(self):
        from conftest import load_tiny_spec
        from net2board.build import build_board
        from net2board.examples.tiny_demo_placements import apply_demo_placements

        demo = apply_demo_placements(build_board(load_tiny_spec()))
        run = run_drc(demo)
        shorts = [v for v in run if v.type is ViolationType.SHORTING_ITEMS]
        clearances = [v for v in run if v.type is ViolationType.CLEARANCE]
        edges = [v for v in run if v.type is ViolationType.COPPER_EDGE_CLEARANCE]
        # the copper receipt: 0/0/2 — the courtyard families' own findings
        # on the human placement are the #73 receipt's separate story
        assert (len(shorts), len(clearances), len(edges)) == (0, 0, 2)
        assert [v.offending_refs for v in edges] == [
            ("placement:C12",),
            ("placement:C12",),
        ]
        for finding in edges:
            assert "placement:C12 pad" in finding.description
            assert "490000 nm" in finding.description
            assert "500000 nm" in finding.description
        # one finding per pad — the two pads, distinct world centres
        assert edges[0].locations != edges[1].locations


class TestCompanionsAreLocalityExact:
    def test_each_companion_returns_exactly_the_refs_findings(self):
        group = (frozenset({"1", "2"}),)
        a = (
            "A",
            footprint(
                rect_pad("1"),
                rect_pad("2", x=500_000),
                net_tie_groups=group,
            ),
            5 * MM,
            5 * MM,
            0,
            "F.Cu",
        )
        b = ("B", footprint(rect_pad()), 5 * MM + 500_000, 5 * MM, 0, "F.Cu")
        c = ("C", footprint(rect_pad()), 800_000, 20 * MM, 0, "F.Cu")
        boards = [
            board_of(
                a,
                b,
                c,
                nets=(
                    ("n1", (("A", "1"),)),
                    ("n2", (("A", "2"),)),
                    ("n3", (("B", "1"),)),
                ),
            ),
            board_of(a, b, c, nets=(("n1", (("A", "1"), ("B", "1"))),)),
        ]
        for board in boards:
            assert run_drc(board), "the fixture board must carry findings"
            for check in CHECKS:
                companion = COMPANIONS.get(check)
                if companion is None:
                    continue
                for placement in board.placements:
                    expected = [
                        v
                        for v in check(board)
                        if f"placement:{placement.ref}" in v.offending_refs
                    ]
                    got = list(companion(board, placement.ref))
                    assert sorted(got, key=sort_key) == sorted(
                        expected, key=sort_key
                    ), (check.__name__, placement.ref)


def check_named(name):
    return next(check for check in CHECKS if check.__name__ == name)


def counted_narrow_phase(monkeypatch):
    """Every exact capsule-pair test the pair pass runs, recorded."""
    import net2board.drc as drc_module

    calls = []
    original = drc_module.capsules_overlap

    def counting(a, b):
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(drc_module, "capsules_overlap", counting)
    return calls


def field_board(far_parts):
    """A two-pad mover A 0.1 mm from B's pad — a clearance finding — plus
    ``far_parts`` one-pad parts parked ≥ 10 mm away, every pad its own net."""
    near = (
        (
            "A",
            footprint(rect_pad("1"), rect_pad("2", x=2 * MM)),
            5 * MM,
            5 * MM,
            0,
            "F.Cu",
        ),
        ("B", footprint(rect_pad()), 5 * MM + 3 * MM + 100_000, 5 * MM, 0, "F.Cu"),
    )
    far = tuple(
        (
            f"F{i}",
            footprint(rect_pad()),
            (20 + 3 * (i % 5)) * MM,
            (18 + 3 * (i // 5)) * MM,
            0,
            "F.Cu",
        )
        for i in range(far_parts)
    )
    return board_of(*near, *far)


class TestThePairPassIsLocal:
    """#98: the pad-pair pass prices only copper that can come within
    ``clearance_nm`` of the moved part, and the shorting and clearance
    checks share one pass — counted narrow-phase calls, never a clock
    (ADR-0016, ADR-0026)."""

    def test_far_parts_cost_a_companion_nothing(self, monkeypatch):
        calls = counted_narrow_phase(monkeypatch)
        companion = COMPANIONS[check_named("_clearance")]
        counts = []
        for far_parts in (0, 10):
            board = field_board(far_parts)
            calls.clear()
            found = companion(board, "A")
            assert [v.offending_refs for v in found] == [("placement:A", "placement:B")]
            counts.append(len(calls))
        assert counts[0] == counts[1]

    def test_far_parts_cost_the_full_check_nothing(self, monkeypatch):
        calls = counted_narrow_phase(monkeypatch)
        check = check_named("_clearance")
        counts = []
        for far_parts in (0, 10):
            board = field_board(far_parts)
            calls.clear()
            assert len(check(board)) == 1
            counts.append(len(calls))
        assert counts[0] == counts[1]

    def test_shorting_and_clearance_companions_share_one_pass(self, monkeypatch):
        calls = counted_narrow_phase(monkeypatch)
        board = field_board(4)
        COMPANIONS[check_named("_clearance")](board, "A")
        one_pass = len(calls)
        assert one_pass > 0
        COMPANIONS[check_named("_shorting_items")](board, "A")
        assert len(calls) == one_pass

    def test_shorting_and_clearance_checks_share_one_pass(self, monkeypatch):
        calls = counted_narrow_phase(monkeypatch)
        board = field_board(4)
        check_named("_shorting_items")(board)
        one_pass = len(calls)
        assert one_pass > 0
        check_named("_clearance")(board)
        assert len(calls) == one_pass


def copper_layers_of(placement, pad, stackup):
    coppers = set(stackup)
    out = set()
    for token in pad.layers:
        if token in ("F.Cu", "B.Cu"):
            if placement.side == "B.Cu":
                token = "B.Cu" if token == "F.Cu" else "F.Cu"
            out.add(token)
        elif token == "*.Cu":
            out |= coppers
        elif token in coppers:
            out.add(token)
    return out


def brute_force_edges(board):
    """Every pad edge finding's location by pricing every copper pad's
    edge gap — the reference the edge check's box skip must reproduce."""
    from net2board.drc import _edge_mount_exemptions, _pad_edge_gap
    from net2board.geometry import world_point

    exempt = _edge_mount_exemptions(board)
    return Counter(
        (world_point(pad.local_pos, placement),)
        for placement in board.placements
        for pad in placement.footprint.pads
        if copper_layers_of(placement, pad, board.stackup)
        and _pad_edge_gap(placement, pad, exempt, board) is not None
    )


def brute_force_pairs(board):
    """Every violating pad pair by exhaustive pricing — no pre-filter, no
    cache: the reference the pair pass must reproduce exactly."""
    from net2board.geometry import (
        capsule_gap_nm,
        capsules_overlap,
        pad_world_outline,
        world_point,
    )

    nets = {pin: net.name for net in board.nets for pin in net.pins}
    pads = [
        (placement, pad, copper_layers_of(placement, pad, board.stackup))
        for placement in board.placements
        for pad in placement.footprint.pads
    ]
    found = Counter()
    for i, (pa_place, pa, la) in enumerate(pads):
        for pb_place, pb, lb in pads[i + 1 :]:
            if not la or not lb or not (la & lb):
                continue
            na = nets.get((pa_place.ref, pa.number))
            if na is not None and na == nets.get((pb_place.ref, pb.number)):
                continue
            if pa_place is pb_place and any(
                pa.number in g and pb.number in g
                for g in pa_place.footprint.net_tie_groups
            ):
                continue
            ca, cb = pad_world_outline(pa_place, pa), pad_world_outline(pb_place, pb)
            if capsules_overlap(ca, cb):
                kind = ViolationType.SHORTING_ITEMS
            elif capsule_gap_nm(ca, cb) < board.design_rules.clearance_nm:
                kind = ViolationType.CLEARANCE
            else:
                continue
            refs = tuple(
                sorted((f"placement:{pa_place.ref}", f"placement:{pb_place.ref}"))
            )
            centers = frozenset(
                {
                    world_point(pa.local_pos, pa_place),
                    world_point(pb.local_pos, pb_place),
                }
            )
            found[(kind, refs, centers)] += 1
    return found


class TestPadAt:
    """``_pad_at`` recovers pad identity from the placement plus the
    location — the first pad at that world centre wins, the recorded
    convention (ADR-0022's no-pad-ref-kind rule), and the cached lookup
    table keeps both: first-match survives the cache, and each
    (footprint, turn, side) key resolves its own table."""

    def test_duplicate_pad_locations_resolve_the_first_pad(self):
        pads = footprint(rect_pad("1"), rect_pad("2"), rect_pad("3", x=MM))
        placement = board_of(("A", pads, 5 * MM, 5 * MM, 0, "F.Cu")).placement("A")
        assert _pad_at(placement, Point(5 * MM, 5 * MM)).number == "1"
        # the second call reads the cached table — the same first match
        assert _pad_at(placement, Point(5 * MM, 5 * MM)).number == "1"
        assert _pad_at(placement, Point(6 * MM, 5 * MM)).number == "3"

    def test_the_table_resolves_each_turn_separately(self):
        pads = footprint(rect_pad("1"), rect_pad("2", x=MM))
        board = board_of(
            ("A", pads, 5 * MM, 5 * MM, 0, "F.Cu"),
            ("B", pads, 5 * MM, 5 * MM, 90, "F.Cu"),
        )
        front, turned = board.placement("A"), board.placement("B")
        assert _pad_at(front, world_point(Point(MM, 0), front)).number == "2"
        assert _pad_at(turned, world_point(Point(MM, 0), turned)).number == "2"
        # the 90° entry never poisons the 0° one — same footprint object
        assert _pad_at(front, Point(5 * MM, 5 * MM)).number == "1"


class TestThePreFilterHidesNothing:
    """The pre-filters only ever skip provably clean copper: on seeded
    dense boards — every pad shape, every rotation, both sides,
    through-hole and inner-layer copper, parts reaching the edge lines —
    the pair pass and the edge check match exhaustive pricing finding for
    finding (#98)."""

    def test_seeded_boards_match_exhaustive_pricing(self):
        import random

        from net2board.drc import _pad_pair_records

        edge_findings = 0
        rng = random.Random(98)
        shapes = ("rect", "circle", "oval", "roundrect")
        layer_sets = (("F.Cu",), ("B.Cu",), ("*.Cu",), ("In1.Cu",), ("F.Mask",))
        for _ in range(40):
            placed = []
            for k in range(6):
                pads = tuple(
                    Pad(
                        number=str(n),
                        local_pos=Point(
                            rng.randint(-2, 2) * MM, rng.randint(-2, 2) * MM
                        ),
                        size=(
                            rng.randint(3, 15) * 100_000,
                            rng.randint(3, 15) * 100_000,
                        ),
                        drill=None,
                        layers=rng.choice(layer_sets),
                        shape_enum=rng.choice(shapes),
                        local_rotation=rng.choice((0, 90, 180, 270)),
                    )
                    for n in range(rng.randint(1, 4))
                )
                placed.append(
                    (
                        f"P{k}",
                        footprint(*pads),
                        rng.randint(0, 120) * 100_000,
                        rng.randint(0, 120) * 100_000,
                        rng.choice((0, 90, 180, 270)),
                        rng.choice(("F.Cu", "B.Cu")),
                    )
                )
            board = board_of(*placed, stackup=("F.Cu", "In1.Cu", "B.Cu"))
            got = Counter(
                (
                    record.violation.type,
                    record.refs,
                    frozenset(record.violation.locations),
                )
                for record in _pad_pair_records(board)
            )
            assert got == brute_force_pairs(board)
            edges = Counter(
                violation.locations
                for violation in check_named("_copper_edge_clearance")(board)
            )
            assert edges == brute_force_edges(board)
            edge_findings += sum(edges.values())
        assert edge_findings, "the seeded boards must reach the edge lines"
