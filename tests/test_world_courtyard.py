"""S5 — world_courtyard: the local → world transform (ADR-0005, ADR-0006).

The transform composes, innermost first: mirror local x on the back side
(KiCad's left-right flip — the writer owns the mirror; the oracle applies
translate + rotate only, probed on kicad-cli 10.0.6), rotate 90°-increment
CCW-as-displayed in the y-down board frame, translate to the anchor.
``None`` courtyard stays ``None`` — the overlap check skips it.
"""

import pytest

from conftest import fresh_board
from net2board.geometry import world_courtyard

# RES courtyard (local, nm): x -1_050_000..8_670_000, y -1_500_000..1_500_000.
# BIGRES courtyard (local, nm): center (5_000_000, 0), radius_sq 10e12,
# end (8_000_000, 1_000_000).


def placed(ref, x, y, rotation, side):
    board = fresh_board().with_placement(ref, x, y, rotation, side)
    return next(p for p in board.placements if p.ref == ref)


class TestRectangle:
    def test_front_rot_0_translates(self):
        yard = world_courtyard(placed("R1", 10_000_000, 20_000_000, 0, "F.Cu"))
        assert (yard.min.x, yard.max.x) == (8_950_000, 18_670_000)
        assert (yard.min.y, yard.max.y) == (18_500_000, 21_500_000)

    def test_front_rot_90_swaps_extents(self):
        yard = world_courtyard(placed("R1", 0, 0, 90, "F.Cu"))
        assert (yard.min.x, yard.max.x) == (-1_500_000, 1_500_000)
        assert (yard.min.y, yard.max.y) == (-8_670_000, 1_050_000)

    def test_front_rot_180(self):
        yard = world_courtyard(placed("R1", 0, 0, 180, "F.Cu"))
        assert (yard.min.x, yard.max.x) == (-8_670_000, 1_050_000)
        assert (yard.min.y, yard.max.y) == (-1_500_000, 1_500_000)

    def test_front_rot_270(self):
        yard = world_courtyard(placed("R1", 0, 0, 270, "F.Cu"))
        assert (yard.min.x, yard.max.x) == (-1_500_000, 1_500_000)
        assert (yard.min.y, yard.max.y) == (-1_050_000, 8_670_000)

    def test_back_rot_0_mirrors_local_x(self):
        yard = world_courtyard(placed("R1", 0, 0, 0, "B.Cu"))
        assert (yard.min.x, yard.max.x) == (-8_670_000, 1_050_000)
        assert (yard.min.y, yard.max.y) == (-1_500_000, 1_500_000)

    def test_back_rot_90_mirrors_then_rotates(self):
        yard = world_courtyard(placed("R1", 0, 0, 90, "B.Cu"))
        # mirror-then-rotate; rotate-then-mirror would give y -8_670_000..1_050_000
        assert (yard.min.x, yard.max.x) == (-1_500_000, 1_500_000)
        assert (yard.min.y, yard.max.y) == (-1_050_000, 8_670_000)


class TestCircle:
    def test_front_rot_0_translates_center_and_end(self):
        yard = world_courtyard(placed("R3", 10_000_000, 10_000_000, 0, "F.Cu"))
        assert (yard.center.x, yard.center.y) == (15_000_000, 10_000_000)
        assert (yard.end.x, yard.end.y) == (18_000_000, 11_000_000)
        assert yard.radius_sq == 10_000_000_000_000

    def test_front_rot_90_rotates_center_and_end(self):
        yard = world_courtyard(placed("R3", 0, 0, 90, "F.Cu"))
        assert (yard.center.x, yard.center.y) == (0, -5_000_000)
        assert (yard.end.x, yard.end.y) == (1_000_000, -8_000_000)

    def test_back_rot_0_mirrors_center_and_end(self):
        yard = world_courtyard(placed("R3", 0, 0, 0, "B.Cu"))
        assert (yard.center.x, yard.center.y) == (-5_000_000, 0)
        assert (yard.end.x, yard.end.y) == (-8_000_000, 1_000_000)

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    @pytest.mark.parametrize("side", ["F.Cu", "B.Cu"])
    def test_end_stays_on_the_circle_under_every_transform(self, rotation, side):
        yard = world_courtyard(placed("R3", 7_000_000, -3_000_000, rotation, side))
        dx = yard.end.x - yard.center.x
        dy = yard.end.y - yard.center.y
        assert dx * dx + dy * dy == yard.radius_sq
