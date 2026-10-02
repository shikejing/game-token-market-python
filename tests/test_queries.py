"""Market data & account queries: recent trades, depth, orders, stable pagination."""
import time

from market.models import Side


def _populate(e):
    """Create a deterministic set of trades and orders."""
    for i in range(10):
        e.submit_order("alice", Side.SELL, 100, 1, f"s{i}")
        e.submit_order("bob", Side.BUY, 100, 1, f"b{i}")


def test_depth(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    e.submit_order("alice", Side.SELL, 101, 3, "s2")
    e.submit_order("bob", Side.BUY, 99, 4, "b1")
    depth = e.depth()
    assert depth["asks"][0] == {"price": 100, "quantity": 5, "order_count": 1}
    assert depth["asks"][1] == {"price": 101, "quantity": 3, "order_count": 1}
    assert depth["bids"][0] == {"price": 99, "quantity": 4, "order_count": 1}


def test_trades_pagination(funded_engine):
    e = funded_engine
    _populate(e)
    page1 = e.list_trades(limit=4)
    assert len(page1["items"]) == 4
    assert page1["next_cursor"] == page1["items"][-1]["seq"]
    page2 = e.list_trades(limit=4, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 4
    assert page2["items"][0]["seq"] > page1["items"][-1]["seq"]
    page3 = e.list_trades(limit=4, cursor=page2["next_cursor"])
    assert len(page3["items"]) == 2
    assert page3["next_cursor"] is None
    # No overlap across pages.
    all_ids = [t["trade_id"] for t in page1["items"] + page2["items"] + page3["items"]]
    assert len(all_ids) == len(set(all_ids)) == 10


def test_orders_pagination(funded_engine):
    e = funded_engine
    for i in range(10):
        e.submit_order("alice", Side.SELL, 100, 1, f"s{i}")
    page1 = e.list_orders(limit=3)
    assert len(page1["items"]) == 3
    page2 = e.list_orders(limit=3, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 3
    page3 = e.list_orders(limit=3, cursor=page2["next_cursor"])
    assert len(page3["items"]) == 3
    page4 = e.list_orders(limit=3, cursor=page3["next_cursor"])
    assert len(page4["items"]) == 1
    assert page4["next_cursor"] is None


def test_orders_filter_by_account_and_status(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    e.submit_order("bob", Side.BUY, 100, 3, "b1")  # fills 3 of alice's 5
    alice_orders = e.list_orders(account_id="alice")
    assert len(alice_orders["items"]) == 1
    assert alice_orders["items"][0]["status"] == "partially_filled"
    open_orders = e.list_orders(status="open")
    assert all(o["status"] == "open" for o in open_orders["items"])


def test_trades_time_range(funded_engine):
    e = funded_engine
    _populate(e)
    now = time.time()
    # All trades are in the past; a future start_ts returns nothing.
    future = e.list_trades(start_ts=now + 1000)
    assert future["items"] == []
    # A past start_ts returns all.
    past = e.list_trades(start_ts=0)
    assert len(past["items"]) == 10


def test_cursor_pagination_is_stable(funded_engine):
    """Paging twice with the same cursor returns the same items."""
    e = funded_engine
    _populate(e)
    p1 = e.list_trades(limit=5)
    p2 = e.list_trades(limit=5, cursor=p1["next_cursor"])
    p2_again = e.list_trades(limit=5, cursor=p1["next_cursor"])
    assert [t["trade_id"] for t in p2["items"]] == [t["trade_id"] for t in p2_again["items"]]
