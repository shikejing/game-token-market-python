"""Cancel request idempotency."""
from app.errors import Conflict
from app.models import OrderStatus, OrderSubmit, Side

SYMBOL = "GEM-GOLD"


def give(engine, account, asset, amount):
    engine.deposit(account, asset, amount)


def test_duplicate_cancel_request_id(engine):
    give(engine, "a", "GEM", 100)
    r = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=100,
                                  client_request_id="wall"))
    first = engine.cancel("a", r["order_id"], client_request_id="cancel-1")
    second = engine.cancel("a", r["order_id"], client_request_id="cancel-1")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["cancelled_qty"] == first["cancelled_qty"] == 100
    assert second["status"] == OrderStatus.CANCELLED.value
    view = engine.account_view("a")
    assert view["balances"][0]["frozen"] == 0
    assert view["balances"][0]["available"] == 100


def test_distinct_cancel_requests_second_conflicts(engine):
    give(engine, "a", "GEM", 100)
    r = engine.submit(OrderSubmit(account="a", symbol=SYMBOL,
                                  side=Side.SELL, price=5, qty=100))
    engine.cancel("a", r["order_id"], client_request_id="c1")
    try:
        engine.cancel("a", r["order_id"], client_request_id="c2")
        raise AssertionError("second distinct cancel must conflict")
    except Conflict:
        pass
