"""Idempotency: repeated client request keys must not duplicate orders/freezes."""
from market.models import Side


def test_duplicate_submit_returns_same_order(funded_engine):
    e = funded_engine
    r1 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    r2 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    assert r1["order"]["order_id"] == r2["order"]["order_id"]
    assert len(e.orders) == 1
    # Only one freeze: 500, not 1000.
    assert e.get_account("alice").frozen == 500
    assert e.get_account("alice").balance == 1_000_000 - 500
    e.check_invariant()


def test_duplicate_submit_after_fill(funded_engine):
    e = funded_engine
    e.submit_order("bob", Side.SELL, 100, 5, "s1")
    r1 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    # Order fully filled; duplicate must return the same first response.
    r2 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    assert r1["order"]["order_id"] == r2["order"]["order_id"]
    assert r2["order"]["status"] == "filled"
    assert len(e.orders) == 2
    e.check_invariant()


def test_different_keys_create_different_orders(funded_engine):
    e = funded_engine
    r1 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    r2 = e.submit_order("alice", Side.BUY, 100, 5, "key-2")
    assert r1["order"]["order_id"] != r2["order"]["order_id"]
    assert len(e.orders) == 2
    assert e.get_account("alice").frozen == 1000
    e.check_invariant()


def test_no_key_no_dedup(funded_engine):
    e = funded_engine
    r1 = e.submit_order("alice", Side.BUY, 100, 5, None)
    r2 = e.submit_order("alice", Side.BUY, 100, 5, None)
    assert r1["order"]["order_id"] != r2["order"]["order_id"]
    assert len(e.orders) == 2


def test_duplicate_cancel_returns_same(funded_engine):
    e = funded_engine
    r1 = e.submit_order("alice", Side.SELL, 100, 5, "s1")
    order_id = r1["order"]["order_id"]
    c1 = e.cancel_order("alice", order_id, "cancel-1")
    c2 = e.cancel_order("alice", order_id, "cancel-1")
    assert c1["order"]["order_id"] == c2["order"]["order_id"]
    assert c2["order"]["status"] == "cancelled"
    e.check_invariant()


def test_idempotency_survives_restart(funded_engine, tmp_path):
    e = funded_engine
    r1 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    e.snapshot()
    # Restart in a new engine instance.
    from market.engine import MarketEngine
    e2 = MarketEngine(str(tmp_path / "data"))
    e2.load()
    r2 = e2.submit_order("alice", Side.BUY, 100, 5, "key-1")
    assert r1["order"]["order_id"] == r2["order"]["order_id"]
    assert len(e2.orders) == 1
    e2.check_invariant()


def test_idempotency_survives_restart_without_snapshot(funded_engine, tmp_path):
    """Replay from journal alone must rebuild the idempotency cache."""
    e = funded_engine
    r1 = e.submit_order("alice", Side.BUY, 100, 5, "key-1")
    # No snapshot; restart replays the journal.
    from market.engine import MarketEngine
    e2 = MarketEngine(str(tmp_path / "data"))
    e2.load()
    r2 = e2.submit_order("alice", Side.BUY, 100, 5, "key-1")
    assert r1["order"]["order_id"] == r2["order"]["order_id"]
    assert len(e2.orders) == 1
    e2.check_invariant()
