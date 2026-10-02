"""HTTP-level integration through FastAPI TestClient."""
import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(svc):
    return TestClient(main.app)


def test_root_endpoint_still_works(client):
    assert client.get("/").json() == {"Hello": "World"}


def test_full_trade_lifecycle_over_http(client):
    assert client.post("/accounts/alice/deposit?asset=GEM&amount=100"
                       ).status_code == 200
    assert client.post("/accounts/bob/deposit?asset=GOLD&amount=1000"
                       ).status_code == 200

    sell = client.post("/orders", json={
        "account": "alice", "symbol": "GEM-GOLD", "side": "SELL",
        "price": 5, "qty": 40, "client_request_id": "sell-1"}).json()
    assert sell["order_id"] == 1 and sell["status"] == "OPEN"

    # duplicate submission over HTTP returns identical first result
    dup = client.post("/orders", json={
        "account": "alice", "symbol": "GEM-GOLD", "side": "SELL",
        "price": 5, "qty": 40, "client_request_id": "sell-1"}).json()
    assert dup["duplicate"] is True and dup["order_id"] == 1

    buy = client.post("/orders", json={
        "account": "bob", "symbol": "GEM-GOLD", "side": "BUY",
        "price": 5, "qty": 40}).json()
    assert buy["filled_qty"] == 40 and buy["trades"][0]["trade_id"] == 1

    alice = client.get("/accounts/alice").json()
    bob = client.get("/accounts/bob").json()
    assert {b["asset"]: b["total"] for b in alice["balances"]} == {
        "GEM": 60, "GOLD": 200}
    assert {b["asset"]: b["total"] for b in bob["balances"]} == {
        "GEM": 40, "GOLD": 800}

    trades = client.get("/trades/GEM-GOLD").json()
    assert trades["items"][0]["qty"] == 40

    # cancelling the filled order conflicts with 409
    resp = client.post("/orders/cancel",
                       json={"account": "alice", "order_id": 1})
    assert resp.status_code == 409


def test_reject_and_not_found_status_codes(client):
    client.post("/accounts/x/deposit?asset=GOLD&amount=3")
    resp = client.post("/orders", json={
        "account": "x", "symbol": "GEM-GOLD", "side": "BUY",
        "price": 5, "qty": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["reject_reason"] == "INSUFFICIENT_FUNDS"
    assert client.get("/depth/NOPE").status_code == 404
    assert client.post("/orders/cancel",
                       json={"account": "x", "order_id": 999}).status_code == 404
    # validation errors stay 422
    bad = client.post("/orders", json={
        "account": "x", "symbol": "GEM-GOLD", "side": "BUY",
        "price": -1, "qty": 0})
    assert bad.status_code == 422


def test_depth_and_open_orders_pagination(client):
    client.post("/accounts/s1/deposit?asset=GEM&amount=1000")
    client.post("/accounts/s2/deposit?asset=GEM&amount=1000")
    client.post("/accounts/b/deposit?asset=GOLD&amount=100000")
    for price, qty in ((9, 10), (8, 20), (8, 30)):
        client.post("/orders", json={
            "account": "s1", "symbol": "GEM-GOLD", "side": "SELL",
            "price": price, "qty": qty})
    client.post("/orders", json={
        "account": "s2", "symbol": "GEM-GOLD", "side": "SELL",
        "price": 7, "qty": 50})
    depth = client.get("/depth/GEM-GOLD?levels=2").json()
    assert depth["asks"] == [{"price": 7, "qty": 50},
                             {"price": 8, "qty": 50}]
    page1 = client.get("/orders?account=s1&limit=2").json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    page2 = client.get(
        f"/orders?account=s1&limit=2&cursor={page1['next_cursor']}").json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    # buy through two levels: 50 @7 + 10 @8
    r = client.post("/orders", json={
        "account": "b", "symbol": "GEM-GOLD", "side": "BUY",
        "price": 9, "qty": 60}).json()
    assert [(t["price"], t["qty"]) for t in r["trades"]] == [(7, 50), (8, 10)]


def test_trade_pagination_cursor_stable(client):
    client.post("/accounts/s/deposit?asset=GEM&amount=100")
    client.post("/accounts/b/deposit?asset=GOLD&amount=1000")
    client.post("/orders", json={"account": "s", "symbol": "GEM-GOLD",
                                 "side": "SELL", "price": 1, "qty": 10})
    for i in range(3):
        client.post("/orders", json={
            "account": "b", "symbol": "GEM-GOLD", "side": "BUY",
            "price": 1, "qty": i + 1})
    page = client.get("/trades/GEM-GOLD?limit=2").json()
    assert [t["trade_id"] for t in page["items"]] == [1, 2]
    page2 = client.get(
        f"/trades/GEM-GOLD?limit=2&cursor={page['next_cursor']}").json()
    assert [t["trade_id"] for t in page2["items"]] == [3]
