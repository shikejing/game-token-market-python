"""Restart recovery: orders, balances, freezes, trades must be restored."""
import json
import os

from market.engine import MarketEngine
from market.models import Side


def _restart(tmp_path):
    e = MarketEngine(str(tmp_path / "data"))
    e.load()
    return e


def test_recover_accounts_and_balances(funded_engine, tmp_path):
    e = funded_engine
    e.deposit("alice", 500)
    e2 = _restart(tmp_path)
    assert e2.get_account("alice").balance == 1_000_000 + 500
    assert e2.get_account("bob").balance == 1_000_000
    e2.check_invariant()


def test_recover_open_orders_and_freezes(funded_engine, tmp_path):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")  # rests, 10 frozen
    e.submit_order("bob", Side.BUY, 99, 5, "b1")       # rests, 495 frozen
    e2 = _restart(tmp_path)
    assert len(e2.orders) == 2
    assert e2.get_account("alice").frozen == 10
    assert e2.get_account("bob").frozen == 495
    # Book rebuilt: orders still matchable.
    depth = e2.depth()
    assert depth["asks"][0]["price"] == 100
    assert depth["bids"][0]["price"] == 99
    e2.check_invariant()


def test_recover_filled_orders_and_trades(funded_engine, tmp_path):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 10, "b1")
    e2 = _restart(tmp_path)
    assert len(e2.trades) == 1
    assert e2.trades[0].quantity == 10
    assert e2.trades[0].price == 100
    assert e2.get_account("alice").balance == 1_000_000 + 1000 - 10
    assert e2.get_account("bob").balance == 1_000_000 - 1000 + 10
    e2.check_invariant()


def test_recover_partial_fill_then_resume(funded_engine, tmp_path):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 4, "b1")  # alice: 4 filled, 6 open
    e2 = _restart(tmp_path)
    # Resume trading on recovered state.
    r = e2.submit_order("bob", Side.BUY, 100, 6, "b2")
    assert r["order"]["status"] == "filled"
    assert len(r["trades"]) == 1
    assert r["trades"][0]["quantity"] == 6
    assert e2.get_account("alice").frozen == 0
    e2.check_invariant()


def test_recover_cancelled_orders(funded_engine, tmp_path):
    e = funded_engine
    r1 = e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 4, "b1")
    e.cancel_order("alice", r1["order"]["order_id"], "c1")
    e2 = _restart(tmp_path)
    order = e2.get_order(r1["order"]["order_id"])
    assert order.status.value == "cancelled"
    assert e2.get_account("alice").frozen == 0
    e2.check_invariant()


def test_recover_with_snapshot_and_journal(funded_engine, tmp_path):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.snapshot()
    # New events after the snapshot.
    e.submit_order("bob", Side.BUY, 100, 4, "b1")
    e2 = _restart(tmp_path)
    assert len(e2.orders) == 2
    assert len(e2.trades) == 1
    assert e2.get_account("alice").frozen == 6
    e2.check_invariant()


def test_torn_journal_line_truncated(funded_engine, tmp_path):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    # Simulate a torn write: append a partial (invalid JSON) last line.
    journal_path = os.path.join(str(tmp_path / "data"), "journal.log")
    with open(journal_path, "a") as f:
        f.write('{"seq": 2, "type": "submit", "order": {"order_id": "torn')
        f.flush()
    e2 = _restart(tmp_path)
    # The torn line is dropped; the valid order survives.
    assert len(e2.orders) == 1
    assert e2.get_account("alice").frozen == 10
    # Journal is truncated to the last valid line (account_open + submit).
    with open(journal_path) as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 3
    e2.check_invariant()


def test_recovered_state_is_self_consistent(funded_engine, tmp_path):
    """After recovery, order book and account state must be mutually consistent."""
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 4, "b1")
    e.submit_order("alice", Side.BUY, 101, 3, "b2")
    e.snapshot()
    e.submit_order("bob", Side.SELL, 101, 2, "s2")
    e2 = _restart(tmp_path)
    e2.check_invariant()
    # Active orders in the book match the depth.
    bids, asks = e2.book.depth(10)
    book_qty = sum(d.quantity for d in bids) + sum(d.quantity for d in asks)
    active_qty = sum(o.remaining for o in e2.orders.values() if o.is_active)
    assert book_qty == active_qty
