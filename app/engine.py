"""In-memory order book + ledger with a single serialization lock.

Every mutating command is executed in three phases while holding the lock:

1. *simulate*: read the current state and produce an event batch without
   mutating anything;
2. *persist*: append the whole batch to the WAL (all-or-nothing);
3. *apply*: fold the events into the in-memory state.

If persistence fails, nothing was mutated, so the command is simply rejected
and the system keeps running. Idempotent responses are themselves events, so
duplicate requests are served from durable state after a restart.
"""
from __future__ import annotations

import base64
import heapq
import json
import threading
import time
from typing import Any, Optional

from app.errors import Conflict, NotFound, Rejected, StorageError
from app.models import (
    AccountState,
    Instrument,
    Order,
    OrderStatus,
    OrderSubmit,
    Side,
    Trade,
)

ACTIVE_STATUSES = {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}


class CrashAfterDurableCommit(StorageError):
    """Test-only: events are durable but were never applied in memory."""


def _account(accounts: dict[str, AccountState], name: str) -> AccountState:
    acc = accounts.get(name)
    if acc is None:
        acc = AccountState()
        accounts[name] = acc
    return acc


class MatchingEngine:
    def __init__(self, store: Optional[Any] = None) -> None:
        self.store = store
        self._lock = threading.RLock()
        self.instruments: dict[str, Instrument] = {}
        self.accounts: dict[str, AccountState] = {}
        self.orders: dict[int, Order] = {}
        self.bids: dict[str, list[tuple]] = {}
        self.asks: dict[str, list[tuple]] = {}
        self.trades_by_symbol: dict[str, list[Trade]] = {}
        self.all_trades: list[Trade] = []
        # (kind, account, client_request_id) -> response payload
        self.idem: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.next_order_id = 1
        self.next_trade_id = 1
        self.clock = 0

    # =============================================================== helpers
    def _tick(self) -> int:
        # wall-clock nanoseconds, forced strictly monotonic
        value = max(time.time_ns(), self.clock + 1)
        self.clock = value
        return value

    def _instrument(self, symbol: str) -> Instrument:
        inst = self.instruments.get(symbol)
        if inst is None:
            raise Rejected("INSTRUMENT_NOT_FOUND", f"unknown symbol {symbol}")
        return inst

    @staticmethod
    def _order_remaining(o: Order) -> int:
        return o.orig_qty - o.filled_qty

    def _heaps(self, symbol: str):
        return (self.bids.setdefault(symbol, []),
                self.asks.setdefault(symbol, []))

    def _cleanup_top(self, heap: list) -> None:
        while heap:
            _, _, oid = heap[0]
            o = self.orders.get(oid)
            if o is None or o.status not in ACTIVE_STATUSES or \
                    o.filled_qty >= o.orig_qty:
                heapq.heappop(heap)
            else:
                break

    # ------------------------------------------------------------- mutation
    def add_instrument(self, symbol: str, base_asset: str, quote_asset: str,
                       lot_size: int = 1, min_qty: int = 1) -> None:
        with self._lock:
            if symbol in self.instruments:
                return
            event = {"type": "instrument", "symbol": symbol,
                     "base_asset": base_asset, "quote_asset": quote_asset,
                     "lot_size": lot_size, "min_qty": min_qty}
            self._persist_and_apply([event])

    def deposit(self, account: str, asset: str, amount: int) -> None:
        if amount <= 0:
            raise Rejected("INVALID_REQUEST", "amount must be positive")
        with self._lock:
            event = {"type": "deposit", "account": account, "asset": asset,
                     "amount": amount, "clock": self._tick()}
            self._persist_and_apply([event])

    def submit(self, req: OrderSubmit) -> dict[str, Any]:
        with self._lock:
            key = ("submit", req.account, req.client_request_id)
            if req.client_request_id is not None and key in self.idem:
                return {**self.idem[key], "duplicate": True}

            inst = self._instrument(req.symbol)
            if req.qty < inst.min_qty or req.qty % inst.lot_size != 0:
                result = {"order_id": None, "accepted": False,
                          "status": None, "filled_qty": 0,
                          "remaining_qty": 0, "trades": [],
                          "reject_reason": "INVALID_REQUEST"}
                if req.client_request_id is not None:
                    self._persist_and_apply(
                        [self._record_event(key, result)])
                return {**result, "duplicate": False,
                        "request_id": req.client_request_id or ""}

            acc = _account(self.accounts, req.account)
            if req.side == Side.BUY:
                need_asset, need_amount = inst.quote_asset, req.price * req.qty
            else:
                need_asset, need_amount = inst.base_asset, req.qty
            if acc.available.get(need_asset, 0) < need_amount:
                result = {"order_id": None, "accepted": False,
                          "status": None, "filled_qty": 0,
                          "remaining_qty": 0, "trades": [],
                          "reject_reason": "INSUFFICIENT_FUNDS"}
                if req.client_request_id is not None:
                    self._persist_and_apply(
                        [self._record_event(key, result)])
                return {**result, "duplicate": False,
                        "request_id": req.client_request_id or ""}

            order_id = self.next_order_id
            order_clock = self._tick()
            sim_trades = self._simulate(inst, req, order_clock)

            events: list[dict[str, Any]] = [{
                "type": "order", "order_id": order_id,
                "account": req.account, "symbol": req.symbol,
                "side": req.side.value, "price": req.price,
                "qty": req.qty, "client_request_id": req.client_request_id,
                "clock": order_clock,
            }]
            remaining = req.qty
            trade_views: list[dict[str, Any]] = []
            for maker_id, price, qty, maker_clock in sim_trades:
                tid = self.next_trade_id
                self.next_trade_id += 1
                self.clock = maker_clock
                events.append({"type": "trade", "trade_id": tid,
                               "symbol": req.symbol, "price": price,
                               "qty": qty, "taker_order_id": order_id,
                               "maker_order_id": maker_id,
                               "taker_side": req.side.value,
                               "clock": maker_clock})
                trade_views.append({"trade_id": tid, "price": price,
                                    "qty": qty,
                                    "maker_order_id": maker_id,
                                    "taker_order_id": order_id})
                remaining -= qty
            filled = req.qty - remaining
            if remaining > 0:
                status = (OrderStatus.OPEN.value if filled == 0
                          else OrderStatus.PARTIALLY_FILLED.value)
            else:
                status = OrderStatus.FILLED.value
            result = {"order_id": order_id, "accepted": True,
                      "status": status, "filled_qty": filled,
                      "remaining_qty": remaining, "trades": trade_views,
                      "reject_reason": None}
            if req.client_request_id is not None:
                events.append(self._record_event(key, result))

            self._persist_and_apply(events)
            self.next_order_id = order_id + 1
            return {**result, "duplicate": False,
                    "request_id": req.client_request_id or str(order_id)}

    def _simulate(self, inst: Instrument, req: OrderSubmit,
                  order_clock: int) -> list[tuple[int, int, int, int]]:
        """Pure matching simulation; reads live orders but never mutates state.

        Active resting orders are snapshotted and sorted by strict price/time
        priority (order_id is the final tie-breaker, equal to arrival order).
        """
        candidates = [o for o in self._live_orders(req.symbol)
                      if o.side != req.side]
        if req.side == Side.BUY:
            candidates = [o for o in candidates if o.price <= req.price]
            # taker BUY crosses resting SELLs: lowest ask first, then earliest
            candidates.sort(key=lambda o: (o.price, o.order_id))
        else:
            candidates = [o for o in candidates if o.price >= req.price]
            # taker SELL crosses resting BUYs: highest bid first, then earliest
            candidates.sort(key=lambda o: (-o.price, o.order_id))
        remaining = req.qty
        out: list[tuple[int, int, int, int]] = []
        clock = order_clock
        for maker in candidates:
            if remaining <= 0:
                break
            clock += 1
            qty = min(remaining, self._order_remaining(maker))
            out.append((maker.order_id, maker.price, qty, clock))
            remaining -= qty
        return out

    def cancel(self, account: str, order_id: int,
               client_request_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            key = ("cancel", account, client_request_id)
            if client_request_id is not None and key in self.idem:
                return {**self.idem[key], "duplicate": True}
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(f"order {order_id} not found")
            if order.account != account:
                raise NotFound(f"order {order_id} not found")
            if order.status not in ACTIVE_STATUSES or \
                    order.filled_qty >= order.orig_qty:
                raise Conflict(f"order {order_id} is not active")
            remaining = self._order_remaining(order)
            clock = self._tick()
            result = {"order_id": order_id, "cancelled_qty": remaining,
                      "status": OrderStatus.CANCELLED.value}
            events: list[dict[str, Any]] = [{
                "type": "cancel", "order_id": order_id, "account": account,
                "cancelled_qty": remaining, "clock": clock,
            }]
            if client_request_id is not None:
                events.append(self._record_event(key, result))
            self._persist_and_apply(events)
            return {**result, "duplicate": False,
                    "request_id": client_request_id or str(order_id)}

    def _record_event(self, key: tuple, payload: dict[str, Any]) -> dict:
        return {"type": "request_record", "kind": key[0],
                "account": key[1], "client_request_id": key[2],
                "payload": payload, "clock": self._tick()}

    # ------------------------------------------------------------- pipeline
    def _persist_and_apply(self, events: list[dict[str, Any]]) -> None:
        if self.store is not None:
            self.store.append(events)
            if getattr(self.store, "simulate_crash_after_append", False):
                raise CrashAfterDurableCommit("crash injected after durable "
                                              "append, before in-memory apply")
        for event in events:
            self._apply(event)

    # ============================================================== queries
    def get_order(self, order_id: int) -> Order:
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(f"order {order_id} not found")
            return order

    def account_view(self, account: str) -> dict[str, Any]:
        with self._lock:
            acc = self.accounts.get(account, AccountState())
            assets = set(acc.available) | set(acc.frozen)
            return {
                "account": account,
                "balances": [
                    {"asset": a, "available": acc.available.get(a, 0),
                     "frozen": acc.frozen.get(a, 0),
                     "total": acc.available.get(a, 0)
                              + acc.frozen.get(a, 0)}
                    for a in sorted(assets)
                ],
            }

    def _live_orders(self, symbol: str) -> list[Order]:
        return [o for o in self.orders.values()
                if o.symbol == symbol and o.status in ACTIVE_STATUSES
                and o.filled_qty < o.orig_qty]

    def depth(self, symbol: str, levels: int = 20) -> dict[str, Any]:
        with self._lock:
            self._instrument(symbol)
            bids_map: dict[int, int] = {}
            asks_map: dict[int, int] = {}
            for o in self._live_orders(symbol):
                rem = self._order_remaining(o)
                if o.side == Side.BUY:
                    bids_map[o.price] = bids_map.get(o.price, 0) + rem
                else:
                    asks_map[o.price] = asks_map.get(o.price, 0) + rem
            bids = [{"price": p, "qty": bids_map[p]}
                    for p in sorted(bids_map, reverse=True)[:levels]]
            asks = [{"price": p, "qty": asks_map[p]}
                    for p in sorted(asks_map)[:levels]]
            return {"symbol": symbol, "bids": bids, "asks": asks}

    def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        with self._lock:
            self._instrument(symbol)
            return self.trades_by_symbol.get(symbol, [])[-limit:]

    def open_orders(self, account: Optional[str] = None,
                    symbol: Optional[str] = None) -> list[Order]:
        with self._lock:
            out = []
            for o in self.orders.values():
                if o.status not in ACTIVE_STATUSES or \
                        o.filled_qty >= o.orig_qty:
                    continue
                if account is not None and o.account != account:
                    continue
                if symbol is not None and o.symbol != symbol:
                    continue
                out.append(o)
            out.sort(key=lambda o: o.order_id)
            return out

    def trades_page(self, symbol: Optional[str], start_time: Optional[int],
                    end_time: Optional[int], cursor: Optional[str],
                    limit: int) -> tuple[list[Trade], Optional[str]]:
        with self._lock:
            after = self._decode_cursor(cursor)
            items: list[Trade] = []
            for t in self.all_trades:
                if symbol is not None and t.symbol != symbol:
                    continue
                if start_time is not None and t.created_at < start_time:
                    continue
                if end_time is not None and t.created_at > end_time:
                    continue
                if t.trade_id <= after:
                    continue
                items.append(t)
            items.sort(key=lambda t: t.trade_id)
            page = items[:limit]
            nxt = None
            if len(items) > limit and page:
                nxt = self._encode_cursor(page[-1].trade_id)
            return page, nxt

    def orders_page(self, account: Optional[str], symbol: Optional[str],
                    start_time: Optional[int], end_time: Optional[int],
                    cursor: Optional[str],
                    limit: int) -> tuple[list[Order], Optional[str]]:
        with self._lock:
            after = self._decode_cursor(cursor)
            items = []
            for o in self.orders.values():
                if o.status not in ACTIVE_STATUSES or \
                        o.filled_qty >= o.orig_qty:
                    continue
                if account is not None and o.account != account:
                    continue
                if symbol is not None and o.symbol != symbol:
                    continue
                if start_time is not None and o.created_at < start_time:
                    continue
                if end_time is not None and o.created_at > end_time:
                    continue
                if o.order_id <= after:
                    continue
                items.append(o)
            items.sort(key=lambda o: o.order_id)
            page = items[:limit]
            nxt = None
            if len(items) > limit and page:
                nxt = self._encode_cursor(page[-1].order_id)
            return page, nxt

    @staticmethod
    def _encode_cursor(after_id: int) -> str:
        raw = json.dumps({"after": after_id}).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: Optional[str]) -> int:
        if not cursor:
            return 0
        try:
            pad = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(cursor + pad)
            value = json.loads(raw.decode("utf-8"))
            after = int(value["after"])
            if after < 0:
                raise ValueError
            return after
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise Rejected("INVALID_REQUEST", "bad cursor") from exc

    # ================================================================ apply
    def _apply(self, event: dict[str, Any]) -> None:
        etype = event["type"]
        if etype == "instrument":
            self.instruments[event["symbol"]] = Instrument(
                symbol=event["symbol"], base_asset=event["base_asset"],
                quote_asset=event["quote_asset"],
                lot_size=event.get("lot_size", 1),
                min_qty=event.get("min_qty", 1))
        elif etype == "deposit":
            acc = _account(self.accounts, event["account"])
            acc.available[event["asset"]] = (
                acc.available.get(event["asset"], 0) + event["amount"])
            self.clock = max(self.clock, event["clock"])
        elif etype == "order":
            self._apply_order(event)
        elif etype == "trade":
            self._apply_trade(event)
        elif etype == "cancel":
            self._apply_cancel(event)
        elif etype == "request_record":
            self.idem[(event["kind"], event["account"],
                       event["client_request_id"])] = event["payload"]
            self.clock = max(self.clock, event["clock"])
        else:
            raise ValueError(f"unknown event type {etype!r}")

    def _apply_order(self, e: dict[str, Any]) -> None:
        side = Side(e["side"])
        order = Order(order_id=e["order_id"], account=e["account"],
                      symbol=e["symbol"], side=side, price=e["price"],
                      orig_qty=e["qty"], filled_qty=0,
                      status=OrderStatus.OPEN, created_at=e["clock"],
                      client_request_id=e.get("client_request_id"))
        self.orders[order.order_id] = order
        inst = self.instruments[e["symbol"]]
        acc = _account(self.accounts, order.account)
        if side == Side.BUY:
            asset, amount = inst.quote_asset, order.price * order.orig_qty
            bids, _ = self._heaps(order.symbol)
            heapq.heappush(bids, (-order.price, order.created_at, order.order_id))
        else:
            asset, amount = inst.base_asset, order.orig_qty
            _, asks = self._heaps(order.symbol)
            heapq.heappush(asks, (order.price, order.created_at, order.order_id))
        acc.available[asset] = acc.available.get(asset, 0) - amount
        acc.frozen[asset] = acc.frozen.get(asset, 0) + amount
        self.clock = max(self.clock, order.created_at)
        self.next_order_id = max(self.next_order_id, order.order_id + 1)

    def _apply_trade(self, e: dict[str, Any]) -> None:
        taker = self.orders[e["taker_order_id"]]
        maker = self.orders[e["maker_order_id"]]
        price, qty = e["price"], e["qty"]
        inst = self.instruments[e["symbol"]]
        taker_acc = _account(self.accounts, taker.account)
        maker_acc = _account(self.accounts, maker.account)

        if taker.side == Side.BUY:
            # taker buys base with quote; maker is a seller. The taker froze
            # limit*qty of quote; the trade costs price*qty, so the price
            # improvement (limit-price)*qty is released back to available.
            taker_acc.frozen[inst.quote_asset] -= taker.price * qty
            taker_acc.available[inst.quote_asset] = (
                taker_acc.available.get(inst.quote_asset, 0)
                + (taker.price - price) * qty)
            taker_acc.available[inst.base_asset] = (
                taker_acc.available.get(inst.base_asset, 0) + qty)
            maker_acc.frozen[inst.base_asset] -= qty
            maker_acc.available[inst.quote_asset] = (
                maker_acc.available.get(inst.quote_asset, 0) + price * qty)
        else:
            # taker sells base for quote; maker is a buyer
            taker_acc.frozen[inst.base_asset] -= qty
            taker_acc.available[inst.quote_asset] = (
                taker_acc.available.get(inst.quote_asset, 0) + price * qty)
            maker_acc.frozen[inst.quote_asset] -= price * qty
            maker_acc.available[inst.base_asset] = (
                maker_acc.available.get(inst.base_asset, 0) + qty)

        for o in (taker, maker):
            o.filled_qty += qty
            if o.filled_qty >= o.orig_qty:
                o.status = OrderStatus.FILLED
            elif o.filled_qty > 0:
                o.status = OrderStatus.PARTIALLY_FILLED

        trade = Trade(
            trade_id=e["trade_id"], symbol=e["symbol"], price=price, qty=qty,
            taker_order_id=taker.order_id, maker_order_id=maker.order_id,
            taker_account=taker.account, maker_account=maker.account,
            taker_side=Side(e["taker_side"]), created_at=e["clock"])
        self.all_trades.append(trade)
        self.trades_by_symbol.setdefault(e["symbol"], []).append(trade)
        self.clock = max(self.clock, e["clock"])
        self.next_trade_id = max(self.next_trade_id, e["trade_id"] + 1)

    def _apply_cancel(self, e: dict[str, Any]) -> None:
        order = self.orders[e["order_id"]]
        remaining = order.orig_qty - order.filled_qty
        if remaining != e["cancelled_qty"]:
            # replay consistency guard
            raise RuntimeError("cancel event does not match order state")
        inst = self.instruments[order.symbol]
        acc = _account(self.accounts, order.account)
        if order.side == Side.BUY:
            asset = inst.quote_asset
            acc.frozen[asset] -= remaining * order.price
            acc.available[asset] += remaining * order.price
        else:
            asset = inst.base_asset
            acc.frozen[asset] -= remaining
            acc.available[asset] += remaining
        order.status = OrderStatus.CANCELLED
        self.clock = max(self.clock, e["clock"])

    # ============================================================== restore
    def restore(self) -> None:
        if self.store is None:
            return
        with self._lock:
            for event in self.store.read_all():
                self._apply(event)
