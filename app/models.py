"""Domain model and API schema definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class RejectReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INVALID_REQUEST = "INVALID_REQUEST"
    INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"


@dataclass
class Instrument:
    symbol: str
    base_asset: str
    quote_asset: str
    lot_size: int = 1
    min_qty: int = 1


@dataclass
class Order:
    order_id: int
    account: str
    symbol: str
    side: Side
    price: int
    orig_qty: int
    filled_qty: int = 0
    status: OrderStatus = OrderStatus.OPEN
    created_at: int = 0
    client_request_id: Optional[str] = None


@dataclass
class Trade:
    trade_id: int
    symbol: str
    price: int
    qty: int
    taker_order_id: int
    maker_order_id: int
    taker_account: str
    maker_account: str
    taker_side: Side
    created_at: int


@dataclass
class AccountState:
    available: dict[str, int] = field(default_factory=dict)
    frozen: dict[str, int] = field(default_factory=dict)


# ---------- API schemas ----------

class OrderSubmit(BaseModel):
    account: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    price: int = Field(gt=0)
    qty: int = Field(gt=0)
    client_request_id: Optional[str] = Field(default=None, min_length=1)


class OrderSubmitResult(BaseModel):
    request_id: str
    order_id: Optional[int] = None
    accepted: bool
    status: Optional[OrderStatus] = None
    filled_qty: int = 0
    remaining_qty: int = 0
    trades: list[dict] = []
    reject_reason: Optional[RejectReason] = None
    duplicate: bool = False


class CancelRequest(BaseModel):
    account: str = Field(min_length=1)
    order_id: int = Field(gt=0)
    client_request_id: Optional[str] = Field(default=None, min_length=1)


class CancelResult(BaseModel):
    request_id: str
    order_id: int
    cancelled_qty: int
    status: OrderStatus
    duplicate: bool = False


class BalanceView(BaseModel):
    asset: str
    available: int
    frozen: int
    total: int


class AccountView(BaseModel):
    account: str
    balances: list[BalanceView]


class OrderView(BaseModel):
    order_id: int
    account: str
    symbol: str
    side: Side
    price: int
    orig_qty: int
    filled_qty: int
    remaining_qty: int
    status: OrderStatus
    created_at: int


class TradeView(BaseModel):
    trade_id: int
    symbol: str
    price: int
    qty: int
    taker_order_id: int
    maker_order_id: int
    created_at: int


class DepthLevel(BaseModel):
    price: int
    qty: int


class DepthView(BaseModel):
    symbol: str
    bids: list[DepthLevel]
    asks: list[DepthLevel]


class TradePage(BaseModel):
    items: list[TradeView]
    next_cursor: Optional[str] = None


class OrderPage(BaseModel):
    items: list[OrderView]
    next_cursor: Optional[str] = None
