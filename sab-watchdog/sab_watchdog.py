#!/usr/bin/env python3
"""
sab_watchdog

Watches SABnzbd's queue. When a job sits in the same post-processing status
(or "Trying RAR renamer") with no observable progress for too long, it:

  1. Alerts (after ALERT_AFTER_MIN minutes)
  2. Bounces post-processing with pause_pp + resume_pp (after BOUNCE_AFTER_MIN)
  3. Calls restart_repair on the job (after RESTART_AFTER_MIN)

It never deletes a job. Sonarr/Radarr Failed Download Handling cleans up
after the job ultimately aborts.

Config via env vars or /config/config.yml. Env wins.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import apprise
import requests
import yaml

LOG = logging.getLogger("sab-watchdog")

STUCK_STATUSES_LOWER = (
    "trying rar renamer",
    "verifying",
    "repairing",
    "extracting",
    "unpacking",
    "running script",
)


@dataclass(frozen=True)
class Config:
    sab_url: str
    sab_api_key: str
    poll_interval: int
    alert_after_min: int
    bounce_after_min: int
    restart_after_min: int
    apprise_urls: list[str]
    healthcheck_url: str | None
    state_path: Path

    @classmethod
    def load(cls) -> "Config":
        cfg_path = Path(os.environ.get("CONFIG", "/config/config.yml"))
        raw: dict = {}
        if cfg_path.exists():
            with cfg_path.open() as f:
                raw = yaml.safe_load(f) or {}

        def get(name: str, default=None, cast=str):
            env_val = os.environ.get(name.upper())
            if env_val is not None:
                return cast(env_val)
            val = raw.get(name.lower(), default)
            if val is None:
                return None
            return cast(val) if cast is not list else val

        sab_url = os.environ.get("SAB_URL") or raw.get("sab_url")
        sab_key = os.environ.get("SAB_API_KEY") or raw.get("sab_api_key")
        if not sab_url or not sab_key:
            raise SystemExit("SAB_URL and SAB_API_KEY are required")

        return cls(
            sab_url=sab_url.rstrip("/"),
            sab_api_key=sab_key,
            poll_interval=int(os.environ.get("POLL_INTERVAL", raw.get("poll_interval", 60))),
            alert_after_min=int(os.environ.get("ALERT_AFTER_MIN", raw.get("alert_after_min", 30))),
            bounce_after_min=int(os.environ.get("BOUNCE_AFTER_MIN", raw.get("bounce_after_min", 45))),
            restart_after_min=int(os.environ.get("RESTART_AFTER_MIN", raw.get("restart_after_min", 75))),
            apprise_urls=(
                (os.environ["APPRISE_URLS"].split(",") if os.environ.get("APPRISE_URLS")
                 else list(raw.get("apprise_urls", [])))
            ),
            healthcheck_url=os.environ.get("HEALTHCHECK_URL") or raw.get("healthcheck_url"),
            state_path=Path(os.environ.get("STATE_PATH", raw.get("state_path", "/state/state.json"))),
        )


class Notifier:
    def __init__(self, urls: list[str], healthcheck_url: str | None):
        self.apprise = apprise.Apprise()
        for u in urls:
            u = u.strip()
            if u:
                self.apprise.add(u)
        self.healthcheck_url = healthcheck_url

    def notify(self, title: str, body: str) -> None:
        LOG.info("%s :: %s", title, body)
        if len(self.apprise) > 0:
            try:
                self.apprise.notify(title=f"[sab-watchdog] {title}", body=body)
            except Exception as e:
                LOG.error("apprise notify failed: %s", e)

    def heartbeat(self, ok: bool) -> None:
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url if ok else f"{self.healthcheck_url}/fail"
        try:
            requests.get(url, timeout=10)
        except Exception as e:
            LOG.debug("healthcheck ping failed: %s", e)


def sab_api(cfg: Config, mode: str, **extra) -> dict:
    qs = urllib.parse.urlencode({"mode": mode, "output": "json", "apikey": cfg.sab_api_key, **extra})
    r = requests.get(f"{cfg.sab_url}/api?{qs}", timeout=20)
    r.raise_for_status()
    return r.json()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def is_stuck_status(status: str) -> bool:
    if not status:
        return False
    s = status.lower()
    return any(needle in s for needle in STUCK_STATUSES_LOWER)


def fingerprint(slot: dict) -> tuple:
    return (
        slot.get("mb_left"),
        slot.get("percentage"),
        slot.get("status"),
        slot.get("filename"),
    )


def poll_once(cfg: Config, notifier: Notifier, state: dict) -> None:
    try:
        queue = sab_api(cfg, "queue").get("queue", {})
    except Exception as e:
        notifier.notify("SAB API unreachable", f"{e}")
        notifier.heartbeat(ok=False)
        return

    slots = queue.get("slots", []) or []
    now = time.time()
    seen = set()

    for slot in slots:
        nzo = slot.get("nzo_id")
        if not nzo:
            continue
        seen.add(nzo)

        fp = list(fingerprint(slot))
        prev = state.get(nzo, {})
        if prev.get("fp") != fp:
            state[nzo] = {
                "fp": fp,
                "since": now,
                "stage": 0,
                "filename": slot.get("filename", "?"),
            }
            continue

        if not is_stuck_status(slot.get("status", "")):
            continue

        age_min = (now - prev["since"]) / 60
        stage = prev.get("stage", 0)
        filename = slot.get("filename", "?")
        status = slot.get("status", "?")

        if age_min >= cfg.alert_after_min and stage < 1:
            notifier.notify(
                "Job stuck",
                f"{filename}\nStatus: {status}\nStuck for: {int(age_min)} min",
            )
            state[nzo]["stage"] = 1

        if age_min >= cfg.bounce_after_min and stage < 2:
            notifier.notify(
                "Bouncing post-processing",
                f"{filename}\npause_pp + resume_pp",
            )
            try:
                sab_api(cfg, "pause_pp")
                time.sleep(20)
                sab_api(cfg, "resume_pp")
                state[nzo]["stage"] = 2
            except Exception as e:
                notifier.notify("Bounce failed", f"{filename}\n{e}")

        if age_min >= cfg.restart_after_min and stage < 3:
            notifier.notify(
                "Calling restart_repair",
                f"{filename}\nLast status: {status}",
            )
            try:
                sab_api(cfg, "restart_repair")
                state[nzo]["stage"] = 3
            except Exception as e:
                notifier.notify("restart_repair failed", f"{filename}\n{e}")

    # Forget completed jobs
    for nzo in list(state.keys()):
        if nzo not in seen:
            state.pop(nzo, None)

    notifier.heartbeat(ok=True)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    cfg = Config.load()
    notifier = Notifier(cfg.apprise_urls, cfg.healthcheck_url)
    state = load_state(cfg.state_path)

    LOG.info(
        "sab-watchdog starting; poll=%ds alert=%dm bounce=%dm restart=%dm",
        cfg.poll_interval, cfg.alert_after_min, cfg.bounce_after_min, cfg.restart_after_min,
    )

    stopping = False

    def _stop(signum, frame):
        nonlocal stopping
        LOG.info("signal %s received, stopping", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stopping:
        try:
            poll_once(cfg, notifier, state)
            save_state(cfg.state_path, state)
        except Exception:
            LOG.exception("poll cycle crashed")
            notifier.heartbeat(ok=False)
        for _ in range(cfg.poll_interval):
            if stopping:
                break
            time.sleep(1)

    LOG.info("sab-watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
