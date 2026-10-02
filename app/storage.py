"""Append-only WAL persistence.

Record framing (little-endian):

    [payload_len: uint32][crc32(payload): uint32][payload: JSON bytes]

A command batch (several events) is written in one ``write()`` call and
fsync'ed before it is acknowledged, so either the whole batch is durable or
none of it is (a torn tail is discarded on the next open).
"""
from __future__ import annotations

import json
import os
import struct
import threading
import zlib
from typing import Any, Optional

from app.errors import StorageError

_HEADER = struct.Struct("<II")  # payload_len, crc32


class EventStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._path = os.path.join(data_dir, "events.log")
        self._fh: Optional[int] = None
        # number of future append() calls that must fail before writing
        self._fail_next = 0
        # when true, append succeeds durably but signals the engine to act as
        # if the process crashed before applying events in memory
        self.simulate_crash_after_append = False
        os.makedirs(data_dir, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._fh = os.open(
            self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600
        )

    # ------------------------------------------------------------------ write
    def append(self, events: list[dict[str, Any]]) -> None:
        """Atomically append a batch; raise StorageError without partial state."""
        if not events:
            return
        blob = b"".join(self._encode(e) for e in events)
        with self._lock:
            if self._fail_next > 0:
                self._fail_next -= 1
                raise StorageError("injected write failure")
            if self._fh is None:
                raise StorageError("store is closed")
            try:
                # One write call: either fully lands or leaves at most a torn
                # tail (which is discarded on read).
                written = 0
                while written < len(blob):
                    n = os.write(self._fh, blob[written:])
                    if n <= 0:
                        raise OSError("short write")
                    written += n
                os.fsync(self._fh)
            except OSError as exc:
                raise StorageError(f"wal append failed: {exc}") from exc

    def _encode(self, event: dict[str, Any]) -> bytes:
        payload = json.dumps(event, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return _HEADER.pack(len(payload), crc) + payload

    # ------------------------------------------------------------------- read
    def read_all(self) -> list[dict[str, Any]]:
        """Return all valid records, truncating any torn/corrupt tail."""
        with self._lock:
            self._flush()
            with open(self._path, "rb") as f:
                data = f.read()
        events: list[dict[str, Any]] = []
        pos = 0
        good_end = 0
        n = len(data)
        while pos + _HEADER.size <= n:
            length, crc = _HEADER.unpack_from(data, pos)
            start = pos + _HEADER.size
            end = start + length
            if end > n:
                break  # torn header payload
            payload = data[start:end]
            if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                break  # corrupt frame
            try:
                events.append(json.loads(payload.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                break
            good_end = end
            pos = end
        if good_end != n and self._fh is not None:
            os.ftruncate(self._fh, good_end)
            os.fsync(self._fh)
        return events

    def _flush(self) -> None:
        if self._fh is not None:
            os.fsync(self._fh)

    # ------------------------------------------------------------------ admin
    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    os.fsync(self._fh)
                finally:
                    os.close(self._fh)
                    self._fh = None

    def reset(self) -> None:
        with self._lock:
            if self._fh is not None:
                os.close(self._fh)
                self._fh = None
            if os.path.exists(self._path):
                os.remove(self._path)
            self._open()

    # -------------------------------------------------------------- test hooks
    def fail_next_writes(self, count: int = 1) -> None:
        with self._lock:
            self._fail_next += count

    def crash_point(self, enabled: bool) -> None:
        with self._lock:
            self.simulate_crash_after_append = enabled
