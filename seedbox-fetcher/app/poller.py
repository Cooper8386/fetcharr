"""
Poller thread.

Walks the seedbox each cycle, evaluates each release through the filters
(state -> cutoff -> stability -> already-present -> arr-history-check), and
enqueues anything that survives onto the pull queue.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .arr import arr_already_handled
from .config import Config, CategoryRoute
from . import rclone as rc
from .notifier import Notifier
from .state import AppState, PersistentState
from .util import free_gb, parse_rfc3339

LOG = logging.getLogger("seedbox-fetcher.poller")


@dataclass
class PullJob:
    key: str
    route_name: str
    remote_path: str           # full path under the remote
    dest: Path                 # full local dest path (with release name)
    bytes_total: int
    file_count: int


def bootstrap(cfg: Config, persistent: PersistentState, notifier: Notifier) -> int:
    count = 0
    now = time.time()
    for route in cfg.routes:
        for entry in rc.list_top_level(cfg.rclone_remote, route.remote_path):
            name = entry.get("Name")
            if not name or name.startswith("."):
                continue
            key = f"{route.name}/{name}"
            mtime = parse_rfc3339(entry.get("ModTime"))
            self_entry = {
                "size": int(entry.get("Size", 0)),
                "file_count": 0,
                "first_seen": now,
                "last_change": now,
                "pulled": True,
                "pulled_at": now,
                "bootstrap": True,
            }
            if mtime is not None:
                self_entry["mtime"] = mtime
            with persistent._lock:                  # noqa: SLF001
                persistent.data[key] = self_entry
            count += 1
    persistent.mark_bootstrapped()
    persistent.save()
    notifier.info(
        "Bootstrap complete",
        f"Snapshotted {count} existing seedbox releases as already-pulled. "
        f"Future pulls only fire for new arrivals."
    )
    return count


def _poll_route(
    cfg: Config,
    route: CategoryRoute,
    persistent: PersistentState,
    app: AppState,
    notifier: Notifier,
    pull_q: "queue.Queue[PullJob]",
    cutoff: float | None,
    now: float,
    stability_seconds: int,
    min_free_gb: int,
) -> int:
    entries = rc.list_top_level(cfg.rclone_remote, route.remote_path)
    if not entries:
        return 0
    check_path = route.dest if route.dest.exists() else route.dest.parent
    free = free_gb(check_path)
    if 0 <= free < min_free_gb:
        notifier.alert(
            "Low disk space",
            f"{route.dest} has only {free:.1f} GB free "
            f"(<{min_free_gb} GB). Skipping {route.name}.",
        )
        return 0

    queued = 0
    for entry in entries:
        name = entry.get("Name")
        if not name or name.startswith("."):
            continue
        is_dir = bool(entry.get("IsDir", False))
        mtime = parse_rfc3339(entry.get("ModTime"))
        remote_full = f"{route.remote_path}/{name}"
        key = f"{route.name}/{name}"

        existing = persistent.get(key)
        if existing and existing.get("pulled"):
            continue

        # Manual re-pull bypasses cutoff and arr-history filters.
        force_pull = bool(existing and existing.get("force_pull"))

        if not force_pull and cutoff is not None and mtime is not None and mtime < cutoff:
            LOG.info("cutoff-skip %s", key)
            app.log("info", f"cutoff-skip {key}")
            persistent.observe(key, int(entry.get("Size", 0)), 0, mtime, now)
            persistent.mark_pulled(key, cutoff_skipped=True)
            continue

        stats = rc.release_stats(cfg.rclone_remote, remote_full, is_dir)
        if stats is None:
            continue
        size, count = stats
        entry_state = persistent.observe(key, size, count, mtime, now)

        stable_for = now - entry_state["last_change"]
        if stable_for < stability_seconds:
            LOG.debug("not stable yet: %s (%ds)", key, int(stable_for))
            continue

        local_dest = route.dest / name
        if local_dest.exists():
            LOG.info("already present locally, marking pulled: %s", key)
            app.log("info", f"already present locally: {key}")
            persistent.mark_pulled(key)
            continue

        if not force_pull and cfg.arr_history_check_enabled and cfg.arr_instances:
            skip, reason = arr_already_handled(
                cfg.arr_instances, name, cfg.arr_history_grace_minutes
            )
            if skip:
                LOG.info("arr-skip %s :: %s", key, reason)
                app.log("info", f"arr-skip {key}: {reason}")
                persistent.mark_pulled(key, arr_skipped=True)
                continue

        pull_q.put(PullJob(
            key=key,
            route_name=route.name,
            remote_path=remote_full,
            dest=local_dest,
            bytes_total=size,
            file_count=count,
        ))
        queued += 1
        LOG.info("queued %s (%.2f GB, %d files)",
                 key, size / 1024 / 1024 / 1024, count)
        app.log("info",
                f"queued {key} ({size / 1024 / 1024 / 1024:.2f} GB, {count} files)")
    return queued


def _garbage_collect(cfg: Config, persistent: PersistentState) -> int:
    seen: set[str] = set()
    for route in cfg.routes:
        for e in rc.list_top_level(cfg.rclone_remote, route.remote_path):
            if e.get("Name"):
                seen.add(f"{route.name}/{e['Name']}")
    stale = [k for k in persistent.all_release_keys() if k not in seen]
    for k in stale:
        persistent.forget(k)
    return len(stale)


def run_poller(
    cfg: Config,
    persistent: PersistentState,
    app: AppState,
    notifier: Notifier,
    pull_q: "queue.Queue[PullJob]",
    stop_event: threading.Event,
) -> None:
    LOG.info("poller starting")
    while not stop_event.is_set():
        # Read tunables fresh each cycle so /config edits apply live.
        poll_interval = int(app.get_runtime("poll_interval", cfg.poll_interval))
        stability_seconds = int(
            app.get_runtime("stability_seconds", cfg.stability_seconds)
        )
        min_free_gb = int(app.get_runtime("min_free_gb", cfg.min_free_gb))

        if app.paused:
            stop_event.wait(timeout=poll_interval)
            continue
        ok = True
        try:
            now = time.time()
            cutoff = cfg.cutoff_mtime_epoch
            total_queued = 0
            for route in cfg.routes:
                total_queued += _poll_route(
                    cfg, route, persistent, app, notifier, pull_q, cutoff, now,
                    stability_seconds, min_free_gb,
                )
            stale = _garbage_collect(cfg, persistent)
            if total_queued or stale:
                persistent.save()
            app.set_queue_depth(pull_q.qsize())
            notifier.heartbeat(ok=True)
        except Exception as e:
            ok = False
            LOG.exception("poll cycle crashed")
            app.log("error", f"poll cycle crashed: {e!r}")
            notifier.alert("Cycle crash", repr(e))
            notifier.heartbeat(ok=False)
        app.record_poll(ok)
        stop_event.wait(timeout=poll_interval)
    LOG.info("poller stopping")
