"""Pull worker: pulls one PullJob off the queue at a time, runs rclone."""
from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
from pathlib import Path

from .config import Config
from .notifier import Notifier
from .poller import PullJob
from .rclone import RcloneCopy
from .state import AppState, PersistentState

LOG = logging.getLogger("seedbox-fetcher.worker")


def _du_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _disk_scanner(
    dest: Path, app: AppState, stop: threading.Event, interval: float = 2.0
) -> None:
    """While stop is not set, periodically scan dest and update bytes_on_disk."""
    while not stop.is_set():
        if dest.exists():
            app.update_pull_disk(_du_bytes(dest))
        stop.wait(interval)


def run_worker(
    cfg: Config,
    persistent: PersistentState,
    app: AppState,
    notifier: Notifier,
    pull_q: "queue.Queue[PullJob]",
    stop_event: threading.Event,
) -> None:
    LOG.info("worker starting")
    while not stop_event.is_set():
        try:
            job: PullJob = pull_q.get(timeout=1.0)
        except queue.Empty:
            continue

        app.start_pull(
            key=job.key,
            remote_path=f"{cfg.rclone_remote}:{job.remote_path}",
            dest=str(job.dest),
            bytes_total=job.bytes_total,
        )
        LOG.info("pulling %s -> %s", job.key, job.dest)
        app.log("info",
                f"pulling {job.key} ({job.bytes_total / 1024 / 1024 / 1024:.2f} GB) "
                f"-> {job.dest}")

        # Re-read tunables at job-start so live /config changes take effect
        # on the next pull without a restart.
        copy = RcloneCopy(
            remote=cfg.rclone_remote,
            remote_path=job.remote_path,
            dest=job.dest,
            transfers=app.get_runtime("rclone_transfers", cfg.rclone_transfers),
            checkers=app.get_runtime("rclone_checkers", cfg.rclone_checkers),
            bwlimit=app.get_runtime("rclone_bwlimit", cfg.rclone_bwlimit),
            on_stats=lambda s: app.update_pull(
                bytes_transferred=s["bytes_transferred"],
                percent=s["percent"],
                speed_bps=s["speed_bps"],
                eta_seconds=s["eta_seconds"],
            ),
            on_log=lambda level, msg: app.log(level, msg),
        )

        # Disk-scan fallback so we still show progress if stats are silent.
        scan_stop = threading.Event()
        scanner = threading.Thread(
            target=_disk_scanner, args=(job.dest, app, scan_stop), daemon=True
        )
        scanner.start()

        # Run rclone in a sub-thread so we can poll for cancel requests.
        result: dict = {}

        def _go() -> None:
            ok, tail = copy.run()
            result["ok"] = ok
            result["tail"] = tail

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        while t.is_alive():
            t.join(timeout=0.5)
            if app.is_cancel_requested() or stop_event.is_set():
                LOG.warning("cancel requested for %s; terminating rclone", job.key)
                app.log("warning", f"cancel requested for {job.key}")
                copy.terminate()
                t.join(timeout=15)
                break

        scan_stop.set()
        scanner.join(timeout=5)

        cancelled = app.is_cancel_requested()
        ok = result.get("ok", False)
        tail = result.get("tail", "")

        bytes_pulled = job.bytes_total if ok else 0
        app.end_pull(success=ok, cancelled=cancelled, bytes_pulled=bytes_pulled)

        if cancelled:
            LOG.warning("pull cancelled: %s", job.key)
            app.log("warning", f"pull cancelled: {job.key}")
            # Wipe any partial bits left in dest so a re-pull starts clean.
            try:
                if job.dest.exists():
                    shutil.rmtree(job.dest)
            except Exception as e:
                LOG.warning("could not clean partial dest %s: %s", job.dest, e)
            # Allow re-pull on next cycle.
            persistent.unmark_pulled(job.key)
            persistent.save()
            notifier.alert("Pull cancelled", job.key)
        elif ok:
            LOG.info("pull complete: %s", job.key)
            app.log("info", f"pull complete: {job.key}")
            persistent.mark_pulled(job.key)
            persistent.save()
            notifier.info(
                "Pulled",
                f"{job.key} ({job.bytes_total / 1024 / 1024 / 1024:.2f} GB, "
                f"{job.file_count} files)",
            )
        else:
            LOG.warning("pull failed: %s\n%s", job.key, tail)
            app.log("error", f"pull failed: {job.key}\n{tail[-500:]}")
            notifier.alert("Pull failed", f"{job.key}\n{tail[-1500:]}")

        pull_q.task_done()
        app.set_queue_depth(pull_q.qsize())
    LOG.info("worker stopping")
