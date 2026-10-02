"""Failure injection: no partial writes, state remains forward-progressable."""
import pytest

from market.engine import MarketEngine, MarketError
from market.models import Side


def test_persistence_failure_on_submit_rolls_back(funded_engine, monkeypatch):
    e = funded_engine
    # Force the durable append to fail.
    monkeypatch.setattr(e.persistence, "append", lambda event: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(MarketError) as exc:
        e.submit_order("alice", Side.BUY, 100, 5, "b1")
    assert exc.value.code == "persistence_failure"
    # No order, no freeze, no journal entry.
    assert len(e.orders) == 0
    assert e.get_account("alice").balance == 1_000_000
    assert e.get_account("alice").frozen == 0
    e.check_invariant()
    # Journal has no submit line.
    import os
    with open(os.path.join(e.data_dir, "journal.log")) as f:
        lines = [l for l in f if l.strip()]
    assert not any('"type":"submit"' in l for l in lines)


def test_retry_after_persistence_failure(funded_engine, monkeypatch):
    e = funded_engine
    calls = {"n": 0}
    real_append = e.persistence.append

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        real_append(event)

    monkeypatch.setattr(e.persistence, "append", flaky)
    # First attempt fails.
    with pytest.raises(MarketError):
        e.submit_order("alice", Side.BUY, 100, 5, "b1")
    assert len(e.orders) == 0
    # Retry succeeds and produces exactly one order.
    r = e.submit_order("alice", Side.BUY, 100, 5, "b1")
    assert r["order"]["status"] == "open"
    assert len(e.orders) == 1
    assert e.get_account("alice").frozen == 500
    e.check_invariant()


def test_persistence_failure_on_cancel_rolls_back(funded_engine, monkeypatch):
    e = funded_engine
    r1 = e.submit_order("alice", Side.SELL, 100, 10, "s1")
    order_id = r1["order"]["order_id"]
    monkeypatch.setattr(e.persistence, "append", lambda event: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(MarketError) as exc:
        e.cancel_order("alice", order_id, "c1")
    assert exc.value.code == "persistence_failure"
    # Order is still active and frozen.
    order = e.get_order(order_id)
    assert order.status.value == "open"
    assert e.get_account("alice").frozen == 10
    e.check_invariant()


def test_validation_failure_leaves_no_state(funded_engine):
    e = funded_engine
    with pytest.raises(MarketError) as exc:
        e.submit_order("alice", Side.BUY, 100, 1_000_001, "b1")
    assert exc.value.code == "insufficient_balance"
    assert len(e.orders) == 0
    assert e.get_account("alice").balance == 1_000_000
    e.check_invariant()


def test_failure_then_restart_is_consistent(funded_engine, tmp_path, monkeypatch):
    e = funded_engine
    monkeypatch.setattr(e.persistence, "append", lambda event: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(MarketError):
        e.submit_order("alice", Side.BUY, 100, 5, "b1")
    # Restart: the failed order must not appear.
    e2 = MarketEngine(str(tmp_path / "data"))
    e2.load()
    assert len(e2.orders) == 0
    assert e2.get_account("alice").balance == 1_000_000
    e2.check_invariant()


def test_snapshot_failure_does_not_corrupt_state(funded_engine, monkeypatch):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    monkeypatch.setattr(e.persistence, "save_snapshot", lambda state: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        e.snapshot()
    # State is still intact and forward-progressable.
    assert len(e.orders) == 1
    e.submit_order("bob", Side.BUY, 100, 10, "b1")
    assert len(e.trades) == 1
    e.check_invariant()
