"""
fetcharr entry point.

Wires together:
  - Config + PersistentState + AppState + Notifier
  - rclone probe + bootstrap (one-time)
  - Poller thread (queues PullJobs)
  - Worker thread (runs rclone copies)
  - Web thread (FastAPI / uvicorn) if enabled
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from .config import Config
from .notifier import Notifier
from .poller import bootstrap, run_poller
from . import rclone as rc
from .state import AppState, PersistentState
from .web import build_app, run_web_in_thread
from .worker import run_worker

LOG = logging.getLogger("seedbox-fetcher")


class AppLogHandler(logging.Handler):
    """Forwards Python log records into AppState's ring buffer for the UI."""
    def __init__(self, app_state: AppState):
        super().__init__()
        self.app_state = app_state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.app_state.log(record.levelname.lower(), self.format(record))
        except Exception:
            pass


def _setup_logging(app_state: AppState | None) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s :: %(message)s"
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=fmt,
    )
    if app_state is not None:
        h = AppLogHandler(app_state)
        h.setFormatter(logging.Formatter("%(name)s :: %(message)s"))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)


def main() -> int:
    cfg_path = Path(os.environ.get("CONFIG", "/config/config.yml"))
    if not cfg_path.exists():
        logging.basicConfig(level="INFO")
        LOG.error("config not found at %s. Create one before starting.", cfg_path)
        return 2
    cfg = Config.load(cfg_path)
    persistent = PersistentState(cfg.state_dir / "state.yml")
    app_state = AppState(persistent)
    notifier = Notifier(cfg.apprise_urls, cfg.healthcheck_url)

    _setup_logging(app_state)

    LOG.info(
        "seedbox-fetcher starting; poll=%ds stability=%ds routes=%d remote=%s",
        cfg.poll_interval, cfg.stability_seconds, len(cfg.routes), cfg.rclone_remote,
    )
    if cfg.cutoff_mtime_epoch is not None:
        LOG.info("cutoff_mtime: %s", cfg.cutoff_mtime_iso)
    else:
        LOG.info("no cutoff_mtime set")
    if cfg.arr_history_check_enabled:
        LOG.info("arr_history_check ENABLED across %d instance(s), grace=%dm",
                 len(cfg.arr_instances), cfg.arr_history_grace_minutes)
    else:
        LOG.info("arr_history_check disabled")

    reachable, err = rc.lsd_probe(cfg.rclone_remote)
    app_state.set_remote_reachable(reachable)
    if not reachable:
        notifier.alert(
            "rclone probe failed at startup",
            f"`rclone lsd {cfg.rclone_remote}:` failed:\n{err}",
        )
    else:
        LOG.info("rclone remote reachable.")

    if not persistent.is_bootstrapped:
        LOG.warning(
            "state is not bootstrapped; snapshotting current seedbox contents "
            "and marking all as already-pulled."
        )
        n = bootstrap(cfg, persistent, notifier)
        LOG.info("bootstrap complete: %d releases marked as already-pulled", n)

    pull_q: "queue.Queue" = queue.Queue()
    stop_event = threading.Event()

    poller_thread = threading.Thread(
        target=run_poller,
        name="poller",
        args=(cfg, persistent, app_state, notifier, pull_q, stop_event),
        daemon=True,
    )
    worker_thread = threading.Thread(
        target=run_worker,
        name="worker",
        args=(cfg, persistent, app_state, notifier, pull_q, stop_event),
        daemon=True,
    )

    poller_thread.start()
    worker_thread.start()

    web_thread = None
    if cfg.web.enabled:
        api = build_app(cfg, persistent, app_state)
        web_thread = run_web_in_thread(api, cfg.web.host, cfg.web.port)

    def _stop(signum, frame):
        LOG.info("signal %s received, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Re-probe the remote every 5 minutes so the UI/metrics gauge stays honest.
    last_probe = time.time()
    while not stop_event.is_set():
        if time.time() - last_probe > 300:
            ok, err = rc.lsd_probe(cfg.rclone_remote)
            app_state.set_remote_reachable(ok)
            if not ok:
                LOG.warning("rclone remote probe failed: %s", err[-200:])
            last_probe = time.time()
        time.sleep(1)

    LOG.info("waiting for threads to drain...")
    poller_thread.join(timeout=10)
    worker_thread.join(timeout=20)
    LOG.info("bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
