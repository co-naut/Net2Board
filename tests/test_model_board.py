"""S4/S5 — Board queries and snapshot equality: branch, backtrack, diff exactly (ADR-0001, ADR-0002)."""

import pytest

from conftest import fresh_board

X = Y = 10_000_000


class TestUnplacedRefs:
    def test_fresh_board_lists_every_netlist_ref(self):
        assert fresh_board().unplaced_refs() == ("R1", "R2", "R3")

    def test_shrinks_as_refs_are_placed(self):
        board = fresh_board().with_placement("R2", X, Y, 0, "F.Cu")
        assert board.unplaced_refs() == ("R1", "R3")

    def test_all_placed_is_empty(self):
        board = (
            fresh_board()
            .with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X, Y, 0, "F.Cu")
            .with_placement("R3", X, Y, 0, "F.Cu")
        )
        assert board.unplaced_refs() == ()


class TestUnroutedNetNames:
    def test_fresh_board_lists_every_net(self):
        assert fresh_board().unrouted_net_names() == ("GND", "N1")

    def test_placing_changes_nothing_nothing_is_ever_routed(self):
        board = fresh_board().with_placement("R1", X, Y, 0, "F.Cu")
        assert board.unrouted_net_names() == ("GND", "N1")


class TestSnapshotEquality:
    def test_replayed_sequences_compare_equal(self):
        def replay(board):
            return (
                board.with_placement("R1", X, Y, 90, "F.Cu")
                .with_placement("R2", 25_000_000, Y, 0, "B.Cu")
                .with_placement("R1", X, 2_000_000, 180, "B.Cu")
            )

        assert replay(fresh_board()) == replay(fresh_board())

    def test_boards_from_separate_builds_compare_equal(self):
        assert fresh_board() == fresh_board()

    def test_divergent_branches_differ(self):
        board = fresh_board().with_placement("R1", X, Y, 0, "F.Cu")
        left = board.with_placement("R2", 5_000_000, 5_000_000, 0, "F.Cu")
        right = board.with_placement("R2", 6_000_000, 5_000_000, 0, "F.Cu")
        assert left != right

    def test_snapshots_hash_consistently(self):
        def replay(board):
            return board.with_placement("R1", X, Y, 90, "F.Cu").with_placement(
                "R3", 0, 0, 270, "B.Cu"
            )

        assert hash(replay(fresh_board())) == hash(replay(fresh_board()))

    def test_backtrack_returns_to_the_earlier_snapshot_exactly(self):
        base = fresh_board()
        placed = base.with_placement("R1", X, Y, 0, "F.Cu")
        moved = placed.with_placement("R1", 5_000_000, 5_000_000, 90, "B.Cu")
        assert placed != moved
        assert moved.with_placement("R1", X, Y, 0, "F.Cu") == placed
        assert base.with_placement("R1", X, Y, 0, "F.Cu") == placed

    def test_board_is_frozen(self):
        with pytest.raises(Exception, match="assign"):
            fresh_board().placements = ()

    def test_net_is_frozen(self):
        board = fresh_board()
        with pytest.raises(Exception, match="assign"):
            board.nets[0].name = "OTHER"
