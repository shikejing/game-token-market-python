"""HTTP API integration tests via FastAPI TestClient."""
import pytest
from fastapi.testclient import TestClient

from market.api import set_engine
from market.engine import MarketEngine
import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app lifespan at a fresh temp data dir so each test gets a
    # clean engine (the lifespan builds and installs the engine itself).
    monkeypatch.setenv("MARKET_DATA_DIR", str(tmp_path / "data"))
    with TestClient(main.app) as c:
        yield c


def test_root_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_open_and_get_account(client):
    r = client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 1000})
    assert r.status_code == 200
    assert r.json()["balance"] == 1000
    r = client.get("/api/accounts/alice")
    assert r.status_code == 200
    assert r.json()["balance"] == 1000
    r = client.get("/api/accounts/nobody")
    assert r.status_code == 404


def test_duplicate_account(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 1000})
    r = client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 1000})
    assert r.status_code == 409


def test_deposit(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 1000})
    r = client.post("/api/accounts/alice/deposit", json={"amount": 500})
    assert r.status_code == 200
    assert r.json()["balance"] == 1500


def test_submit_and_match_order(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 10000})
    client.post("/api/accounts", json={"account_id": "bob", "initial_balance": 10000})
    r1 = client.post("/api/orders", json={
        "account_id": "alice", "side": "sell", "price": 100, "quantity": 5,
    })
    assert r1.status_code == 200
    assert r1.json()["order"]["status"] == "open"
    r2 = client.post("/api/orders", json={
        "account_id": "bob", "side": "buy", "price": 100, "quantity": 5,
    })
    assert r2.status_code == 200
    assert r2.json()["order"]["status"] == "filled"
    assert len(r2.json()["trades"]) == 1
    # Depth is empty after full fill.
    depth = client.get("/api/depth").json()
    assert depth == {"bids": [], "asks": []}


def test_idempotency_via_header(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 10000})
    headers = {"Idempotency-Key": "req-1"}
    r1 = client.post("/api/orders", json={
        "account_id": "alice", "side": "buy", "price": 100, "quantity": 5,
    }, headers=headers)
    r2 = client.post("/api/orders", json={
        "account_id": "alice", "side": "buy", "price": 100, "quantity": 5,
    }, headers=headers)
    assert r1.json()["order"]["order_id"] == r2.json()["order"]["order_id"]
    # Only one order exists.
    orders = client.get("/api/orders?account_id=alice").json()
    assert len(orders["items"]) == 1


def test_cancel_order(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 10000})
    r = client.post("/api/orders", json={
        "account_id": "alice", "side": "sell", "price": 100, "quantity": 5,
    })
    order_id = r.json()["order"]["order_id"]
    r2 = client.post(f"/api/orders/{order_id}/cancel", json={"account_id": "alice"})
    assert r2.status_code == 200
    assert r2.json()["order"]["status"] == "cancelled"
    # Freeze released.
    acct = client.get("/api/accounts/alice").json()
    assert acct["frozen"] == 0
    assert acct["balance"] == 10000


def test_cancel_forbidden_for_other_account(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 10000})
    client.post("/api/accounts", json={"account_id": "bob", "initial_balance": 10000})
    r = client.post("/api/orders", json={
        "account_id": "alice", "side": "sell", "price": 100, "quantity": 5,
    })
    order_id = r.json()["order"]["order_id"]
    r2 = client.post(f"/api/orders/{order_id}/cancel", json={"account_id": "bob"})
    assert r2.status_code == 403


def test_insufficient_balance(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 100})
    r = client.post("/api/orders", json={
        "account_id": "alice", "side": "buy", "price": 100, "quantity": 5,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "insufficient_balance"


def test_trades_and_orders_pagination(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 100000})
    client.post("/api/accounts", json={"account_id": "bob", "initial_balance": 100000})
    for i in range(6):
        client.post("/api/orders", json={
            "account_id": "alice", "side": "sell", "price": 100, "quantity": 1,
        })
        client.post("/api/orders", json={
            "account_id": "bob", "side": "buy", "price": 100, "quantity": 1,
        })
    page1 = client.get("/api/trades?limit=3").json()
    assert len(page1["items"]) == 3
    assert page1["next_cursor"] is not None
    page2 = client.get(f"/api/trades?limit=3&cursor={page1['next_cursor']}").json()
    assert len(page2["items"]) == 3
    orders = client.get("/api/orders?limit=5").json()
    assert len(orders["items"]) == 5


def test_validation_errors(client):
    client.post("/api/accounts", json={"account_id": "alice", "initial_balance": 10000})
    # Zero price.
    r = client.post("/api/orders", json={
        "account_id": "alice", "side": "buy", "price": 0, "quantity": 5,
    })
    assert r.status_code == 422
    # Negative quantity.
    r = client.post("/api/orders", json={
        "account_id": "alice", "side": "buy", "price": 100, "quantity": -1,
    })
    assert r.status_code == 422
