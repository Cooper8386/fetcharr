"""Small shared utilities."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


def parse_rfc3339(s: str | None) -> float | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            offset = ""
            for sign in ("+", "-"):
                if sign in tail:
                    idx = tail.index(sign)
                    offset = tail[idx:]
                    tail = tail[:idx]
                    break
            tail = (tail[:6]).ljust(6, "0")
            s = f"{head}.{tail}{offset}"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def free_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / 1024 / 1024 / 1024
    except OSError:
        return -1.0


def human_bytes(n: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.2f} {units[i]}"


def human_seconds(s: int | float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
