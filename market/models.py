"""Core data types for the game token market.

All monetary amounts and quantities are integers (smallest unit, "ticks")
to avoid floating point rounding errors.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass
from typing import Optional


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, enum.Enum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class Account:
    """A token account.

    balance: tokens available for new orders / withdrawal.
    frozen: tokens escrowed by open orders (cannot be spent).
    """

    account_id: str
    balance: int = 0
    frozen: int = 0

    @property
    def total(self) -> int:
        return self.balance + self.frozen


@dataclass
class Order:
    order_id: str
    account_id: str
    side: Side
    price: int
    quantity: int
    remaining: int
    status: OrderStatus
    created_at: float
    seq: int
    client_key: Optional[str] = None
    # Cumulative filled quantity (in lots) and cumulative quote amount
    # (price * quantity summed over fills) at the actual trade prices.
    traded_qty: int = 0
    traded_amount: int = 0

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


@dataclass
class Trade:
    trade_id: str
    price: int
    quantity: int
    buy_order_id: str
    sell_order_id: str
    buy_account_id: str
    sell_account_id: str
    timestamp: float
    seq: int


@dataclass
class DepthLevel:
    price: int
    quantity: int
    order_count: int


def new_order_id() -> str:
    return "o-" + uuid.uuid4().hex


def new_trade_id(trade_seq: int) -> str:
    return f"t{trade_seq}"


def now_ts() -> float:
    return time.time()
