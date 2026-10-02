"""Failure injection: storage errors leave no partial state; durable-but-unapplied
events are recovered on restart."""
import threading

import pytest

from app.engine import CrashAfterDurableCommit
from app.errors import StorageError
from app.models import OrderStatus, OrderSubmit, Side

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def freeze_totals(engine):
    return (
        sum(a.available.get("GEM", 0) + a.frozen.get("GEM", 0)
            for a in engine.accounts.values()),
        sum(a.available.get("GOLD", 0) + a.frozen.get("GOLD", 0)
            for a in engine.accounts.values()),
    )


def test_submit_whole_batch_fails_atomically(svc, engine):
    give(engine, "s", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=5, qty=40))
    gem, gold = freeze_totals(engine)

    svc.store.fail_next_writes(1)
    with pytest.raises(StorageError):
        engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=40,
                                  client_request_id="atomic-buy"))
    # no new order, no trade, no balance movement
    assert 2 not in engine.orders
    assert engine.all_trades == []
    assert freeze_totals(engine) == (gem, gold)
    view = engine.account_view("b")
    assert view["balances"][0]["frozen"] == 0
    assert view["balances"][0]["available"] == 1000
    # system keeps advancing after the failure
    r = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=40))
    assert r["filled_qty"] == 40
    assert len(engine.all_trades) == 1


def test_failed_write_does_not_consume_idempotency(svc, engine):
    give(engine, "b", "GOLD", 1000)
    svc.store.fail_next_writes(1)
    with pytest.raises(StorageError):
        engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=10,
                                  client_request_id="retry-me"))
    # the request id was never recorded durably or in memory
    r = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=10,
                                  client_request_id="retry-me"))
    assert r["duplicate"] is False and r["accepted"] is True


def test_crash_between_durable_append_and_apply(svc, engine, tmp_path):
    give(engine, "s", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=5, qty=40,
                              client_request_id="wall"))
    svc.store.crash_point(True)
    with pytest.raises(CrashAfterDurableCommit):
        engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=40,
                                  client_request_id="crash-buy"))
    # in-memory: taker order/trade were never applied
    assert 2 not in engine.orders
    assert engine.all_trades == []
    assert engine.accounts["b"].frozen.get("GOLD", 0) == 0

    # restart: durable batch is replayed, everything becomes consistent
    from app.engine import MatchingEngine
    from app.storage import EventStore
    svc.stop()
    replayed = MatchingEngine(EventStore(str(tmp_path / "marketdata")))
    replayed.restore()
    assert len(replayed.all_trades) == 1
    taker = replayed.orders[2]
    assert taker.status == OrderStatus.FILLED
    wall = replayed.orders[1]
    assert wall.status == OrderStatus.FILLED
    # and the client request is now known: duplicate returns first response
    dup = replayed.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                      side=Side.BUY, price=5, qty=40,
                                      client_request_id="crash-buy"))
    assert dup["duplicate"] is True
    assert dup["filled_qty"] == 40
    # no second trade created by the duplicate replay
    assert len(replayed.all_trades) == 1


def test_failure_under_concurrency_never_corrupts(svc, engine):
    give(engine, "s", "GEM", 100_000)
    give(engine, "b", "GOLD", 100_000)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=5, qty=10_000,
                              client_request_id="wall"))
    n = 30
    barrier = threading.Barrier(n)
    errors = []

    def fire(i):
        barrier.wait()
        try:
            engine.submit(OrderSubmit(
                account="b", symbol=SYMBOL, side=Side.BUY, price=5, qty=10,
                client_request_id=f"buy-{i}"))
        except StorageError as exc:
            errors.append(exc)

    # a few writes fail mid-flight
    svc.store.fail_next_writes(5)
    threads = [threading.Thread(target=fire, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 5
    succeeded = n - len(errors)
    assert sum(t.qty for t in engine.all_trades) == succeeded * 10
    wall = engine.orders[1]
    assert wall.filled_qty == succeeded * 10
    gem = sum(a.available.get("GEM", 0) + a.frozen.get("GEM", 0)
              for a in engine.accounts.values())
    gold = sum(a.available.get("GOLD", 0) + a.frozen.get("GOLD", 0)
               for a in engine.accounts.values())
    assert gem == 100_000
    assert gold == 100_000
    # frozen quote for the resting wall matches exactly
    frozen_gem = sum(a.frozen.get("GEM", 0)
                     for a in engine.accounts.values())
    assert frozen_gem == 10_000 - succeeded * 10
    # retry the failed request ids: each creates exactly one order+fill
    before = len(engine.all_trades)
    for i in range(n):
        engine.submit(OrderSubmit(
            account="b", symbol=SYMBOL, side=Side.BUY, price=5, qty=10,
            client_request_id=f"buy-{i}"))
    assert len(engine.all_trades) == before + 5


def test_book_intact_after_failed_aggressive_order(svc, engine):
    give(engine, "s1", "GEM", 100)
    give(engine, "s2", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    engine.submit(OrderSubmit(account="s1", symbol=SYMBOL,
                              side=Side.SELL, price=9, qty=10))
    engine.submit(OrderSubmit(account="s2", symbol=SYMBOL,
                              side=Side.SELL, price=10, qty=10))
    svc.store.fail_next_writes(1)
    with pytest.raises(StorageError):
        engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=10, qty=20,
                                  client_request_id="failed-sweep"))
    # book untouched by the aborted simulation
    assert engine.depth(SYMBOL)["asks"] == [
        {"price": 9, "qty": 10}, {"price": 10, "qty": 10}]
    # a later order matches in exactly the right order
    r = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=10, qty=15))
    assert [(t["price"], t["qty"]) for t in r["trades"]] == [(9, 10), (10, 5)]
