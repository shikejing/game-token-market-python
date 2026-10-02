"""Price/time priority, partial fills, escrow and conservation."""
import threading

import pytest

from app.errors import Conflict, NotFound
from app.models import OrderStatus, OrderSubmit, Side

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def balances(engine, account):
    view = engine.account_view(account)
    return {b["asset"]: (b["available"], b["frozen"], b["total"])
            for b in view["balances"]}


def totals(engine, asset):
    return sum(
        a.available.get(asset, 0) + a.frozen.get(asset, 0)
        for a in engine.accounts.values()
    )


def test_freeze_on_submit_and_settle_on_trade(engine):
    give(engine, "seller", "GEM", 100)
    give(engine, "buyer", "GOLD", 500)
    r = engine.submit(OrderSubmit(account="seller", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=40))
    assert r["status"] == OrderStatus.OPEN.value
    assert balances(engine, "seller")["GEM"] == (60, 40, 100)
    b = engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                                  side=Side.BUY, price=5, qty=40))
    assert b["status"] == OrderStatus.FILLED.value
    # buyer spent 200 GOLD frozen, received 40 GEM
    assert balances(engine, "buyer")["GOLD"] == (300, 0, 300)
    assert balances(engine, "buyer")["GEM"] == (40, 0, 40)
    # seller delivered 40 frozen GEM, received 200 GOLD
    assert balances(engine, "seller")["GEM"] == (60, 0, 60)
    assert balances(engine, "seller")["GOLD"] == (200, 0, 200)
    assert totals(engine, "GEM") == 100
    assert totals(engine, "GOLD") == 500


def test_price_priority_then_time_priority(engine):
    give(engine, "s1", "GEM", 100)
    give(engine, "s2", "GEM", 100)
    give(engine, "s3", "GEM", 100)
    give(engine, "b", "GOLD", 10_000)
    engine.submit(OrderSubmit(account="s1", symbol=SYMBOL,
                              side=Side.SELL, price=10, qty=10))
    engine.submit(OrderSubmit(account="s2", symbol=SYMBOL,
                              side=Side.SELL, price=9, qty=10))
    engine.submit(OrderSubmit(account="s3", symbol=SYMBOL,
                              side=Side.SELL, price=9, qty=10))
    r = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=10, qty=15))
    # lowest price first, then earliest time
    assert [(t["maker_order_id"], t["price"], t["qty"])
            for t in r["trades"]] == [(2, 9, 10), (3, 9, 5)]
    # buy rests fully consumed
    assert r["filled_qty"] == 15
    # maker s3 partially filled remains on book at 9
    depth = engine.depth(SYMBOL)
    assert depth["asks"] == [{"price": 9, "qty": 5},
                             {"price": 10, "qty": 10}]


def test_partial_fill_stays_on_book_and_continues(engine):
    give(engine, "seller", "GEM", 100)
    give(engine, "buyer", "GOLD", 1000)
    r = engine.submit(OrderSubmit(account="seller", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=100,
                                  client_request_id="wall"))
    wall_id = r["order_id"]
    b1 = engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                                   side=Side.BUY, price=5, qty=30))
    assert b1["filled_qty"] == 30
    order = engine.orders[wall_id]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == 30
    assert engine.depth(SYMBOL)["asks"] == [{"price": 5, "qty": 70}]
    b2 = engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                                   side=Side.BUY, price=5, qty=70))
    assert b2["filled_qty"] == 70
    assert engine.orders[wall_id].status == OrderStatus.FILLED
    assert engine.depth(SYMBOL)["asks"] == []


def test_unmatched_buy_rests_as_bid_with_quote_frozen(engine):
    give(engine, "buyer", "GOLD", 100)
    r = engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                                  side=Side.BUY, price=4, qty=10))
    assert r["status"] == OrderStatus.OPEN.value
    assert balances(engine, "buyer")["GOLD"] == (60, 40, 100)
    assert engine.depth(SYMBOL)["bids"] == [{"price": 4, "qty": 10}]


def test_cancel_releases_remaining_freeze(engine):
    give(engine, "seller", "GEM", 100)
    give(engine, "buyer", "GOLD", 1000)
    r = engine.submit(OrderSubmit(account="seller", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=100))
    engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                              side=Side.BUY, price=5, qty=40))
    cr = engine.cancel("seller", r["order_id"])
    assert cr["cancelled_qty"] == 60
    assert cr["status"] == OrderStatus.CANCELLED.value
    # 40 sold + 60 released
    assert balances(engine, "seller")["GEM"] == (60, 0, 60)
    assert balances(engine, "seller")["GOLD"] == (200, 0, 200)
    assert engine.open_orders() == []
    # cancelling again conflicts
    with pytest.raises(Conflict):
        engine.cancel("seller", r["order_id"])
    with pytest.raises(NotFound):
        engine.cancel("seller", 999_999)
    with pytest.raises(NotFound):
        engine.cancel("someone-else", r["order_id"])


def test_insufficient_balance_rejected_without_freeze(engine):
    give(engine, "seller", "GEM", 5)
    give(engine, "buyer", "GOLD", 10)
    r1 = engine.submit(OrderSubmit(account="seller", symbol=SYMBOL,
                                   side=Side.SELL, price=5, qty=10))
    assert r1["accepted"] is False
    assert r1["reject_reason"] == "INSUFFICIENT_FUNDS"
    r2 = engine.submit(OrderSubmit(account="buyer", symbol=SYMBOL,
                                   side=Side.BUY, price=5, qty=10))
    assert r2["accepted"] is False
    assert balances(engine, "seller")["GEM"] == (5, 0, 5)
    assert balances(engine, "buyer")["GOLD"] == (10, 0, 10)
    assert engine.open_orders() == []


def test_no_cross_when_prices_do_not_overlap(engine):
    give(engine, "s", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=10, qty=10))
    r = engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=9, qty=10))
    assert r["filled_qty"] == 0
    depth = engine.depth(SYMBOL)
    assert depth["bids"] == [{"price": 9, "qty": 10}]
    assert depth["asks"] == [{"price": 10, "qty": 10}]


def test_trades_paginate_with_cursor_and_time_range(engine):
    give(engine, "s", "GEM", 1000)
    give(engine, "b", "GOLD", 1_000_000)
    engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                              side=Side.SELL, price=1, qty=100))
    for i in range(5):
        engine.submit(OrderSubmit(account="b", symbol=SYMBOL,
                                  side=Side.BUY, price=1, qty=20))
    page1, cur1 = engine.trades_page(SYMBOL, None, None, None, 2)
    assert [t.trade_id for t in page1] == [1, 2]
    page2, cur2 = engine.trades_page(SYMBOL, None, None, cur1, 2)
    assert [t.trade_id for t in page2] == [3, 4]
    page3, cur3 = engine.trades_page(SYMBOL, None, None, cur2, 2)
    assert [t.trade_id for t in page3] == [5]
    assert cur3 is None
    t3 = engine.all_trades[2].created_at
    page_t, _ = engine.trades_page(SYMBOL, t3, t3, None, 10)
    assert [t.trade_id for t in page_t] == [3]


def test_sell_taker_hits_highest_bid_then_earliest(engine):
    give(engine, "b1", "GOLD", 1000)
    give(engine, "b2", "GOLD", 1000)
    give(engine, "b3", "GOLD", 1000)
    give(engine, "s", "GEM", 100)
    engine.submit(OrderSubmit(account="b1", symbol=SYMBOL,
                              side=Side.BUY, price=8, qty=10))
    engine.submit(OrderSubmit(account="b2", symbol=SYMBOL,
                              side=Side.BUY, price=9, qty=10))
    engine.submit(OrderSubmit(account="b3", symbol=SYMBOL,
                              side=Side.BUY, price=9, qty=10))
    r = engine.submit(OrderSubmit(account="s", symbol=SYMBOL,
                                  side=Side.SELL, price=8, qty=15))
    assert [(t["maker_order_id"], t["price"], t["qty"])
            for t in r["trades"]] == [(2, 9, 10), (3, 9, 5)]
    depth = engine.depth(SYMBOL)
    assert depth["bids"] == [{"price": 9, "qty": 5},
                             {"price": 8, "qty": 10}]


def test_book_agglomerates_same_price_levels(engine):
    give(engine, "s1", "GEM", 100)
    give(engine, "s2", "GEM", 100)
    give(engine, "b", "GOLD", 1000)
    engine.submit(OrderSubmit(account="s1", symbol=SYMBOL,
                              side=Side.SELL, price=7, qty=30))
    engine.submit(OrderSubmit(account="s2", symbol=SYMBOL,
                              side=Side.SELL, price=7, qty=20))
    assert engine.depth(SYMBOL)["asks"] == [{"price": 7, "qty": 50}]
