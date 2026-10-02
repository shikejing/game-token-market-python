"""HTTP API for the game token market.

Routers are thin: request validation and serialization only; all state
transitions live in MarketEngine.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .engine import MarketEngine, MarketError
from .models import OrderStatus, Side

router = APIRouter(prefix="/api")


# ---- request models ------------------------------------------------------
class OpenAccountRequest(BaseModel):
    account_id: str = Field(min_length=1)
    initial_balance: int = Field(default=0, ge=0)


class DepositRequest(BaseModel):
    amount: int = Field(gt=0)


class SubmitOrderRequest(BaseModel):
    account_id: str = Field(min_length=1)
    side: Side
    price: int = Field(gt=0)
    quantity: int = Field(gt=0)
    client_key: Optional[str] = None


class CancelOrderRequest(BaseModel):
    account_id: str = Field(min_length=1)
    client_key: Optional[str] = None


# ---- dependency ------------------------------------------------------------
_engine: Optional[MarketEngine] = None


def set_engine(engine: MarketEngine) -> None:
    global _engine
    _engine = engine


def get_engine() -> MarketEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")
    return _engine


def _error(exc: MarketError) -> HTTPException:
    status = {
        "bad_request": 400,
        "account_exists": 409,
        "account_not_found": 404,
        "order_not_found": 404,
        "forbidden": 403,
        "insufficient_balance": 409,
        "persistence_failure": 500,
        "corrupt_event": 500,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


# ---- accounts ---------------------------------------------------------------
@router.post("/accounts")
def open_account(req: OpenAccountRequest) -> dict:
    engine = get_engine()
    try:
        account = engine.open_account(req.account_id, req.initial_balance)
    except MarketError as exc:
        raise _error(exc)
    return {"account_id": account.account_id, "balance": account.balance, "frozen": account.frozen}


@router.post("/accounts/{account_id}/deposit")
def deposit(account_id: str, req: DepositRequest) -> dict:
    engine = get_engine()
    try:
        account = engine.deposit(account_id, req.amount)
    except MarketError as exc:
        raise _error(exc)
    return {"account_id": account.account_id, "balance": account.balance, "frozen": account.frozen}


@router.get("/accounts/{account_id}")
def get_account(account_id: str) -> dict:
    engine = get_engine()
    account = engine.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail={"code": "account_not_found"})
    return {"account_id": account.account_id, "balance": account.balance, "frozen": account.frozen}


# ---- orders ------------------------------------------------------------------
@router.post("/orders")
def submit_order(
    req: SubmitOrderRequest,
    idempotency_key: Optional[str] = Header(default=None),
) -> dict:
    engine = get_engine()
    client_key = req.client_key or idempotency_key
    try:
        return engine.submit_order(req.account_id, req.side, req.price, req.quantity, client_key)
    except MarketError as exc:
        raise _error(exc)


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    req: CancelOrderRequest,
    idempotency_key: Optional[str] = Header(default=None),
) -> dict:
    engine = get_engine()
    client_key = req.client_key or idempotency_key
    try:
        return engine.cancel_order(req.account_id, order_id, client_key)
    except MarketError as exc:
        raise _error(exc)


@router.get("/orders")
def list_orders(
    account_id: Optional[str] = Query(default=None),
    status: Optional[OrderStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: Optional[int] = Query(default=None),
) -> dict:
    engine = get_engine()
    return engine.list_orders(account_id=account_id, status=status, limit=limit, cursor=cursor)


@router.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    engine = get_engine()
    order = engine.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail={"code": "order_not_found"})
    return engine._order_to_dict(order)


# ---- market data ---------------------------------------------------------------
@router.get("/trades")
def list_trades(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: Optional[int] = Query(default=None),
    start_ts: Optional[float] = Query(default=None),
    end_ts: Optional[float] = Query(default=None),
) -> dict:
    engine = get_engine()
    return engine.list_trades(limit=limit, cursor=cursor, start_ts=start_ts, end_ts=end_ts)


@router.get("/depth")
def get_depth(levels: int = Query(default=10, ge=1, le=50)) -> dict:
    engine = get_engine()
    return engine.depth(levels=levels)
