import pytest

from app.packer import UnfitItemError, build_plan
from tests.check_plan import check_plan, iter_leaves


def plan(stock_w, stock_h, kerf, items):
    return build_plan(stock_w, stock_h, kerf, items)


class TestBasicPacking:
    def test_exact_fit_single_piece(self):
        result = plan(100, 100, 3, [("A", 100, 100, 1)])
        assert result["summary"]["sheet_count"] == 1
        sheet = result["sheets"][0]
        assert sheet["utilization"] == 1.0
        assert sheet["placements"] == [
            {"item": "A", "x": 0, "y": 0, "width": 100, "height": 100,
             "rotated": False}
        ]
        assert sheet["cut_tree"]["type"] == "item"

    def test_kerf_is_consumed_between_pieces(self):
        # Two 45-wide pieces on a 100-wide sheet with kerf 5:
        # the second must start at 45 + 5 = 50, not 45.
        result = plan(100, 100, 5, [("A", 45, 100, 2)])
        sheet = result["sheets"][0]
        assert result["summary"]["sheet_count"] == 1
        xs = sorted(p["x"] for p in sheet["placements"])
        assert xs == [0, 50]
        root = sheet["cut_tree"]
        assert root["type"] == "cut" and root["direction"] == "vertical"
        first, second = root["children"]
        assert second["x"] == first["x"] + first["width"] + 5

    def test_kerf_can_force_an_extra_sheet(self):
        # 3 pieces of 35 need 35*3 + 2*4 = 113 > 100 -> two sheets.
        result = plan(100, 100, 4, [("A", 35, 100, 3)])
        assert result["summary"]["sheet_count"] == 2
        check_plan(result)

    def test_zero_kerf_packs_tightly(self):
        result = plan(100, 100, 0, [("A", 50, 100, 2)])
        assert result["summary"]["sheet_count"] == 1
        assert result["sheets"][0]["utilization"] == 1.0

    def test_rotation_when_unrotated_does_not_fit(self):
        result = plan(50, 100, 0, [("A", 60, 40, 1)])
        placement = result["sheets"][0]["placements"][0]
        assert placement["rotated"] is True
        assert placement["width"] == 40
        assert placement["height"] == 60

    def test_unrotated_preferred_when_both_fit(self):
        result = plan(100, 100, 0, [("A", 60, 40, 1)])
        placement = result["sheets"][0]["placements"][0]
        assert placement["rotated"] is False
        assert placement["width"] == 60

    def test_multiple_sheets(self):
        # 8 pieces of 50x50 pack exactly 4 per 100x100 sheet.
        result = plan(100, 100, 0, [("A", 50, 50, 8)])
        assert result["summary"]["sheet_count"] == 2
        assert result["summary"]["utilization"] == 1.0
        check_plan(result)

    def test_multiple_sheets_and_items(self):
        result = plan(100, 100, 0, [("A", 60, 60, 3), ("B", 40, 40, 4)])
        assert result["summary"]["sheet_count"] >= 2
        assert result["summary"]["piece_count"] == 7
        check_plan(result)


class TestUnfitItems:
    def test_item_larger_than_stock_raises(self):
        with pytest.raises(UnfitItemError) as excinfo:
            plan(100, 100, 0, [("big", 101, 50, 1)])
        assert "big" in str(excinfo.value)

    def test_item_unfit_in_both_orientations_raises(self):
        with pytest.raises(UnfitItemError):
            plan(100, 60, 0, [("awkward", 120, 70, 1)])

    def test_fit_check_ignores_kerf_on_fresh_sheet(self):
        # A piece matching the stock exactly must fit even with a large kerf:
        # no cut is needed at the sheet edges.
        result = plan(100, 100, 10, [("A", 100, 100, 1)])
        assert result["summary"]["sheet_count"] == 1


class TestTreeConsistency:
    def test_mixed_batch_tree_is_consistent(self):
        result = plan(
            900, 600, 3,
            [("flyer", 210, 148, 8), ("card", 90, 55, 20), ("tag", 120, 80, 5)],
        )
        check_plan(result)

    def test_waste_leaves_have_positive_size(self):
        result = plan(100, 100, 5, [("A", 30, 30, 3)])
        check_plan(result)
        for sheet in result["sheets"]:
            for leaf in iter_leaves(sheet["cut_tree"]):
                if leaf["type"] == "waste":
                    assert leaf["width"] > 0
                    assert leaf["height"] > 0

    def test_trim_sliver_narrower_than_kerf_is_allowed(self):
        # 300-wide piece on 302-wide stock with kerf 3: the 2mm sliver
        # cannot host anything (the blade would eat it) -> trim loss,
        # the piece must still be placed.
        result = plan(302, 100, 3, [("A", 300, 100, 1)])
        assert result["summary"]["sheet_count"] == 1
        check_plan(result)

    def test_decimal_dimensions(self):
        result = plan(297.5, 210.0, 1.5, [("A", 99.1, 70.3, 6)])
        check_plan(result)

    def test_utilization_never_exceeds_one(self):
        result = plan(200, 100, 2, [("A", 45, 45, 30)])
        check_plan(result)
        for sheet in result["sheets"]:
            assert 0 < sheet["utilization"] <= 1.0
