"""Trading engine: serializable, idempotent, self-recovering.

Concurrency model
-----------------
A single re-entrant lock guards every state mutation. All mutations are
therefore equivalent to some serial execution (linearizable). Reads take
the same lock and return point-in-time snapshots.

Custody model (single token, integer amounts)
---------------------------------------------
- Buy submit: freeze price * quantity tokens (balance -> frozen).
- Sell submit: freeze quantity tokens.
- Trade at price p, quantity q:
    buyer:  frozen -= p*q, balance += q      (receives q tokens)
    seller: frozen -= q,   balance += p*q    (receives p*q tokens)
- Order close (fully filled / cancelled): release any leftover frozen
  amount back to the order's account balance.
Total tokens across all accounts (balance + frozen) is conserved.

Commit protocol
---------------
1. Validate (no mutation).
2. Mutate in-memory state under the engine lock.
3. Append the event to the durable journal (WAL commit point).
   If the append fails, roll back the in-memory mutation and raise;
   no partial state is left behind and the client may retry safely.
4. Cache the idempotent response only after a successful commit.

Recovery
--------
On load(): latest snapshot is restored, active orders are rebuilt into
the book, and journal events after the snapshot are replayed in order.
Replay is deterministic (price-time matching + integer settlement), so
recovered state equals the state at the moment of the last commit.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .book import OrderBook
from .models import (
    Account,
    Order,
    OrderStatus,
    Side,
    Trade,
    new_order_id,
    now_ts,
)
from .persistence import Persistence


class MarketError(Exception):
    """Business error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MarketEngine:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.persistence = Persistence(data_dir)
        self._lock = threading.RLock()
        self.accounts: Dict[str, Account] = {}
        self.orders: Dict[str, Order] = {}
        self.trades: List[Trade] = []
        self.idempotency: Dict[str, Dict[str, Any]] = {}
        self.book = OrderBook()
        self._seq = 0          # next event / order sequence number
        self._trade_seq = 0    # next trade sequence number
        self._minted = 0       # total tokens ever issued (conservation check)
        self._loaded = False

    # ---- serialization helpers ------------------------------------------
    @staticmethod
    def _order_to_dict(o: Order) -> Dict[str, Any]:
        return {
            "order_id": o.order_id,
            "account_id": o.account_id,
            "side": o.side.value,
            "price": o.price,
            "quantity": o.quantity,
            "remaining": o.remaining,
            "status": o.status.value,
            "created_at": o.created_at,
            "seq": o.seq,
            "client_key": o.client_key,
            "traded_qty": o.traded_qty,
            "traded_amount": o.traded_amount,
        }

    @staticmethod
    def _order_from_dict(d: Dict[str, Any]) -> Order:
        return Order(
            order_id=d["order_id"],
            account_id=d["account_id"],
            side=Side(d["side"]),
            price=int(d["price"]),
            quantity=int(d["quantity"]),
            remaining=int(d["remaining"]),
            status=OrderStatus(d["status"]),
            created_at=float(d["created_at"]),
            seq=int(d["seq"]),
            client_key=d.get("client_key"),
            traded_qty=int(d.get("traded_qty", 0)),
            traded_amount=int(d.get("traded_amount", 0)),
        )

    @staticmethod
    def _trade_to_dict(t: Trade) -> Dict[str, Any]:
        return {
            "trade_id": t.trade_id,
            "price": t.price,
            "quantity": t.quantity,
            "buy_order_id": t.buy_order_id,
            "sell_order_id": t.sell_order_id,
            "buy_account_id": t.buy_account_id,
            "sell_account_id": t.sell_account_id,
            "timestamp": t.timestamp,
            "seq": t.seq,
        }

    @staticmethod
    def _trade_from_dict(d: Dict[str, Any]) -> Trade:
        return Trade(
            trade_id=d["trade_id"],
            price=int(d["price"]),
            quantity=int(d["quantity"]),
            buy_order_id=d["buy_order_id"],
            sell_order_id=d["sell_order_id"],
            buy_account_id=d["buy_account_id"],
            sell_account_id=d["sell_account_id"],
            timestamp=float(d["timestamp"]),
            seq=int(d["seq"]),
        )

    @staticmethod
    def _account_to_dict(a: Account) -> Dict[str, Any]:
        return {"account_id": a.account_id, "balance": a.balance, "frozen": a.frozen}

    @staticmethod
    def _account_from_dict(d: Dict[str, Any]) -> Account:
        return Account(
            account_id=d["account_id"],
            balance=int(d["balance"]),
            frozen=int(d["frozen"]),
        )

    # ---- lifecycle -------------------------------------------------------
    def load(self) -> None:
        """Restore state from snapshot + journal. Idempotent."""
        with self._lock:
            state, events = self.persistence.load()
            self.accounts = {
                aid: self._account_from_dict(d) for aid, d in state.get("accounts", {}).items()
            }
            self.orders = {}
            self.trades = [self._trade_from_dict(d) for d in state.get("trades", [])]
            self.idempotency = dict(state.get("idempotency", {}))
            self._seq = int(state.get("last_seq", 0))
            self._trade_seq = int(state.get("trade_seq", 0))
            self._minted = int(state.get("minted", 0))
            self.book = OrderBook()
            # Rebuild book from active orders in the snapshot.
            for d in state.get("orders", []):
                order = self._order_from_dict(d)
                self.orders[order.order_id] = order
                if order.is_active:
                    self.book.add(order)
            # Replay journal events after the snapshot.
            for event in events:
                self._replay(event)
            self._loaded = True

    def snapshot(self) -> None:
        """Atomically persist full state and reset the journal."""
        with self._lock:
            state = {
                "version": 1,
                "last_seq": self._seq,
                "trade_seq": self._trade_seq,
                "minted": self._minted,
                "accounts": {aid: self._account_to_dict(a) for aid, a in self.accounts.items()},
                "orders": [self._order_to_dict(o) for o in self.orders.values()],
                "trades": [self._trade_to_dict(t) for t in self.trades],
                "idempotency": self.idempotency,
            }
            self.persistence.save_snapshot(state)

    # ---- event replay ----------------------------------------------------
    def _replay(self, event: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "account_open":
            aid = event["account_id"]
            if aid not in self.accounts:
                self.accounts[aid] = Account(aid, balance=int(event["initial_balance"]))
                self._minted += int(event["initial_balance"])
        elif etype == "deposit":
            aid = event["account_id"]
            if aid in self.accounts:
                self.accounts[aid].balance += int(event["amount"])
                self._minted += int(event["amount"])
        elif etype == "submit":
            self._replay_submit(event)
        elif etype == "cancel":
            self._replay_cancel(event)
        else:
            raise MarketError("corrupt_event", f"unknown event type: {etype}")

    def _replay_submit(self, event: Dict[str, Any]) -> None:
        d = event["order"]
        # Reconstruct the fresh order as submitted (unfilled).
        order = Order(
            order_id=d["order_id"],
            account_id=d["account_id"],
            side=Side(d["side"]),
            price=int(d["price"]),
            quantity=int(d["quantity"]),
            remaining=int(d["quantity"]),
            status=OrderStatus.OPEN,
            created_at=float(d["created_at"]),
            seq=int(d["seq"]),
            client_key=d.get("client_key"),
        )
        account = self.accounts.get(order.account_id)
        if account is None:
            raise MarketError("corrupt_event", f"unknown account {order.account_id}")
        # Freeze the required amount.
        freeze = order.price * order.quantity if order.side == Side.BUY else order.quantity
        account.balance -= freeze
        account.frozen += freeze
        self.orders[order.order_id] = order
        # Deterministic matching + settlement.
        trades, next_trade_seq = self.book.match(order, self._trade_seq)
        self._trade_seq = next_trade_seq
        for trade in trades:
            self._settle(trade)
            self.trades.append(trade)
        if not order.is_active:
            self._release_leftover(order)
        # Re-derive the idempotent response deterministically (matching and
        # settlement above reproduce the exact first response).
        if order.client_key:
            self.idempotency[f"submit:{order.client_key}"] = {
                "order": self._order_to_dict(order),
                "trades": [self._trade_to_dict(t) for t in trades],
            }

    def _replay_cancel(self, event: Dict[str, Any]) -> None:
        order = self.orders.get(event["order_id"])
        if order is None or not order.is_active:
            return
        self.book.remove(order)
        order.status = OrderStatus.CANCELLED
        self._release_leftover(order)
        if event.get("client_key"):
            self.idempotency[f"cancel:{event['client_key']}"] = {
                "order": self._order_to_dict(order),
                "trades": [],
            }

    # ---- accounts --------------------------------------------------------
    def open_account(self, account_id: str, initial_balance: int = 0) -> Account:
        if not account_id:
            raise MarketError("bad_request", "account_id required")
        if initial_balance < 0:
            raise MarketError("bad_request", "initial_balance must be >= 0")
        with self._lock:
            if account_id in self.accounts:
                raise MarketError("account_exists", f"account {account_id} already exists")
            seq = self._next_seq()
            account = Account(account_id, balance=initial_balance)
            self.accounts[account_id] = account
            self._minted += initial_balance
            try:
                self.persistence.append(
                    {
                        "seq": seq,
                        "type": "account_open",
                        "account_id": account_id,
                        "initial_balance": initial_balance,
                    }
                )
            except Exception:
                # Roll back the in-memory open.
                self.accounts.pop(account_id, None)
                self._minted -= initial_balance
                self._seq -= 1
                raise MarketError("persistence_failure", "failed to open account")
            return account

    def deposit(self, account_id: str, amount: int) -> Account:
        if amount <= 0:
            raise MarketError("bad_request", "amount must be positive")
        with self._lock:
            account = self.accounts.get(account_id)
            if account is None:
                raise MarketError("account_not_found", f"account {account_id} not found")
            seq = self._next_seq()
            account.balance += amount
            self._minted += amount
            try:
                self.persistence.append(
                    {"seq": seq, "type": "deposit", "account_id": account_id, "amount": amount}
                )
            except Exception:
                account.balance -= amount
                self._minted -= amount
                self._seq -= 1
                raise MarketError("persistence_failure", "failed to persist deposit")
            return account

    def get_account(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return self.accounts.get(account_id)

    # ---- internal helpers -------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _settle(self, trade: Trade) -> None:
        """Move assets between buyer and seller according to a trade.

        buyer:  frozen -= p*q, balance += q
        seller: frozen -= q,   balance += p*q
        """
        buyer = self.accounts[trade.buy_account_id]
        seller = self.accounts[trade.sell_account_id]
        quote = trade.price * trade.quantity
        buyer.frozen -= quote
        buyer.balance += trade.quantity
        seller.frozen -= trade.quantity
        seller.balance += quote

    def _release_leftover(self, order: Order) -> None:
        """Release unused frozen amount back to the account balance."""
        account = self.accounts[order.account_id]
        if order.side == Side.BUY:
            leftover = order.price * order.quantity - order.traded_amount
        else:
            leftover = order.quantity - order.traded_qty
        if leftover > 0:
            account.frozen -= leftover
            account.balance += leftover

    # ---- orders -----------------------------------------------------------
    def submit_order(
        self,
        account_id: str,
        side: Side,
        price: int,
        quantity: int,
        client_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if price <= 0 or quantity <= 0:
            raise MarketError("bad_request", "price and quantity must be positive")
        with self._lock:
            # Idempotency: a repeated client key returns the first response.
            if client_key:
                cached = self.idempotency.get(f"submit:{client_key}")
                if cached is not None:
                    return cached
            account = self.accounts.get(account_id)
            if account is None:
                raise MarketError("account_not_found", f"account {account_id} not found")
            if side == Side.BUY:
                freeze = price * quantity
            else:
                freeze = quantity
            if account.balance < freeze:
                raise MarketError(
                    "insufficient_balance",
                    f"need {freeze} available, have {account.balance}",
                )

            seq = self._next_seq()
            order = Order(
                order_id=new_order_id(),
                account_id=account_id,
                side=side,
                price=price,
                quantity=quantity,
                remaining=quantity,
                status=OrderStatus.OPEN,
                created_at=now_ts(),
                seq=seq,
                client_key=client_key,
            )
            # Freeze (escrow) the required assets.
            account.balance -= freeze
            account.frozen += freeze
            self.orders[order.order_id] = order

            try:
                self.persistence.append(
                    {
                        "seq": seq,
                        "type": "submit",
                        "order": self._order_to_dict(
                            Order(
                                order_id=order.order_id,
                                account_id=order.account_id,
                                side=order.side,
                                price=order.price,
                                quantity=order.quantity,
                                remaining=order.quantity,
                                status=OrderStatus.OPEN,
                                created_at=order.created_at,
                                seq=seq,
                                client_key=client_key,
                            )
                        ),
                        "client_key": client_key,
                    }
                )
            except Exception:
                # Roll back the in-memory mutation; nothing was committed.
                account.balance += freeze
                account.frozen -= freeze
                self.orders.pop(order.order_id, None)
                self._seq -= 1
                raise MarketError("persistence_failure", "failed to persist order")

            # Commit point passed: match and settle in memory.
            trades, next_trade_seq = self.book.match(order, self._trade_seq)
            self._trade_seq = next_trade_seq
            for trade in trades:
                self._settle(trade)
                self.trades.append(trade)
            if not order.is_active:
                self._release_leftover(order)

            response = {
                "order": self._order_to_dict(order),
                "trades": [self._trade_to_dict(t) for t in trades],
            }
            # Cache the response so retries with the same client key return
            # the first result without duplicating the order or freeze.
            if client_key:
                self.idempotency[f"submit:{client_key}"] = response
            return response

    def cancel_order(
        self,
        account_id: str,
        order_id: str,
        client_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if client_key:
                cached = self.idempotency.get(f"cancel:{client_key}")
                if cached is not None:
                    return cached
            order = self.orders.get(order_id)
            if order is None:
                raise MarketError("order_not_found", f"order {order_id} not found")
            if order.account_id != account_id:
                raise MarketError("forbidden", "order belongs to another account")
            if not order.is_active:
                # Already filled/cancelled: return current state, no-op.
                return {"order": self._order_to_dict(order), "trades": []}

            seq = self._next_seq()
            self.book.remove(order)
            order.status = OrderStatus.CANCELLED
            self._release_leftover(order)
            try:
                self.persistence.append(
                    {
                        "seq": seq,
                        "type": "cancel",
                        "order_id": order_id,
                        "account_id": account_id,
                        "client_key": client_key,
                    }
                )
            except Exception:
                # Roll back: re-open the order and re-freeze.
                order.status = (
                    OrderStatus.PARTIALLY_FILLED if order.traded_qty else OrderStatus.OPEN
                )
                self.book.add(order)
                account = self.accounts[account_id]
                if order.side == Side.BUY:
                    refund = order.price * order.quantity - order.traded_amount
                else:
                    refund = order.quantity - order.traded_qty
                if refund > 0:
                    account.balance -= refund
                    account.frozen += refund
                self._seq -= 1
                raise MarketError("persistence_failure", "failed to persist cancel")

            response = {"order": self._order_to_dict(order), "trades": []}
            if client_key:
                self.idempotency[f"cancel:{client_key}"] = response
            return response

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._lock:
            return self.orders.get(order_id)

    def list_orders(
        self,
        account_id: Optional[str] = None,
        status: Optional[OrderStatus] = None,
        limit: int = 50,
        cursor: Optional[int] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            items = list(self.orders.values())
            if account_id is not None:
                items = [o for o in items if o.account_id == account_id]
            if status is not None:
                items = [o for o in items if o.status == status]
            if start_ts is not None:
                items = [o for o in items if o.created_at >= start_ts]
            if end_ts is not None:
                items = [o for o in items if o.created_at <= end_ts]
            if cursor is not None:
                items = [o for o in items if o.seq > cursor]
            items.sort(key=lambda o: o.seq)
            page = items[:limit]
            next_cursor = page[-1].seq if len(items) > limit else None
            return {
                "items": [self._order_to_dict(o) for o in page],
                "next_cursor": next_cursor,
            }

    # ---- market data -------------------------------------------------------
    def list_trades(
        self,
        limit: int = 50,
        cursor: Optional[int] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            items = list(self.trades)
            if start_ts is not None:
                items = [t for t in items if t.timestamp >= start_ts]
            if end_ts is not None:
                items = [t for t in items if t.timestamp <= end_ts]
            if cursor is not None:
                items = [t for t in items if t.seq > cursor]
            items.sort(key=lambda t: t.seq)
            page = items[:limit]
            next_cursor = page[-1].seq if len(items) > limit else None
            return {
                "items": [self._trade_to_dict(t) for t in page],
                "next_cursor": next_cursor,
            }

    def depth(self, levels: int = 10) -> Dict[str, Any]:
        with self._lock:
            bids, asks = self.book.depth(levels)
            return {
                "bids": [
                    {"price": d.price, "quantity": d.quantity, "order_count": d.order_count}
                    for d in bids
                ],
                "asks": [
                    {"price": d.price, "quantity": d.quantity, "order_count": d.order_count}
                    for d in asks
                ],
            }

    # ---- invariants ----------------------------------------------------------
    def total_supply(self) -> int:
        """Sum of all balances + frozen amounts; must equal minted tokens."""
        with self._lock:
            return sum(a.balance + a.frozen for a in self.accounts.values())

    def check_invariant(self) -> None:
        """Raise AssertionError if any custody invariant is violated."""
        with self._lock:
            for a in self.accounts.values():
                assert a.balance >= 0, f"negative balance for {a.account_id}"
                assert a.frozen >= 0, f"negative frozen for {a.account_id}"
            supply = sum(a.balance + a.frozen for a in self.accounts.values())
            assert supply == self._minted, f"conservation broken: {supply} != {self._minted}"
            # Every active order's frozen amount is escrowed exactly once.
            escrowed = 0
            for o in self.orders.values():
                if o.is_active:
                    if o.side == Side.BUY:
                        # frozen still held = limit cost - spent at trade prices.
                        escrowed += o.price * o.quantity - o.traded_amount
                    else:
                        escrowed += o.quantity - o.traded_qty
            total_frozen = sum(a.frozen for a in self.accounts.values())
            assert escrowed == total_frozen, (
                f"escrow mismatch: orders hold {escrowed}, accounts frozen {total_frozen}"
            )
