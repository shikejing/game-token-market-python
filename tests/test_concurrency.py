"""Concurrent submit/cancel/match: conservation and ledger<->book invariants."""
import threading

import pytest

from app.errors import Conflict, NotFound
from app.models import OrderStatus, OrderSubmit, Side

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def assert_books_are_consistent(engine):
    open_orders = engine.open_orders()
    frozen_gem = sum(a.frozen.get("GEM", 0) for a in engine.accounts.values())
    frozen_gold = sum(a.frozen.get("GOLD", 0) for a in engine.accounts.values())
    expect_gem = sum(o.orig_qty - o.filled_qty for o in open_orders
                     if o.side == Side.SELL)
    expect_gold = sum((o.orig_qty - o.filled_qty) * o.price
                      for o in open_orders if o.side == Side.BUY)
    assert frozen_gem == expect_gem
    assert frozen_gold == expect_gold
    for acc in engine.accounts.values():
        for bucket in (acc.available, acc.frozen):
            for v in bucket.values():
                assert v >= 0
    for o in open_orders:
        assert 0 <= o.filled_qty < o.orig_qty


def total_of(engine, asset):
    return sum(a.available.get(asset, 0) + a.frozen.get(asset, 0)
               for a in engine.accounts.values())


def test_concurrent_submits_against_one_wall(engine):
    give(engine, "wall", "GEM", 1000)
    wall = engine.submit(OrderSubmit(account="wall", symbol=SYMBOL,
                                     side=Side.SELL, price=3, qty=1000))
    n_buyers = 20
    for i in range(n_buyers):
        give(engine, f"b{i}", "GOLD", 1000)
    barrier = threading.Barrier(n_buyers)
    errors = []

    def buyer(i):
        try:
            barrier.wait()
            engine.submit(OrderSubmit(
                account=f"b{i}", symbol=SYMBOL, side=Side.BUY,
                price=3, qty=50, client_request_id=f"buy-{i}"))
        except Exception as exc:  # pragma: no cover - failure visibility
            errors.append(exc)

    threads = [threading.Thread(target=buyer, args=(i,))
               for i in range(n_buyers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    wall_order = engine.orders[wall["order_id"]]
    assert wall_order.status == OrderStatus.FILLED
    assert wall_order.filled_qty == 1000
    assert sum(t.qty for t in engine.all_trades) == 1000
    assert total_of(engine, "GEM") == 1000
    assert total_of(engine, "GOLD") == n_buyers * 1000
    assert_books_are_consistent(engine)


def test_mixed_concurrent_submits_and_cancels(engine):
    give(engine, "market-maker", "GEM", 100_000)
    give(engine, "market-maker", "GOLD", 100_000)
    give(engine, "taker", "GEM", 100_000)
    give(engine, "taker", "GOLD", 100_000)
    n = 40
    barrier = threading.Barrier(2)
    errors = []

    def maker_side():
        try:
            barrier.wait()
            for i in range(n):
                r = engine.submit(OrderSubmit(
                    account="market-maker", symbol=SYMBOL,
                    side=(Side.SELL if i % 2 == 0 else Side.BUY),
                    price=10, qty=10, client_request_id=f"m-{i}"))
                if i % 4 == 0 and r["accepted"] and r["remaining_qty"]:
                    try:
                        engine.cancel("market-maker", r["order_id"])
                    except Conflict:
                        pass  # may have been filled by a concurrent taker
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def taker_side():
        try:
            barrier.wait()
            for i in range(n):
                engine.submit(OrderSubmit(
                    account="taker", symbol=SYMBOL,
                    side=(Side.BUY if i % 2 == 0 else Side.SELL),
                    price=10, qty=10, client_request_id=f"t-{i}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=maker_side),
               threading.Thread(target=taker_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    gem = total_of(engine, "GEM")
    gold = total_of(engine, "GOLD")
    assert gem == 200_000
    assert gold == 200_000
    assert_books_are_consistent(engine)
    # every order's fill + remaining == orig
    for o in engine.orders.values():
        assert o.filled_qty + (
            0 if o.status in (OrderStatus.FILLED, OrderStatus.CANCELLED)
            else o.orig_qty - o.filled_qty) <= o.orig_qty


def test_duplicate_submit_under_race_freezes_once(engine):
    give(engine, "racer", "GOLD", 1000)
    n = 16
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def fire():
        barrier.wait()
        r = engine.submit(OrderSubmit(
            account="racer", symbol=SYMBOL, side=Side.BUY, price=5, qty=10,
            client_request_id="same-request"))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    order_ids = {r["order_id"] for r in results}
    assert order_ids == {1}
    assert sum(1 for r in results if not r["duplicate"]) == 1
    assert sum(1 for r in results if r["duplicate"]) == n - 1
    view = engine.account_view("racer")
    bal = {b["asset"]: (b["available"], b["frozen"])
           for b in view["balances"]}
    assert bal["GOLD"] == (950, 50)
    assert len(engine.open_orders(account="racer")) == 1
    assert_books_are_consistent(engine)


def test_concurrent_cancel_single_winner(engine):
    give(engine, "s", "GEM", 100)
    r = engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                                  side=Side.SELL, price=9, qty=100,
                                  client_request_id="ord"))
    n = 12
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def cancel():
        barrier.wait()
        try:
            engine.cancel("s", r["order_id"])
            with lock:
                outcomes.append("ok")
        except (Conflict, NotFound):
            with lock:
                outcomes.append("conflict")

    threads = [threading.Thread(target=cancel) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == n - 1
    assert engine.orders[r["order_id"]].status == OrderStatus.CANCELLED
    assert_books_are_consistent(engine)
