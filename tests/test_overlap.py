"""S5 — strict-interior overlap truth table, exact-integer (ADR-0006).

Touching edges and tangent points are NOT overlaps — the boundary cases are
the point of this table. Every case is decided in integer arithmetic: the
``radius_sq`` 10/40 pair is chosen so external tangency sits at d² = 90
exactly, where float ``sqrt`` arithmetic misjudges the boundary.
"""

from net2board.geometry import Circle, Point, Rectangle, shapes_overlap


def rect(x0, y0, x1, y1):
    return Rectangle(min=Point(x0, y0), max=Point(x1, y1))


def circle(cx, cy, radius_sq):
    # `end` carries the defining radius vector for export (ADR-0010); the
    # overlap predicates never read it, so it is inert here.
    return Circle(center=Point(cx, cy), radius_sq=radius_sq, end=Point(cx, cy))


class TestRectRect:
    def test_clear_overlap(self):
        assert shapes_overlap(rect(0, 0, 10, 10), rect(5, 5, 15, 15))

    def test_touching_edge_is_not_overlap(self):
        assert not shapes_overlap(rect(0, 0, 10, 10), rect(10, 0, 20, 10))

    def test_touching_corner_is_not_overlap(self):
        assert not shapes_overlap(rect(0, 0, 10, 10), rect(10, 10, 20, 20))

    def test_disjoint(self):
        assert not shapes_overlap(rect(0, 0, 10, 10), rect(11, 0, 20, 10))

    def test_containment_overlaps(self):
        assert shapes_overlap(rect(0, 0, 10, 10), rect(2, 2, 3, 3))

    def test_identical_overlaps(self):
        assert shapes_overlap(rect(0, 0, 10, 10), rect(0, 0, 10, 10))


class TestCircleCircle:
    # circles of radius_sq 9 (r=3) and 16 (r=4)

    def test_clear_overlap(self):
        assert shapes_overlap(circle(0, 0, 9), circle(5, 0, 16))  # d²=25 < 49

    def test_external_tangent_is_not_overlap(self):
        assert not shapes_overlap(circle(0, 0, 9), circle(7, 0, 16))  # d²=49

    def test_disjoint(self):
        assert not shapes_overlap(circle(0, 0, 9), circle(8, 0, 16))  # d²=64

    def test_containment_overlaps(self):
        assert shapes_overlap(circle(0, 0, 16), circle(1, 0, 9))

    def test_internal_tangent_overlaps(self):
        # small disk strictly inside the big one, touching at one boundary
        # point — interiors still intersect: d = 5 - 3, d² = 4
        assert shapes_overlap(circle(0, 0, 25), circle(2, 0, 9))

    def test_concentric_overlaps(self):
        assert shapes_overlap(circle(0, 0, 16), circle(0, 0, 9))


class TestCircleRect:
    def test_clear_overlap(self):
        assert shapes_overlap(circle(0, 0, 9), rect(1, 1, 4, 4))  # d²=2 < 9

    def test_edge_tangent_is_not_overlap(self):
        assert not shapes_overlap(circle(0, 0, 9), rect(3, -1, 8, 1))  # d²=9

    def test_corner_tangent_is_not_overlap(self):
        assert not shapes_overlap(circle(0, 0, 18), rect(3, 3, 8, 8))  # d²=18

    def test_disjoint(self):
        assert not shapes_overlap(circle(0, 0, 9), rect(4, -1, 8, 1))  # d²=16

    def test_circle_inside_rect_overlaps(self):
        assert shapes_overlap(circle(0, 0, 9), rect(-10, -10, 10, 10))

    def test_rect_inside_circle_overlaps(self):
        assert shapes_overlap(circle(0, 0, 9), rect(-1, -1, 1, 1))

    def test_argument_order_does_not_matter(self):
        assert shapes_overlap(rect(1, 1, 4, 4), circle(0, 0, 9))
        assert not shapes_overlap(rect(3, -1, 8, 1), circle(0, 0, 9))


class TestExactIntegerBoundaries:
    # radius_sq 10 and 40: radii √10 and √40, sum² = 50 + 2·√400 = 90 exact

    def test_external_tangent_exact(self):
        assert not shapes_overlap(circle(0, 0, 10), circle(9, 3, 40))  # d²=90

    def test_just_inside_tangent_overlaps(self):
        assert shapes_overlap(circle(0, 0, 10), circle(9, 2, 40))  # d²=85

    def test_internal_tangent_exact(self):
        # (√40 - √10)² = 50 - 2·√400 = 10 exact — interiors still intersect
        assert shapes_overlap(circle(0, 0, 40), circle(1, 3, 10))  # d²=10
