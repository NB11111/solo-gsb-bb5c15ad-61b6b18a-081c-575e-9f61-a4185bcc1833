from fastapi.testclient import TestClient

from app.main import app
from tests.check_plan import check_plan

client = TestClient(app)

VALID_PAYLOAD = {
    "stock": {"width": 900, "height": 600},
    "kerf": 3,
    "items": [
        {"id": "flyer", "width": 210, "height": 148, "quantity": 8},
        {"id": "card", "width": 90, "height": 55, "quantity": 20},
    ],
}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_plan_happy_path():
    response = client.post("/plans", json=VALID_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["stock"] == {"width": 900.0, "height": 600.0}
    assert body["kerf"] == 3.0
    assert body["summary"]["piece_count"] == 28
    assert body["summary"]["sheet_count"] == len(body["sheets"])
    check_plan(body)


def test_create_plan_auto_generates_item_ids():
    payload = {
        "stock": {"width": 100, "height": 100},
        "items": [{"width": 50, "height": 50, "quantity": 2}],
    }
    response = client.post("/plans", json=payload)
    assert response.status_code == 200
    items = {p["item"] for s in response.json()["sheets"] for p in s["placements"]}
    assert items == {"item-1"}


def test_unfit_item_returns_clear_error():
    payload = {
        "stock": {"width": 100, "height": 100},
        "kerf": 2,
        "items": [{"id": "poster", "width": 150, "height": 80, "quantity": 1}],
    }
    response = client.post("/plans", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "poster" in detail
    assert "150" in detail and "100" in detail


def test_invalid_stock_dimensions_rejected():
    payload = {"stock": {"width": -10, "height": 100},
               "items": [{"width": 10, "height": 10, "quantity": 1}]}
    assert client.post("/plans", json=payload).status_code == 422

    payload["stock"] = {"width": 100, "height": 0}
    assert client.post("/plans", json=payload).status_code == 422


def test_invalid_item_dimensions_rejected():
    payload = {"stock": {"width": 100, "height": 100},
               "items": [{"width": 0, "height": 10, "quantity": 1}]}
    assert client.post("/plans", json=payload).status_code == 422


def test_invalid_quantity_rejected():
    payload = {"stock": {"width": 100, "height": 100},
               "items": [{"width": 10, "height": 10, "quantity": 0}]}
    assert client.post("/plans", json=payload).status_code == 422


def test_negative_kerf_rejected():
    payload = {"stock": {"width": 100, "height": 100}, "kerf": -1,
               "items": [{"width": 10, "height": 10, "quantity": 1}]}
    assert client.post("/plans", json=payload).status_code == 422


def test_empty_item_list_rejected():
    payload = {"stock": {"width": 100, "height": 100}, "items": []}
    assert client.post("/plans", json=payload).status_code == 422


def test_duplicate_item_ids_rejected():
    payload = {
        "stock": {"width": 100, "height": 100},
        "items": [
            {"id": "A", "width": 10, "height": 10, "quantity": 1},
            {"id": "A", "width": 20, "height": 20, "quantity": 1},
        ],
    }
    assert client.post("/plans", json=payload).status_code == 422


def test_missing_fields_rejected():
    assert client.post("/plans", json={}).status_code == 422
    assert client.post("/plans", json={"stock": {"width": 100}}).status_code == 422
