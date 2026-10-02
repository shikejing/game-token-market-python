"""Client request idempotency for submit and cancel."""
import pytest

from app.models import OrderStatus, OrderSubmit, Side

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def test_duplicate_submit_returns_first_result(engine):
    give(engine, "a", "GOLD", 1000)
    payload = dict(account="a", symbol=SYMBOL, side=Side.BUY, price=5, qty=10,
                   client_request_id="req-1")
    first = engine.submit(OrderSubmit(**payload))
    # even a wildly different duplicate body returns the original outcome
    other = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                      side=Side.BUY, price=9, qty=99,
                                      client_request_id="req-1"))
    assert other["duplicate"] is True
    assert other["order_id"] == first["order_id"]
    assert other["price"] if "price" in other else True
    assert other["status"] == first["status"]
    assert len(engine.open_orders(account="a")) == 1
    view = engine.account_view("a")
    bal = {b["asset"]: b["frozen"] for b in view["balances"]}
    assert bal["GOLD"] == 50  # frozen exactly once


def test_rejection_is_idempotent(engine):
    give(engine, "a", "GOLD", 5)
    req = dict(account="a", symbol=SYMBOL, side=Side.BUY, price=5, qty=10,
               client_request_id="bad-req")
    r1 = engine.submit(OrderSubmit(**req))
    r2 = engine.submit(OrderSubmit(**req))
    assert r1["accepted"] is False
    assert r1["reject_reason"] == "INSUFFICIENT_FUNDS"
    assert r2["duplicate"] is True
    assert r2["accepted"] is False
    assert r2["order_id"] is None
    assert engine.open_orders() == []


def test_same_account_different_request_ids_are_distinct(engine):
    give(engine, "a", "GEM", 100)
    r1 = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                   side=Side.SELL, price=5, qty=10,
                                   client_request_id="r1"))
    r2 = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                   side=Side.SELL, price=5, qty=10,
                                   client_request_id="r2"))
    assert r1["order_id"] != r2["order_id"]
    assert len(engine.open_orders(account="a")) == 2


def test_same_request_id_different_accounts_are_distinct(engine):
    give(engine, "a", "GEM", 100)
    give(engine, "b", "GEM", 100)
    r1 = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                   side=Side.SELL, price=5, qty=10,
                                   client_request_id="dup"))
    r2 = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                   side=Side.SELL, price=5, qty=10,
                                   client_request_id="dup"))
    assert r1["order_id"] != r2["order_id"]


def test_idempotency_survives_restart(svc, engine, tmp_path):
    give(engine, "a", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    r = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=100,
                                  client_request_id="big-wall"))
    engine.submit(OrderSubmit(account="b", symbol=SYMBOL, side=Side.BUY,
                              price=5, qty=30, client_request_id="fill-30"))

    from app.engine import MatchingEngine
    from app.storage import EventStore
    svc.stop()
    replayed = MatchingEngine(EventStore(str(tmp_path / "marketdata")))
    replayed.restore()

    dup = replayed.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                      side=Side.SELL, price=5, qty=100,
                                      client_request_id="big-wall"))
    assert dup["duplicate"] is True
    assert dup["order_id"] == r["order_id"]
    # idempotent response is the verbatim first-response snapshot
    assert dup["filled_qty"] == 0
    assert dup["remaining_qty"] == 100
    # live order state reflects the later fill independently
    live = replayed.orders[r["order_id"]]
    assert live.filled_qty == 30
    assert live.orig_qty - live.filled_qty == 70
    fill_dup = replayed.submit(OrderSubmit(
        account="b", symbol=SYMBOL, side=Side.BUY, price=5, qty=30,
        client_request_id="fill-30"))
    assert fill_dup["duplicate"] is True
    assert fill_dup["filled_qty"] == 30
    # wall remains at 70 and only one trade exists
    assert len(replayed.open_orders()) == 1
    assert len(replayed.all_trades) == 1
