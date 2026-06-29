"""Apprise notifications + Healthchecks pings."""
from __future__ import annotations

import logging

import apprise
import requests

LOG = logging.getLogger("seedbox-fetcher.notifier")


class Notifier:
    def __init__(self, urls: list[str], healthcheck_url: str | None):
        self.apprise = apprise.Apprise()
        for u in urls:
            u = u.strip() if isinstance(u, str) else ""
            if u:
                self.apprise.add(u)
        self.healthcheck_url = healthcheck_url

    def alert(self, title: str, body: str) -> None:
        LOG.warning("ALERT: %s :: %s", title, body)
        if len(self.apprise) > 0:
            try:
                self.apprise.notify(title=f"[seedbox-fetcher] {title}", body=body)
            except Exception as e:
                LOG.error("apprise notify failed: %s", e)

    def info(self, title: str, body: str) -> None:
        LOG.info("INFO: %s :: %s", title, body)
        if len(self.apprise) > 0:
            try:
                self.apprise.notify(
                    title=f"[seedbox-fetcher] {title}",
                    body=body,
                    notify_type=apprise.NotifyType.INFO,
                )
            except Exception as e:
                LOG.error("apprise notify failed: %s", e)

    def heartbeat(self, ok: bool = True) -> None:
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url if ok else f"{self.healthcheck_url}/fail"
        try:
            requests.get(url, timeout=10)
        except Exception as e:
            LOG.debug("healthcheck ping failed: %s", e)
