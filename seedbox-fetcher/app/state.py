"""
Persistent state (state.yml on disk) + live AppState (in-memory, thread-safe).

PersistentState is the source of truth for "what releases have we seen and
what did we do with them." AppState wraps it plus live, ephemeral data:
current pull progress, recent log lines, current event sequence for SSE.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger("seedbox-fetcher.state")


# ---------- Persistent state ----------

class PersistentState:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if state_file.exists():
            try:
                with state_file.open() as f:
                    self.data = yaml.safe_load(f) or {}
            except Exception:
                self.data = {}
        else:
            self.data = {}

    @property
    def is_bootstrapped(self) -> bool:
        with self._lock:
            return bool(self.data.get("__meta__", {}).get("bootstrapped", False))

    def mark_bootstrapped(self) -> None:
        with self._lock:
            self.data.setdefault("__meta__", {})
            self.data["__meta__"]["bootstrapped"] = True
            self.data["__meta__"]["bootstrapped_at"] = time.time()

    def save(self) -> None:
        with self._lock:
            tmp = self.state_file.with_suffix(".tmp")
            with tmp.open("w") as f:
                yaml.safe_dump(self.data, f)
            tmp.replace(self.state_file)

    def get(self, key: str) -> dict | None:
        if key == "__meta__":
            return None
        with self._lock:
            v = self.data.get(key)
            return dict(v) if v else None

    def observe(self, key: str, size: int, file_count: int,
                mtime: float | None, now: float) -> dict:
        with self._lock:
            prev = self.data.get(key)
            if (not prev
                    or prev.get("size") != size
                    or prev.get("file_count") != file_count):
                entry = {
                    "size": size,
                    "file_count": file_count,
                    "first_seen": prev["first_seen"] if prev else now,
                    "last_change": now,
                    "pulled": prev.get("pulled", False) if prev else False,
                }
                if mtime is not None:
                    entry["mtime"] = mtime
                for flag in ("bootstrap", "cutoff_skipped", "arr_skipped",
                             "manual_skip", "pulled_at"):
                    if prev and flag in prev:
                        entry[flag] = prev[flag]
                self.data[key] = entry
            else:
                entry = dict(prev)
                if mtime is not None:
                    entry["mtime"] = mtime
                self.data[key] = entry
            return dict(entry)

    def mark_pulled(self, key: str, **flags) -> None:
        with self._lock:
            if key not in self.data:
                return
            self.data[key]["pulled"] = True
            self.data[key]["pulled_at"] = time.time()
            for k, v in flags.items():
                self.data[key][k] = v

    def unmark_pulled(self, key: str) -> None:
        with self._lock:
            if key not in self.data:
                return
            self.data[key]["pulled"] = False
            for flag in ("pulled_at", "bootstrap", "cutoff_skipped",
                         "arr_skipped", "manual_skip"):
                self.data[key].pop(flag, None)

    def forget(self, key: str) -> None:
        with self._lock:
            self.data.pop(key, None)

    def all_release_keys(self) -> list[str]:
        with self._lock:
            return [k for k in self.data.keys() if k != "__meta__"]

    def snapshot(self) -> dict[str, Any]:
        """Read-only deep-copy of the state for the UI."""
        with self._lock:
            return {k: dict(v) if isinstance(v, dict) else v
                    for k, v in self.data.items()}


# ---------- Live application state ----------

@dataclass
class CurrentPull:
    key: str = ""
    remote_path: str = ""
    dest: str = ""
    bytes_total: int = 0
    bytes_transferred: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: int = 0
    started_at: float = 0.0
    last_stats_at: float = 0.0
    cancelled: bool = False


@dataclass
class AppMetrics:
    poll_total: int = 0
    poll_errors_total: int = 0
    pulls_succeeded_total: int = 0
    pulls_failed_total: int = 0
    pulls_cancelled_total: int = 0
    bytes_pulled_total: int = 0
    queue_depth: int = 0
    last_poll_ts: float = 0.0
    last_pull_ok_ts: float = 0.0
    last_pull_fail_ts: float = 0.0
    remote_reachable: bool = False


class AppState:
    """
    Thread-safe shared state. Holds:
      - PersistentState (already thread-safe)
      - CurrentPull (the running rclone copy, if any)
      - AppMetrics (counters/gauges for /metrics and UI)
      - log_ring (recent log lines)
      - sse_seq (monotonic int incremented on every meaningful change so the
        SSE endpoint knows when to emit)
    """

    def __init__(self, persistent: PersistentState, log_ring_size: int = 500):
        self.persistent = persistent
        self.current_pull = CurrentPull()
        self.metrics = AppMetrics()
        self._lock = threading.RLock()
        self._log_ring: collections.deque[dict] = collections.deque(maxlen=log_ring_size)
        self._sse_seq = 0
        self._sse_cond = threading.Condition()
        self.paused = False

    # ----- log ring -----

    def log(self, level: str, message: str) -> None:
        with self._lock:
            self._log_ring.append({
                "ts": time.time(),
                "level": level,
                "message": message,
            })
        self._bump()

    def log_lines(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._log_ring)[-n:]

    # ----- SSE -----

    def _bump(self) -> None:
        with self._sse_cond:
            self._sse_seq += 1
            self._sse_cond.notify_all()

    def wait_for_change(self, last_seq: int, timeout: float = 15.0) -> int:
        with self._sse_cond:
            if self._sse_seq > last_seq:
                return self._sse_seq
            self._sse_cond.wait(timeout=timeout)
            return self._sse_seq

    @property
    def sse_seq(self) -> int:
        with self._sse_cond:
            return self._sse_seq

    # ----- current pull -----

    def start_pull(self, key: str, remote_path: str, dest: str, bytes_total: int) -> None:
        with self._lock:
            self.current_pull = CurrentPull(
                key=key,
                remote_path=remote_path,
                dest=dest,
                bytes_total=bytes_total,
                started_at=time.time(),
                last_stats_at=time.time(),
            )
        self._bump()

    def update_pull(self, bytes_transferred: int, percent: float,
                    speed_bps: float, eta_seconds: int) -> None:
        with self._lock:
            self.current_pull.bytes_transferred = bytes_transferred
            self.current_pull.percent = percent
            self.current_pull.speed_bps = speed_bps
            self.current_pull.eta_seconds = eta_seconds
            self.current_pull.last_stats_at = time.time()
        self._bump()

    def end_pull(self, success: bool, cancelled: bool, bytes_pulled: int) -> None:
        with self._lock:
            self.current_pull = CurrentPull()
            if cancelled:
                self.metrics.pulls_cancelled_total += 1
            elif success:
                self.metrics.pulls_succeeded_total += 1
                self.metrics.last_pull_ok_ts = time.time()
                self.metrics.bytes_pulled_total += bytes_pulled
            else:
                self.metrics.pulls_failed_total += 1
                self.metrics.last_pull_fail_ts = time.time()
        self._bump()

    def request_cancel(self) -> None:
        with self._lock:
            self.current_pull.cancelled = True
        self._bump()

    def is_cancel_requested(self) -> bool:
        with self._lock:
            return self.current_pull.cancelled

    # ----- metrics -----

    def record_poll(self, ok: bool) -> None:
        with self._lock:
            self.metrics.poll_total += 1
            self.metrics.last_poll_ts = time.time()
            if not ok:
                self.metrics.poll_errors_total += 1
        self._bump()

    def set_remote_reachable(self, ok: bool) -> None:
        with self._lock:
            self.metrics.remote_reachable = ok
        self._bump()

    def set_queue_depth(self, n: int) -> None:
        with self._lock:
            self.metrics.queue_depth = n
        self._bump()

    # ----- pause -----

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused
        self._bump()

    # ----- snapshot for UI -----

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "metrics": asdict(self.metrics),
                "current_pull": asdict(self.current_pull),
                "paused": self.paused,
                "sse_seq": self.sse_seq,
            }
