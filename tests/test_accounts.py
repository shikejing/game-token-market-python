"""Custody (freeze/escrow), settlement at trade price, and conservation."""
import pytest

from market.engine import MarketError
from market.models import Side


def test_freeze_on_submit(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    # Alice sells 5 tokens -> 5 frozen.
    assert e.get_account("alice").frozen == 5
    assert e.get_account("alice").balance == 1_000_000 - 5
    e.submit_order("bob", Side.BUY, 99, 5, "b1")
    # Bob buys 5 @ 99 (no cross) -> 495 frozen, rests in book.
    assert e.get_account("bob").frozen == 495
    assert e.get_account("bob").balance == 1_000_000 - 495
    e.check_invariant()


def test_settlement_at_trade_price(funded_engine):
    e = funded_engine
    # Alice sells 5 @ 100; Bob buys 5 @ 100 -> trade at 100.
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    e.submit_order("bob", Side.BUY, 100, 5, "b1")
    alice = e.get_account("alice")
    bob = e.get_account("bob")
    # Alice: froze 5, receives 500 -> balance 1_000_000 + 495, frozen 0.
    assert alice.frozen == 0
    assert alice.balance == 1_000_000 + 495
    # Bob: froze 500, spends 500, receives 5 -> balance 1_000_000 - 500 + 5.
    assert bob.frozen == 0
    assert bob.balance == 1_000_000 - 500 + 5
    e.check_invariant()


def test_price_improvement_leftover_released(funded_engine):
    e = funded_engine
    # Alice sells 5 @ 100; Bob bids 101 -> trade at 100 (maker price).
    e.submit_order("alice", Side.SELL, 100, 5, "s1")
    r = e.submit_order("bob", Side.BUY, 101, 5, "b1")
    assert r["trades"][0]["price"] == 100
    # Bob froze 505, spent 500, received 5 -> net -495, frozen 0.
    bob = e.get_account("bob")
    assert bob.frozen == 0
    assert bob.balance == 1_000_000 - 495
    e.check_invariant()


def test_partial_fill_keeps_frozen(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 3, "b1")
    # Alice: 3 sold, 7 still frozen.
    assert e.get_account("alice").frozen == 7
    assert e.get_account("alice").balance == 1_000_000 - 10 + 300
    # Bob: fully filled, no frozen.
    assert e.get_account("bob").frozen == 0
    e.check_invariant()


def test_cancel_releases_remaining_freeze(funded_engine):
    e = funded_engine
    e.submit_order("alice", Side.SELL, 100, 10, "s1")
    e.submit_order("bob", Side.BUY, 100, 4, "b1")  # fills 4 of alice's 10
    order = next(iter(e.orders.values()))
    e.cancel_order("alice", order.order_id, "c1")
    # Alice: 4 sold @100, 6 released.
    alice = e.get_account("alice")
    assert alice.frozen == 0
    assert alice.balance == 1_000_000 - 10 + 400 + 6
    e.check_invariant()


def test_insufficient_balance_rejected(funded_engine):
    e = funded_engine
    with pytest.raises(MarketError) as exc:
        e.submit_order("alice", Side.BUY, 100, 1_000_000, "b1")
    assert exc.value.code == "insufficient_balance"
    # State unchanged.
    assert e.get_account("alice").balance == 1_000_000
    assert e.get_account("alice").frozen == 0
    assert len(e.orders) == 0
    e.check_invariant()


def test_unknown_account_rejected(engine):
    with pytest.raises(MarketError) as exc:
        engine.submit_order("nobody", Side.BUY, 100, 1, "b1")
    assert exc.value.code == "account_not_found"


def test_deposit_and_open_account(engine):
    engine.open_account("carol", 500)
    assert engine.get_account("carol").balance == 500
    engine.deposit("carol", 250)
    assert engine.get_account("carol").balance == 750
    assert engine.total_supply() == 750
    with pytest.raises(MarketError) as exc:
        engine.open_account("carol", 100)
    assert exc.value.code == "account_exists"


def test_conservation_many_trades(funded_engine):
    e = funded_engine
    # Throw a sequence of trades and verify conservation throughout.
    for i in range(20):
        px = 100 + (i % 5)
        if i % 2 == 0:
            e.submit_order("alice", Side.SELL, px, 2, f"s{i}")
        else:
            e.submit_order("bob", Side.BUY, px, 2, f"b{i}")
    e.check_invariant()
    # Total supply must equal initial mint.
    assert e.total_supply() == 2_000_000
