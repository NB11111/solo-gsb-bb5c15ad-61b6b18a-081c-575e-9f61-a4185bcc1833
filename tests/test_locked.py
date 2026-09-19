"""Tests for locked (pre-positioned) placements."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.packer import LockedPlacementError, build_plan
from tests.check_plan import check_plan, iter_leaves

client = TestClient(app)


class TestLockedPacking:
    def test_locked_piece_stays_at_exact_position(self):
        result = build_plan(100, 100, 3, [("A", 40, 100, 2)],
                            locked=[("A", 0, 0, False)])
        check_plan(result)
        sheet = result["sheets"][0]
        locked = [p for p in sheet["placements"] if p["locked"]]
        assert locked == [
            {"item": "A", "x": 0, "y": 0, "width": 40, "height": 100,
             "rotated": False, "locked": True}
        ]
        # The remaining piece goes into the offcut leaf right of the kerf.
        xs = sorted(p["x"] for p in sheet["placements"])
        assert xs == [0, 43]

    def test_locked_counts_toward_demand(self):
        result = build_plan(100, 100, 0, [("A", 50, 100, 2)],
                            locked=[("A", 0, 0, False)])
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1
        assert result["summary"]["piece_count"] == 2
        placed = result["sheets"][0]["placements"]
        assert sum(p["locked"] for p in placed) == 1
        assert sorted(p["x"] for p in placed) == [0, 50]

    def test_locked_rotation(self):
        result = build_plan(100, 100, 3, [("A", 40, 100, 1)],
                            locked=[("A", 0, 0, True)])
        check_plan(result)
        (placement,) = result["sheets"][0]["placements"]
        assert placement["rotated"] is True
        assert (placement["width"], placement["height"]) == (100, 40)

    def test_remaining_pieces_only_use_offcut_leaves(self):
        # Locked A occupies the left half; the second A must fit into the
        # right offcut leaf of the same sheet.
        result = build_plan(100, 100, 0, [("A", 50, 100, 2)],
                            locked=[("A", 0, 0, False)])
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1
        free_xs = {p["x"] for p in result["sheets"][0]["placements"]}
        assert free_xs == {0, 50}

    def test_overflow_opens_new_sheet(self):
        result = build_plan(100, 100, 0, [("A", 50, 100, 3)],
                            locked=[("A", 0, 0, False)])
        check_plan(result)
        assert result["summary"]["sheet_count"] == 2
        assert result["sheets"][0]["placements"][0]["locked"] is True
        assert all(not p["locked"] for p in result["sheets"][1]["placements"])

    def test_summary_covers_locked_and_placed(self):
        result = build_plan(
            200, 100, 0,
            [("A", 50, 100, 2), ("B", 100, 100, 1)],
            locked=[("A", 0, 0, False), ("B", 100, 0, False)],
        )
        check_plan(result)
        summary = result["summary"]
        assert summary["sheet_count"] == 1
        assert summary["piece_count"] == 3
        assert summary["used_area"] == 2 * 50 * 100 + 100 * 100
        assert summary["utilization"] == 1.0

    def test_all_pieces_locked(self):
        result = build_plan(100, 100, 0, [("A", 50, 100, 2)],
                            locked=[("A", 0, 0, False), ("A", 50, 0, False)])
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1
        assert all(p["locked"] for p in result["sheets"][0]["placements"])
        assert result["sheets"][0]["utilization"] == 1.0

    def test_locked_tree_item_leaves_match_locked_positions(self):
        result = build_plan(100, 100, 3, [("A", 40, 100, 1)],
                            locked=[("A", 0, 0, False)])
        items = [leaf for leaf in iter_leaves(result["sheets"][0]["cut_tree"])
                 if leaf["type"] == "item"]
        assert items == [
            {"type": "item", "item": "A", "x": 0, "y": 0,
             "width": 40, "height": 100, "rotated": False}
        ]

    def test_trim_sliver_at_far_edge_allowed(self):
        # Piece 2mm short of the right edge with kerf 3: the sliver cannot
        # host anything and is trim loss; the layout is valid.
        result = build_plan(102, 100, 3, [("A", 100, 100, 1)],
                            locked=[("A", 0, 0, False)])
        check_plan(result)
        assert result["sheets"][0]["cut_tree"]["type"] == "item"


class TestLockedValidation:
    def test_unknown_item_rejected(self):
        with pytest.raises(LockedPlacementError, match="zzz"):
            build_plan(100, 100, 0, [("A", 50, 50, 1)],
                       locked=[("zzz", 0, 0, False)])

    def test_out_of_bounds_rejected(self):
        with pytest.raises(LockedPlacementError, match=r"#1.*exceeds stock"):
            build_plan(100, 100, 0, [("A", 50, 50, 1)],
                       locked=[("A", 80, 0, False)])

    def test_overlap_rejected(self):
        with pytest.raises(LockedPlacementError, match="overlap"):
            build_plan(100, 100, 0, [("A", 50, 50, 2)],
                       locked=[("A", 0, 0, False), ("A", 25, 0, False)])

    def test_locked_count_cannot_exceed_quantity(self):
        with pytest.raises(LockedPlacementError, match="quantity"):
            build_plan(100, 100, 0, [("A", 50, 100, 1)],
                       locked=[("A", 0, 0, False), ("A", 50, 0, False)])

    def test_gap_narrower_than_kerf_rejected(self):
        # The 1mm gap between the locked pieces cannot host the 3mm blade:
        # the layout is not guillotine-decomposable.
        with pytest.raises(LockedPlacementError) as excinfo:
            build_plan(100, 100, 3, [("A", 40, 100, 2)],
                       locked=[("A", 0, 0, False), ("A", 41, 0, False)])
        message = str(excinfo.value)
        assert "guillotine" in message
        assert "#1" in message and "#2" in message

    def test_touching_pieces_with_nonzero_kerf_rejected(self):
        # Flush neighbours leave no room for the blade between them.
        with pytest.raises(LockedPlacementError):
            build_plan(100, 100, 3, [("A", 40, 100, 2)],
                       locked=[("A", 0, 0, False), ("A", 40, 0, False)])

    def test_pinwheel_layout_rejected(self):
        # Classic non-guillotine arrangement: no straight cut spans the
        # sheet without crossing a piece.
        items = [("A", 60, 30, 1), ("B", 40, 60, 1),
                 ("C", 60, 40, 1), ("D", 40, 70, 1)]
        locked = [("A", 0, 0, False), ("B", 60, 0, False),
                  ("C", 40, 60, False), ("D", 0, 30, False)]
        with pytest.raises(LockedPlacementError) as excinfo:
            build_plan(100, 100, 0, items, locked=locked)
        message = str(excinfo.value)
        assert "guillotine" in message
        for position in ("#1", "#2", "#3", "#4"):
            assert position in message

    def test_sub_kerf_sliver_at_sheet_edge_rejected(self):
        # Piece 2mm off the left edge with kerf 3: the sliver can neither
        # be cut off nor host the blade.
        with pytest.raises(LockedPlacementError):
            build_plan(100, 100, 3, [("A", 50, 100, 1)],
                       locked=[("A", 2, 0, False)])

    def test_rotated_locked_piece_out_of_bounds_rejected(self):
        with pytest.raises(LockedPlacementError, match="exceeds stock"):
            build_plan(100, 60, 0, [("A", 80, 50, 1)],
                       locked=[("A", 0, 0, True)])


class TestLockedApi:
    def test_locked_placements_happy_path(self):
        payload = {
            "stock": {"width": 900, "height": 600},
            "kerf": 3,
            "items": [
                {"id": "flyer", "width": 210, "height": 148, "quantity": 4},
                {"id": "card", "width": 90, "height": 55, "quantity": 6},
            ],
            "locked_placements": [
                {"item": "flyer", "x": 0, "y": 0, "rotated": False},
                {"item": "card", "x": 213, "y": 0, "rotated": True},
            ],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 200
        body = response.json()
        check_plan(body)
        first_two = body["sheets"][0]["placements"][:2]
        assert first_two[0] == {"item": "flyer", "x": 0, "y": 0,
                                "width": 210, "height": 148,
                                "rotated": False, "locked": True}
        assert first_two[1] == {"item": "card", "x": 213, "y": 0,
                                "width": 55, "height": 90,
                                "rotated": True, "locked": True}
        locked_total = sum(p["locked"] for s in body["sheets"]
                           for p in s["placements"])
        assert locked_total == 2
        assert body["summary"]["piece_count"] == 10

    def test_locked_with_auto_generated_ids(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"width": 50, "height": 100, "quantity": 2}],
            "locked_placements": [{"item": "item-1", "x": 0, "y": 0}],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 200
        body = response.json()
        check_plan(body)
        assert body["sheets"][0]["placements"][0]["locked"] is True

    def test_locked_overlap_returns_422(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 50, "height": 50, "quantity": 2}],
            "locked_placements": [
                {"item": "A", "x": 0, "y": 0},
                {"item": "A", "x": 10, "y": 10},
            ],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 422
        assert "overlap" in response.json()["detail"]

    def test_locked_out_of_bounds_returns_422(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 50, "height": 50, "quantity": 1}],
            "locked_placements": [{"item": "A", "x": 80, "y": 0}],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 422
        assert "'A'" in response.json()["detail"]

    def test_locked_unknown_item_returns_422(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 50, "height": 50, "quantity": 1}],
            "locked_placements": [{"item": "zzz", "x": 0, "y": 0}],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 422
        assert "zzz" in response.json()["detail"]

    def test_locked_count_exceeds_quantity_returns_422(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 50, "height": 100, "quantity": 1}],
            "locked_placements": [
                {"item": "A", "x": 0, "y": 0},
                {"item": "A", "x": 50, "y": 0},
            ],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 422
        assert "quantity" in response.json()["detail"]

    def test_non_decomposable_returns_422_with_conflicts(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "kerf": 3,
            "items": [{"id": "A", "width": 40, "height": 100, "quantity": 2}],
            "locked_placements": [
                {"item": "A", "x": 0, "y": 0},
                {"item": "A", "x": 41, "y": 0},
            ],
        }
        response = client.post("/plans", json=payload)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "guillotine" in detail
        assert "'A'" in detail and "#1" in detail and "#2" in detail

    def test_too_many_locked_placements_rejected(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 1, "height": 1, "quantity": 1}],
            "locked_placements": [{"item": "A", "x": 0, "y": 0}] * 201,
        }
        assert client.post("/plans", json=payload).status_code == 422

    def test_negative_locked_coordinate_rejected(self):
        payload = {
            "stock": {"width": 100, "height": 100},
            "items": [{"id": "A", "width": 50, "height": 50, "quantity": 1}],
            "locked_placements": [{"item": "A", "x": -1, "y": 0}],
        }
        assert client.post("/plans", json=payload).status_code == 422
