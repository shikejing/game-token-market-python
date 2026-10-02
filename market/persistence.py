"""Durable persistence: append-only event journal (WAL) + snapshots.

Persistence conventions:
- Every state-changing operation is journaled as one JSON line before the
  operation result is returned to the client. The journal is fsynced.
- On restart the engine loads the latest snapshot (if any) and replays
  journal events whose seq is greater than the snapshot's last seq.
- A torn final journal line (process killed mid-write) is detected: the
  line is dropped and the journal truncated to the last valid line, so a
  partial write never poisons recovery.
- Snapshots are written atomically (temp file + fsync + rename) while the
  engine lock is held; after a snapshot the journal is reset.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple


class Persistence:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.snapshot_path = os.path.join(data_dir, "snapshot.json")
        self.journal_path = os.path.join(data_dir, "journal.log")

    def ensure_dir(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)

    # ---- journal --------------------------------------------------------
    def append(self, event: Dict[str, Any]) -> None:
        """Durably append one event to the journal (WAL commit point)."""
        self.ensure_dir()
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _read_journal(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not os.path.exists(self.journal_path):
            return events
        valid_end = 0
        with open(self.journal_path, "r", encoding="utf-8") as f:
            for line in f:
                valid_end += len(line.encode("utf-8"))
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(json.loads(stripped))
                except json.JSONDecodeError:
                    # Torn final line: drop it and truncate the journal.
                    self._truncate(valid_end - len(line.encode("utf-8")))
                    break
        return events

    def _truncate(self, size: int) -> None:
        with open(self.journal_path, "r+b") as f:
            f.truncate(size)
            f.flush()
            os.fsync(f.fileno())

    # ---- snapshot --------------------------------------------------------
    def load(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Return (snapshot_state, journal_events_after_snapshot)."""
        state: Dict[str, Any] = {}
        if os.path.exists(self.snapshot_path):
            with open(self.snapshot_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        snap_seq = state.get("last_seq", 0)
        events = [e for e in self._read_journal() if e.get("seq", 0) > snap_seq]
        return state, events

    def save_snapshot(self, state: Dict[str, Any]) -> None:
        """Atomically write a full snapshot and reset the journal."""
        self.ensure_dir()
        tmp_path = self.snapshot_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, separators=(",", ":"), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.snapshot_path)
        # Journal is now fully covered by the snapshot; reset it.
        with open(self.journal_path, "w", encoding="utf-8") as f:
            f.flush()
            os.fsync(f.fileno())
