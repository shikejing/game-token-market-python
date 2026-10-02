"""Durable recovery: balances, freezes, open orders, trades, continued trading."""
import os

from app.engine import MatchingEngine
from app.models import OrderStatus, OrderSubmit, Side
from app.storage import EventStore

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def replay(svc, data_dir):
    svc.stop()
    store = EventStore(data_dir)
    engine = MatchingEngine(store)
    engine.restore()
    return engine


def test_full_state_restored_and_book_keeps_matching(svc, tmp_path):
    engine = svc.engine
    give(engine, "s1", "GEM", 100)
    give(engine, "s2", "GEM", 100)
    give(engine, "b", "GOLD", 10_000)
    engine.submit(OrderSubmit(account="s1", symbol=SYMBOL,
                              side=Side.SELL, price=10, qty=40,
                              client_request_id="s1"))
    engine.submit(OrderSubmit(account="s2", symbol=SYMBOL,
                              side=Side.SELL, price=9, qty=60,
                              client_request_id="s2"))
    engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                              side=Side.BUY, price=10, qty=70,
                              client_request_id="b1"))

    data_dir = str(tmp_path / "marketdata")
    replayed = replay(svc, data_dir)

    # trades: 60 @9 (s2) + 10 @10 (s1)
    assert [(t.price, t.qty) for t in replayed.all_trades] == [(9, 60), (10, 10)]
    s1 = replayed.orders[1]
    assert s1.status == OrderStatus.PARTIALLY_FILLED
    assert s1.filled_qty == 10
    assert replayed.orders[2].status == OrderStatus.FILLED
    # depth reflects remaining s1 wall of 30 @10
    assert replayed.depth(SYMBOL)["asks"] == [{"price": 10, "qty": 30}]

    def total(asset):
        return sum(a.available.get(asset, 0) + a.frozen.get(asset, 0)
                   for a in replayed.accounts.values())
    assert total("GEM") == 200
    assert total("GOLD") == 10_000
    # frozen GEM == 30 resting sell, frozen GOLD == 0
    assert sum(a.frozen.get("GEM", 0)
               for a in replayed.accounts.values()) == 30
    assert sum(a.frozen.get("GOLD", 0)
               for a in replayed.accounts.values()) == 0

    # order id / trade id sequences continue correctly
    give(replayed, "b", "GOLD", 1000)
    r = replayed.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                    side=Side.BUY, price=10, qty=30,
                                    client_request_id="b2"))
    assert r["order_id"] == 4
    assert r["filled_qty"] == 30
    assert [(t["trade_id"], t["price"]) for t in r["trades"]] == [(3, 10)]
    assert replayed.orders[1].status == OrderStatus.FILLED
    assert replayed.depth(SYMBOL)["asks"] == []
    # frozen released after full settlement
    assert sum(a.frozen.get("GEM", 0)
               for a in replayed.accounts.values()) == 0


def test_cancelled_order_and_pagination_restored(svc, tmp_path):
    engine = svc.engine
    give(engine, "s", "GEM", 100)
    r = engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                                  side=Side.SELL, price=7, qty=100,
                                  client_request_id="wall"))
    engine.cancel("s", r["order_id"], client_request_id="cancel-wall")
    data_dir = str(tmp_path / "marketdata")
    replayed = replay(svc, data_dir)
    assert replayed.orders[r["order_id"]].status == OrderStatus.CANCELLED
    assert replayed.open_orders() == []
    acc = replayed.account_view("s")["balances"][0]
    assert acc == {"asset": "GEM", "available": 100, "frozen": 0, "total": 100}
    # duplicate cancel still returns the first response
    dup = replayed.cancel("s", r["order_id"],
                          client_request_id="cancel-wall")
    assert dup["duplicate"] is True and dup["cancelled_qty"] == 100


def test_torn_tail_is_discarded_and_service_continues(svc, tmp_path):
    engine = svc.engine
    give(engine, "s", "GEM", 100)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=5, qty=10,
                              client_request_id="good"))
    svc.stop()
    log_path = os.path.join(str(tmp_path / "marketdata"), "events.log")
    with open(log_path, "rb") as f:
        raw = f.read()
    # append a garbage partial frame, as a crash mid-write would leave
    with open(log_path, "wb") as f:
        f.write(raw + b"\x10\x00\x00\x00\xff\xff\xff\xff{broken")
    store = EventStore(str(tmp_path / "marketdata"))
    replayed = MatchingEngine(store)
    replayed.restore()
    assert len(replayed.orders) == 1
    # store truncates the torn tail; appends keep working
    replayed.add_instrument("X-Y", "X", "Y")
    assert "X-Y" in replayed.instruments
    again = MatchingEngine(EventStore(str(tmp_path / "marketdata")))
    again.restore()
    assert "X-Y" in again.instruments and len(again.orders) == 1
