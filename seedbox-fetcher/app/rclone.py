"""
rclone interactions.

list_top_level / release_stats: quick subprocess.run calls (synchronous).
RcloneCopy: long-running pull, runs in a thread; parses --stats-one-line
output from stderr line by line and pushes progress into AppState.

Stats line format (rclone 1.66, --stats-one-line):
    Transferred:   12.345 GiB / 91.382 GiB, 13%, 67.234 MiB/s, ETA 19m45s
We extract (transferred_bytes, total_bytes, percent, speed_bps, eta_seconds).
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("seedbox-fetcher.rclone")


# ---------- simple wrappers ----------

def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    full = ["rclone"] + cmd
    LOG.debug("rclone: %s", " ".join(full))
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def lsd_probe(remote: str) -> tuple[bool, str]:
    r = _run(["lsd", f"{remote}:"], timeout=30)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or "").strip()[-1000:]


def list_top_level(remote: str, remote_path: str) -> list[dict]:
    target = f"{remote}:{remote_path}"
    r = _run(["lsjson", target, "--fast-list"], timeout=120)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "directory not found" in err.lower() or "no such file" in err.lower():
            return []
        LOG.warning("rclone lsjson failed for %s: %s", target, err[-500:])
        return []
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        LOG.warning("rclone lsjson invalid JSON for %s: %s", target, e)
        return []


def release_stats(remote: str, remote_path: str, is_dir: bool) -> tuple[int, int] | None:
    target = f"{remote}:{remote_path}"
    if not is_dir:
        r = _run(["lsjson", target, "--no-modtime"], timeout=60)
        if r.returncode != 0:
            return None
        try:
            entries = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not entries:
            return None
        return int(entries[0].get("Size", 0)), 1
    r = _run(["size", target, "--json", "--fast-list"], timeout=300)
    if r.returncode != 0:
        LOG.warning("rclone size failed for %s: %s",
                    target, (r.stderr or "").strip()[-500:])
        return None
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return int(info.get("bytes", 0)), int(info.get("count", 0))


# ---------- live copy ----------

# Accept all the unit forms rclone might emit.
_UNIT_TO_BYTES = {
    "B": 1, "Bytes": 1,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5,
    "KBytes": 1000, "MBytes": 1000**2, "GBytes": 1000**3,
    "TBytes": 1000**4, "PBytes": 1000**5,
}

# Matches both short-form ("12.3 GiB / 91.4 GiB, 13%, 67.2 MiB/s, ETA 19m45s")
# and long-form ("0 / 73.84 GBytes, 0%, 0 Bytes/s, ETA -") with optional
# log decoration like "2026/06/29 16:23:01 NOTICE: Transferred: ...".
_STATS_RE = re.compile(
    r"Transferred:\s*"
    # transferred amount (number + optional unit)
    r"([\d.]+)\s*([KMGTP]i?B(?:ytes)?|Bytes|B)?\s*/\s*"
    # total amount
    r"([\d.]+)\s*([KMGTP]i?B(?:ytes)?|Bytes|B)\s*,\s*"
    # percent
    r"(\d+(?:\.\d+)?|-)\s*%"
    # optional speed
    r"(?:\s*,\s*(\d+(?:\.\d+)?|-)\s*([KMGTP]i?B(?:ytes)?|Bytes|B)/s)?"
    # optional ETA
    r"(?:\s*,\s*ETA\s*([0-9hmsd:-]+))?"
)


def _to_bytes(value: str | float | None, unit: str | None) -> int:
    if value is None or value == "-":
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    return int(v * _UNIT_TO_BYTES.get(unit or "B", 1))


def _eta_to_seconds(s: str | None) -> int:
    if not s or s == "-":
        return 0
    total = 0
    for part, mult in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        if part in s:
            chunk, _, s = s.partition(part)
            try:
                total += int(chunk) * mult
            except ValueError:
                pass
    return total


def parse_stats_line(line: str) -> dict | None:
    m = _STATS_RE.search(line)
    if not m:
        return None
    t_val, t_unit, total_val, total_unit, pct_str, sp_val, sp_unit, eta = m.groups()
    # A line like "Transferred:  20 / 22, 91%" has no units; that's the file-count
    # progress line, not bytes. Ignore those.
    if t_unit is None and total_unit is None:
        return None
    transferred = _to_bytes(t_val, t_unit)
    total = _to_bytes(total_val, total_unit)
    try:
        pct = float(pct_str) if pct_str != "-" else (
            (transferred / total * 100.0) if total else 0.0
        )
    except (TypeError, ValueError):
        pct = (transferred / total * 100.0) if total else 0.0
    speed_bps = _to_bytes(sp_val, sp_unit)
    eta_sec = _eta_to_seconds(eta)
    return {
        "bytes_transferred": transferred,
        "bytes_total": total,
        "percent": pct,
        "speed_bps": speed_bps,
        "eta_seconds": eta_sec,
    }


class RcloneCopy:
    """
    Runs `rclone copy` as a subprocess, parses --stats-one-line output, and
    invokes on_stats(stats_dict) on every parsed line.

    .terminate() sends SIGTERM (then SIGKILL after 10s). rclone responds to
    SIGTERM by stopping cleanly and removing partial transfers (--use-mmap +
    its own cleanup), but if a chunk file is left behind we sweep dest on
    cancel ourselves.
    """

    def __init__(
        self,
        remote: str,
        remote_path: str,
        dest: Path,
        transfers: int,
        checkers: int,
        bwlimit: str | None,
        on_stats: Callable[[dict], None],
        on_log: Callable[[str, str], None],
    ):
        self.remote = remote
        self.remote_path = remote_path
        self.dest = dest
        self.transfers = transfers
        self.checkers = checkers
        self.bwlimit = bwlimit
        self.on_stats = on_stats
        self.on_log = on_log
        self.proc: subprocess.Popen | None = None
        self._tail: list[str] = []
        self._tail_lock = threading.Lock()

    def _append_tail(self, line: str) -> None:
        with self._tail_lock:
            self._tail.append(line)
            if len(self._tail) > 200:
                self._tail = self._tail[-200:]

    def run(self) -> tuple[bool, str]:
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "rclone", "copy",
            f"{self.remote}:{self.remote_path}",
            str(self.dest),
            "--transfers", str(self.transfers),
            "--checkers", str(self.checkers),
            "--retries", "5",
            "--low-level-retries", "10",
            "--stats", "1s",
            "--stats-one-line",
            "--stats-log-level", "NOTICE",
            "--use-mmap",
            "--progress-terminal-title=false",
        ]
        if self.bwlimit:
            cmd.extend(["--bwlimit", self.bwlimit])
        self.on_log("info", f"rclone {' '.join(cmd[1:])}")

        # rclone writes stats to stderr by default.
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,  # own process group, for clean SIGTERM
        )
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            self._append_tail(line)
            stats = parse_stats_line(line)
            if stats:
                try:
                    self.on_stats(stats)
                except Exception:
                    LOG.exception("on_stats raised")
            else:
                # Non-stats lines are usually rclone warnings/info; surface them.
                LOG.debug("rclone: %s", line)
        rc = self.proc.wait()
        with self._tail_lock:
            tail = "\n".join(self._tail[-50:])
        if rc == 0:
            return True, tail
        return False, f"rclone exit {rc}\n{tail}"

    def terminate(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        # give it 10s to clean up, then SIGKILL
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.2)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
