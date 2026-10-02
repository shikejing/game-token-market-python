"""Matching engine semantics: price-time priority, partial fills, removal."""
from market.models import OrderStatus, Side


def _order_status(engine, order_id):
    return engine.get_order(order_id).status


def test_buy_matches_lowest_ask_first(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.SELL, 99, 10, "s2")
    r = e.submit_order("alice", Side.BUY, 100, 5, "b1")
    assert r["order"]["status"] == OrderStatus.FILLED.value
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["price"] == 99  # maker price (cheapest ask)
    assert t["quantity"] == 5
    # The 99 ask is partially filled and remains; the 100 ask untouched.
    asks = e.depth()["asks"]
    assert asks[0]["price"] == 99
    assert asks[0]["quantity"] == 5
    assert asks[1]["price"] == 100
    assert asks[1]["quantity"] == 10


def test_sell_matches_highest_bid_first(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.BUY, 98, 10, "b1")
    e.submit_order("bob", Side.BUY, 100, 10, "b2")
    r = e.submit_order("alice", Side.SELL, 99, 5, "s1")
    assert r["order"]["status"] == OrderStatus.FILLED.value
    t = r["trades"][0]
    assert t["price"] == 100  # highest bid
    assert t["quantity"] == 5
    bids = e.depth()["bids"]
    assert bids[0]["price"] == 100
    assert bids[0]["quantity"] == 5
    assert bids[1]["price"] == 98
    assert bids[1]["quantity"] == 10


def test_time_priority_at_same_price(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    e.submit_order("bob", Side.SELL, 100, 5, "s2")
    r = e.submit_order("alice", Side.BUY, 100, 5, "b1")
    # First ask (s1) fully filled at 100; s2 remains.
    assert r["trades"][0]["price"] == 100
    depth = e.depth()
    assert depth["asks"][0]["quantity"] == 5
    assert depth["asks"][0]["order_count"] == 1


def test_partial_fill_then_rest(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 3, "s1")
    r = e.submit_order("bob", Side.BUY, 100, 10, "b1")
    assert r["order"]["status"] == OrderStatus.PARTIALLY_FILLED.value
    assert r["order"]["remaining"] == 7
    assert len(r["trades"]) == 1
    assert r["trades"][0]["quantity"] == 3
    depth = e.depth()
    assert depth["bids"][0]["price"] == 100
    assert depth["bids"][0]["quantity"] == 7


def test_full_fill_removes_from_book(funded_engine):
    e = funded_engine
    r1 = e.submit_order("alice", Side.SELL, 100, 5, "s1")
    r2 = e.submit_order("bob", Side.BUY, 100, 5, "b1")
    # Re-query: the submit response is a point-in-time snapshot.
    assert _order_status(e, r1["order"]["order_id"]) == OrderStatus.FILLED
    assert _order_status(e, r2["order"]["order_id"]) == OrderStatus.FILLED
    assert e.depth() == {"bids": [], "asks": []}
    assert not e.book.has_order(r1["order"]["order_id"])
    assert not e.book.has_order(r2["order"]["order_id"])


def test_no_match_when_prices_dont_cross(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 101, 5, "s1")
    r = e.submit_order("bob", Side.BUY, 100, 5, "b1")
    assert r["order"]["status"] == OrderStatus.OPEN.value
    assert r["trades"] == []
    depth = e.depth()
    assert depth["bids"][0]["price"] == 100
    assert depth["asks"][0]["price"] == 101


def test_multiple_levels_sweep(funded_engine):
    e = funded_engine
    for i, px in enumerate([100, 101, 102]):
        e.submit_order("alice", Side.SELL, px, 2, f"s{i}")
    # Buy 6: sweeps 2@100 + 2@101 + 2@102, leaving 1 bid @102.
    r = e.submit_order("bob", Side.BUY, 102, 7, "b1")
    assert r["order"]["status"] == OrderStatus.PARTIALLY_FILLED.value
    assert r["order"]["remaining"] == 1
    assert len(r["trades"]) == 3
    assert [t["price"] for t in r["trades"]] == [100, 101, 102]
    assert [t["quantity"] for t in r["trades"]] == [2, 2, 2]
    depth = e.depth()
    assert depth["bids"][0]["price"] == 102
    assert depth["bids"][0]["quantity"] == 1
    assert depth["asks"] == []
