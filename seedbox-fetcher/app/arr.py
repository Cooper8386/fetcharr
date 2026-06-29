"""Sonarr/Radarr history lookup (optional filter)."""
from __future__ import annotations

import logging
import time

import requests

from .config import ArrInstance
from .util import parse_rfc3339

LOG = logging.getLogger("seedbox-fetcher.arr")


def _recent_history(inst: ArrInstance, page_size: int = 200) -> list[dict]:
    try:
        r = requests.get(
            f"{inst.url}/api/v3/history",
            params={
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
            },
            headers={"X-Api-Key": inst.api_key},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        LOG.warning("arr history fetch failed for %s: %s", inst.name, e)
        return []


def arr_already_handled(
    instances: list[ArrInstance],
    release_name: str,
    grace_minutes: int,
) -> tuple[bool, str | None]:
    """Returns (skip, reason). True iff an *arr already imported this >grace ago."""
    now = time.time()
    grace_sec = grace_minutes * 60
    target = release_name.lower()
    for inst in instances:
        for rec in _recent_history(inst):
            source = (rec.get("sourceTitle") or "").lower()
            if not source:
                continue
            if source == target or target in source or source in target:
                event_type = rec.get("eventType")
                date_str = rec.get("date")
                ts = parse_rfc3339(date_str) or 0
                age_sec = now - ts if ts else 0
                if (event_type in ("downloadFolderImported", 3)
                        and age_sec >= grace_sec):
                    return True, (
                        f"{inst.name} imported {age_sec/3600:.1f}h ago "
                        f"(sourceTitle={rec.get('sourceTitle')!r})"
                    )
                return False, None
    return False, None
