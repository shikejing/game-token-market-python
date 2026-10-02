"""Concurrency: global + per-account concurrent submit/cancel must be serializable."""
import threading

from market.models import Side


def test_concurrent_submits_conserve(funded_engine):
    e = funded_engine
    errors = []

    def worker(acct, side, n):
        try:
            for i in range(n):
                e.submit_order(acct, side, 100 + (i % 3), 1, f"{acct}-{side}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("alice", Side.SELL, 50)),
        threading.Thread(target=worker, args=("bob", Side.BUY, 50)),
        threading.Thread(target=worker, args=("alice", Side.BUY, 30)),
        threading.Thread(target=worker, args=("bob", Side.SELL, 30)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    e.check_invariant()
    # Every order is either open, partially filled, filled, or cancelled.
    for o in e.orders.values():
        assert o.status.value in ("open", "partially_filled", "filled", "cancelled")
    # No negative balances anywhere.
    for a in e.accounts.values():
        assert a.balance >= 0
        assert a.frozen >= 0


def test_concurrent_submit_and_cancel(funded_engine):
    e = funded_engine
    # Seed some resting orders.
    for i in range(20):
        e.submit_order("alice", Side.SELL, 100, 1, f"seed-{i}")
    errors = []
    stop = threading.Event()

    def submitter():
        try:
            i = 0
            while not stop.is_set():
                e.submit_order("bob", Side.BUY, 100, 1, f"buy-{i}")
                i += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def canceller():
        try:
            for oid in list(e.orders.keys()):
                o = e.orders[oid]
                if o.account_id == "alice" and o.is_active:
                    e.cancel_order("alice", oid, f"cancel-{oid}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=submitter)
    t2 = threading.Thread(target=canceller)
    t1.start()
    t2.start()
    t2.join()
    stop.set()
    t1.join()

    assert not errors, errors
    e.check_invariant()


def test_no_double_settlement(funded_engine):
    """A single order must never settle twice (trade count consistency)."""
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    errors = []

    def buyer(n):
        try:
            for i in range(n):
                e.submit_order("bob", Side.BUY, 100, 1, f"b-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=buyer, args=(10,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # Exactly 10 shares traded (the sell order's full quantity), no more.
    total_qty = sum(t.quantity for t in e.trades)
    assert total_qty == 10
    e.check_invariant()


def test_concurrent_deposits_conserve(engine):
    engine.open_account("alice", 0)
    errors = []

    def depositor(n):
        try:
            for i in range(n):
                engine.deposit("alice", 10)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=depositor, args=(100,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert engine.get_account("alice").balance == 8000
    assert engine.total_supply() == 8000
    engine.check_invariant()
