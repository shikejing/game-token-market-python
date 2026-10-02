"""Service container: wires persistence, engine and instruments."""
from __future__ import annotations

import os
import threading
from typing import Optional

from app.engine import MatchingEngine
from app.storage import EventStore

# symbol -> (base asset, quote asset)
DEFAULT_INSTRUMENTS = (
    ("GEM-GOLD", "GEM", "GOLD"),
    ("ORE-GOLD", "ORE", "GOLD"),
)


class MarketService:
    """Owns the durable store and the matching engine for the process."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.environ.get(
            "MARKET_DATA_DIR", os.path.join(os.getcwd(), "data")
        )
        self.store: Optional[EventStore] = None
        self.engine: Optional[MatchingEngine] = None
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> "MarketService":
        with self._lock:
            if self._started:
                return self
            os.makedirs(self.data_dir, exist_ok=True)
            self.store = EventStore(self.data_dir)
            self.engine = MatchingEngine(self.store)
            self.engine.restore()
            for symbol, base, quote in DEFAULT_INSTRUMENTS:
                self.engine.add_instrument(symbol, base, quote)
            self._started = True
            return self

    def stop(self) -> None:
        with self._lock:
            if self.store is not None:
                self.store.close()
            self._started = False

    def reset_for_tests(self) -> None:
        """Wipe durable data and rebuild an empty engine (tests only)."""
        with self._lock:
            if self.store is not None:
                self.store.reset()
            self.engine = MatchingEngine(self.store)
            for symbol, base, quote in DEFAULT_INSTRUMENTS:
                self.engine.add_instrument(symbol, base, quote)
            self._started = True


_service: Optional[MarketService] = None
_service_lock = threading.Lock()


def _build_service(data_dir: Optional[str]) -> MarketService:
    # caller must hold _service_lock (avoids re-entering a non-reentrant lock)
    global _service
    if _service is not None:
        _service.stop()
    _service = MarketService(data_dir).start()
    return _service


def init_service(data_dir: Optional[str] = None) -> MarketService:
    """Create (or replace) the process-wide service and start it."""
    with _service_lock:
        return _build_service(data_dir)


def get_service() -> MarketService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _build_service(None)
    assert _service is not None
    return _service
