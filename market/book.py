"""Price-time priority order book and matching engine.

Matching rules:
- Buy order matches asks (sell orders) with ask price <= limit price,
  lowest ask first; at the same price level, earliest order first.
- Sell order matches bids (buy orders) with bid price >= limit price,
  highest bid first; at the same price level, earliest order first.
- Trade price is the resting (maker) order's price.
- Partial fills are allowed; the unfilled remainder rests in the book.
- Fully filled orders are removed from the book.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import DepthLevel, Order, OrderStatus, Side, Trade, new_trade_id, now_ts


class OrderBook:
    def __init__(self) -> None:
        # price -> list of orders, in arrival (time) order.
        self._bids: Dict[int, List[Order]] = {}
        self._asks: Dict[int, List[Order]] = {}
        # order_id -> (side, price) for O(1) lookup on cancel.
        self._index: Dict[str, Tuple[Side, int]] = {}

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _activate(order: Order) -> None:
        if order.remaining == order.quantity:
            order.status = OrderStatus.OPEN
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

    def _pop_level(self, side: Side, price: int) -> None:
        book = self._bids if side == Side.BUY else self._asks
        level = book.get(price)
        if level is not None and not level:
            del book[price]

    # ---- lifecycle -----------------------------------------------------
    def add(self, order: Order) -> None:
        """Add a fresh, unfilled order to the book."""
        if order.order_id in self._index:
            return
        self._activate(order)
        book = self._bids if order.side == Side.BUY else self._asks
        book.setdefault(order.price, []).append(order)
        self._index[order.order_id] = (order.side, order.price)

    def remove(self, order: Order) -> bool:
        """Remove an order from the book (cancel). Returns True if present."""
        loc = self._index.pop(order.order_id, None)
        if loc is None:
            return False
        side, price = loc
        book = self._bids if side == Side.BUY else self._asks
        level = book.get(price)
        if level is None:
            return False
        for i, resting in enumerate(level):
            if resting.order_id == order.order_id:
                del level[i]
                break
        self._pop_level(side, price)
        return True

    # ---- matching ------------------------------------------------------
    def match(self, incoming: Order, trade_seq_start: int) -> Tuple[List[Trade], int]:
        """Match an incoming order against the book.

        Mutates resting orders and the book. Returns (trades, next_trade_seq).
        Settlement (balance/frozen moves) is the engine's responsibility;
        this method only mutates order quantities/status and book levels.
        """
        trades: List[Trade] = []
        trade_seq = trade_seq_start

        if incoming.side == Side.BUY:
            # Match asks from the lowest price upward, while ask <= limit.
            while incoming.remaining > 0 and self._asks:
                best_ask = min(self._asks)
                if best_ask > incoming.price:
                    break
                level = self._asks[best_ask]
                resting = level[0]
                qty = min(incoming.remaining, resting.remaining)
                trades.append(
                    Trade(
                        trade_id=new_trade_id(trade_seq),
                        price=best_ask,
                        quantity=qty,
                        buy_order_id=incoming.order_id,
                        sell_order_id=resting.order_id,
                        buy_account_id=incoming.account_id,
                        sell_account_id=resting.account_id,
                        timestamp=now_ts(),
                        seq=trade_seq,
                    )
                )
                trade_seq += 1
                incoming.remaining -= qty
                incoming.traded_qty += qty
                incoming.traded_amount += best_ask * qty
                resting.remaining -= qty
                resting.traded_qty += qty
                resting.traded_amount += best_ask * qty
                if resting.remaining == 0:
                    resting.status = OrderStatus.FILLED
                    level.pop(0)
                    self._index.pop(resting.order_id, None)
                    self._pop_level(Side.SELL, best_ask)
                else:
                    resting.status = OrderStatus.PARTIALLY_FILLED
            if incoming.remaining == 0:
                incoming.status = OrderStatus.FILLED
            else:
                self.add(incoming)
        else:
            # Match bids from the highest price downward, while bid >= limit.
            while incoming.remaining > 0 and self._bids:
                best_bid = max(self._bids)
                if best_bid < incoming.price:
                    break
                level = self._bids[best_bid]
                resting = level[0]
                qty = min(incoming.remaining, resting.remaining)
                trades.append(
                    Trade(
                        trade_id=new_trade_id(trade_seq),
                        price=best_bid,
                        quantity=qty,
                        buy_order_id=resting.order_id,
                        sell_order_id=incoming.order_id,
                        buy_account_id=resting.account_id,
                        sell_account_id=incoming.account_id,
                        timestamp=now_ts(),
                        seq=trade_seq,
                    )
                )
                trade_seq += 1
                incoming.remaining -= qty
                incoming.traded_qty += qty
                incoming.traded_amount += best_bid * qty
                resting.remaining -= qty
                resting.traded_qty += qty
                resting.traded_amount += best_bid * qty
                if resting.remaining == 0:
                    resting.status = OrderStatus.FILLED
                    level.pop(0)
                    self._index.pop(resting.order_id, None)
                    self._pop_level(Side.BUY, best_bid)
                else:
                    resting.status = OrderStatus.PARTIALLY_FILLED
            if incoming.remaining == 0:
                incoming.status = OrderStatus.FILLED
            else:
                self.add(incoming)

        return trades, trade_seq

    # ---- queries -------------------------------------------------------
    def best_bid(self) -> Optional[int]:
        return max(self._bids) if self._bids else None

    def best_ask(self) -> Optional[int]:
        return min(self._asks) if self._asks else None

    def depth(self, levels: int = 10) -> Tuple[List[DepthLevel], List[DepthLevel]]:
        bids: List[DepthLevel] = []
        for price in sorted(self._bids, reverse=True)[:levels]:
            level = self._bids[price]
            bids.append(
                DepthLevel(
                    price=price,
                    quantity=sum(o.remaining for o in level),
                    order_count=len(level),
                )
            )
        asks: List[DepthLevel] = []
        for price in sorted(self._asks)[:levels]:
            level = self._asks[price]
            asks.append(
                DepthLevel(
                    price=price,
                    quantity=sum(o.remaining for o in level),
                    order_count=len(level),
                )
            )
        return bids, asks

    def has_order(self, order_id: str) -> bool:
        return order_id in self._index
