"""Config loading."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

LOG = logging.getLogger("seedbox-fetcher.config")


@dataclass(frozen=True)
class CategoryRoute:
    name: str
    remote_path: str         # e.g. "downloads/qbittorrent/remote-tv"
    dest: Path               # e.g. "/data/torrents/remote-tv"


@dataclass(frozen=True)
class ArrInstance:
    name: str
    url: str
    api_key: str


@dataclass(frozen=True)
class WebConfig:
    enabled: bool
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    routes: list[CategoryRoute]
    poll_interval: int
    stability_seconds: int
    min_free_gb: int
    rclone_remote: str
    apprise_urls: list[str]
    healthcheck_url: str | None
    state_dir: Path
    rclone_bwlimit: str | None
    rclone_transfers: int
    rclone_checkers: int
    cutoff_mtime_iso: str | None
    arr_history_check_enabled: bool
    arr_history_grace_minutes: int
    arr_instances: list[ArrInstance]
    web: WebConfig

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open() as f:
            raw = yaml.safe_load(f) or {}

        routes = [
            CategoryRoute(
                name=r["name"],
                remote_path=r["remote_path"].strip("/"),
                dest=Path(r["dest"]),
            )
            for r in raw["routes"]
        ]
        rc = raw.get("rclone", {}) or {}

        arr_cfg = raw.get("arr_history_check", {}) or {}
        arr_instances: list[ArrInstance] = []
        for inst in arr_cfg.get("instances", []) or []:
            api_key = inst.get("api_key", "") or ""
            if api_key.startswith("env:"):
                api_key = os.environ.get(api_key[4:], "")
            if not api_key:
                LOG.warning("arr instance %r has no api_key; skipping", inst.get("name"))
                continue
            arr_instances.append(ArrInstance(
                name=inst["name"],
                url=inst["url"].rstrip("/"),
                api_key=api_key,
            ))

        web_raw = raw.get("web", {}) or {}
        web = WebConfig(
            enabled=bool(web_raw.get("enabled", True)),
            host=str(web_raw.get("host", "0.0.0.0")),
            port=int(web_raw.get("port", 8765)),
        )

        return cls(
            routes=routes,
            poll_interval=int(raw.get("poll_interval", 60)),
            stability_seconds=int(raw.get("stability_seconds", 90)),
            min_free_gb=int(raw.get("min_free_gb", 50)),
            rclone_remote=rc.get("remote", "seedbox"),
            apprise_urls=list(raw.get("apprise_urls", []) or []),
            healthcheck_url=raw.get("healthcheck_url"),
            state_dir=Path(raw.get("state_dir", "/state")),
            rclone_bwlimit=rc.get("bwlimit"),
            rclone_transfers=int(rc.get("transfers", 4)),
            rclone_checkers=int(rc.get("checkers", 8)),
            cutoff_mtime_iso=raw.get("cutoff_mtime"),
            arr_history_check_enabled=bool(arr_cfg.get("enabled", False)),
            arr_history_grace_minutes=int(arr_cfg.get("grace_minutes", 60)),
            arr_instances=arr_instances,
            web=web,
        )

    @property
    def cutoff_mtime_epoch(self) -> float | None:
        if not self.cutoff_mtime_iso:
            return None
        try:
            dt = datetime.fromisoformat(self.cutoff_mtime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError as e:
            LOG.warning("invalid cutoff_mtime %r: %s", self.cutoff_mtime_iso, e)
            return None
