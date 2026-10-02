"""HTTP routes for the trading market."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.errors import Conflict, NotFound, Rejected
from app.models import (
    AccountView,
    CancelRequest,
    CancelResult,
    DepthView,
    Order,
    OrderPage,
    OrderSubmit,
    OrderSubmitResult,
    OrderView,
    Trade,
    TradePage,
    TradeView,
)
from app.service import get_service

router = APIRouter()


def _engine():
    return get_service().engine


def _order_view(o: Order) -> OrderView:
    return OrderView(
        order_id=o.order_id, account=o.account, symbol=o.symbol, side=o.side,
        price=o.price, orig_qty=o.orig_qty, filled_qty=o.filled_qty,
        remaining_qty=o.orig_qty - o.filled_qty, status=o.status,
        created_at=o.created_at,
    )


def _trade_view(t: Trade) -> TradeView:
    return TradeView(
        trade_id=t.trade_id, symbol=t.symbol, price=t.price, qty=t.qty,
        taker_order_id=t.taker_order_id, maker_order_id=t.maker_order_id,
        created_at=t.created_at,
    )


def _http_for_market_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, Rejected):
        status = 404 if exc.reason == "INSTRUMENT_NOT_FOUND" else 400
        return HTTPException(status_code=status,
                             detail={"reason": exc.reason,
                                     "message": str(exc)})
    raise exc


# ----------------------------------------------------------------- commands
@router.post("/orders", response_model=OrderSubmitResult)
def submit_order(req: OrderSubmit) -> OrderSubmitResult:
    try:
        result = _engine().submit(req)
    except (NotFound, Rejected, Conflict) as exc:
        raise _http_for_market_error(exc)
    result.setdefault("request_id",
                      req.client_request_id or str(result.get("order_id") or ""))
    return OrderSubmitResult(**result)


@router.post("/orders/cancel", response_model=CancelResult)
def cancel_order(req: CancelRequest) -> CancelResult:
    try:
        result = _engine().cancel(req.account, req.order_id,
                                  req.client_request_id)
    except (NotFound, Conflict) as exc:
        raise _http_for_market_error(exc)
    result.setdefault("request_id",
                      req.client_request_id or str(req.order_id))
    return CancelResult(**result)


@router.post("/accounts/{account}/deposit")
def deposit(account: str, asset: str = Query(..., min_length=1),
            amount: int = Query(..., gt=0)):
    try:
        _engine().deposit(account, asset, amount)
    except Rejected as exc:
        raise _http_for_market_error(exc)
    return {"account": account, "asset": asset, "amount": amount, "ok": True}


# ------------------------------------------------------------------ queries
@router.get("/accounts/{account}", response_model=AccountView)
def get_account(account: str) -> AccountView:
    return AccountView(**_engine().account_view(account))


@router.get("/instruments")
def list_instruments() -> dict[str, Any]:
    return {"instruments": [
        {"symbol": s, "base_asset": i.base_asset,
         "quote_asset": i.quote_asset, "lot_size": i.lot_size,
         "min_qty": i.min_qty}
        for s, i in sorted(_engine().instruments.items())
    ]}


@router.get("/depth/{symbol}", response_model=DepthView)
def get_depth(symbol: str, levels: int = Query(20, ge=1, le=200)) -> DepthView:
    try:
        return DepthView(**_engine().depth(symbol, levels))
    except Rejected as exc:
        raise _http_for_market_error(exc)


@router.get("/trades/{symbol}", response_model=TradePage)
def get_trades(symbol: str, start_time: Optional[int] = None,
               end_time: Optional[int] = None, cursor: Optional[str] = None,
               limit: int = Query(50, ge=1, le=200)) -> TradePage:
    try:
        items, nxt = _engine().trades_page(
            symbol, start_time, end_time, cursor, limit)
    except Rejected as exc:
        raise _http_for_market_error(exc)
    return TradePage(items=[_trade_view(t) for t in items], next_cursor=nxt)


@router.get("/orders", response_model=OrderPage)
def get_orders(account: Optional[str] = None, symbol: Optional[str] = None,
               start_time: Optional[int] = None, end_time: Optional[int] = None,
               cursor: Optional[str] = None, limit: int = Query(50, ge=1, le=200)
               ) -> OrderPage:
    try:
        items, nxt = _engine().orders_page(
            account, symbol, start_time, end_time, cursor, limit)
    except Rejected as exc:
        raise _http_for_market_error(exc)
    return OrderPage(items=[_order_view(o) for o in items], next_cursor=nxt)
