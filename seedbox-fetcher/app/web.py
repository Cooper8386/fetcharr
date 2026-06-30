"""
Web UI + JSON API + SSE + Prometheus metrics + action endpoints.

Runs uvicorn in a background thread so the main process can keep the poller
and worker alive even if FastAPI explodes.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Config
from .state import AppState, PersistentState

LOG = logging.getLogger("seedbox-fetcher.web")

# CSRF token mints on first GET, required on all POSTs. Single global token
# is fine here - this is LAN-only and not multi-user.
_CSRF_TOKEN = secrets.token_urlsafe(32)


def _release_status(entry: dict, key_in_current_pull: bool) -> str:
    if key_in_current_pull:
        return "pulling"
    if entry.get("manual_skip"):
        return "manual_skip"
    if entry.get("bootstrap"):
        return "bootstrap"
    if entry.get("cutoff_skipped"):
        return "cutoff_skipped"
    if entry.get("arr_skipped"):
        return "arr_skipped"
    if entry.get("pulled"):
        return "pulled"
    return "pending"


def build_app(cfg: Config, persistent: PersistentState, app_state: AppState) -> FastAPI:
    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))

    api = FastAPI(title="fetcharr", docs_url=None, redoc_url=None)

    if static_dir.exists():
        api.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ---------- helpers ----------

    def _require_csrf(req_token: str | None) -> None:
        if not req_token or not secrets.compare_digest(req_token, _CSRF_TOKEN):
            raise HTTPException(status_code=403, detail="CSRF token mismatch")

    def _snapshot_payload() -> dict[str, Any]:
        live = app_state.snapshot()
        cur_key = live["current_pull"].get("key") or ""
        persistent_data = persistent.snapshot()
        releases: list[dict[str, Any]] = []
        for key, entry in persistent_data.items():
            if key == "__meta__":
                continue
            releases.append({
                "key": key,
                "size": entry.get("size", 0),
                "file_count": entry.get("file_count", 0),
                "mtime": entry.get("mtime"),
                "first_seen": entry.get("first_seen"),
                "last_change": entry.get("last_change"),
                "pulled": bool(entry.get("pulled", False)),
                "pulled_at": entry.get("pulled_at"),
                "status": _release_status(entry, key == cur_key),
            })
        releases.sort(key=lambda r: (
            -(r.get("pulled_at") or r.get("last_change") or 0),
        ))
        return {
            "metrics": live["metrics"],
            "current_pull": live["current_pull"],
            "paused": live["paused"],
            "sse_seq": live["sse_seq"],
            "speed_history": live.get("speed_history", []),
            "releases": releases,
            "logs": app_state.log_lines(n=200),
            "config_summary": {
                "rclone_remote": cfg.rclone_remote,
                "poll_interval": cfg.poll_interval,
                "stability_seconds": cfg.stability_seconds,
                "cutoff_mtime": cfg.cutoff_mtime_iso,
                "arr_history_check_enabled": cfg.arr_history_check_enabled,
                "routes": [
                    {"name": r.name,
                     "remote_path": r.remote_path,
                     "dest": str(r.dest)}
                    for r in cfg.routes
                ],
            },
        }

    # ---------- routes ----------

    @api.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "csrf_token": _CSRF_TOKEN,
                "title": "fetcharr",
            },
        )

    @api.get("/api/state")
    def api_state() -> JSONResponse:
        return JSONResponse(_snapshot_payload())

    @api.get("/api/events")
    def api_events() -> StreamingResponse:
        """
        Server-Sent Events stream. Emits `data: {...}` whenever the app's
        seq counter advances (start of pull, stats update, completion, etc.)
        or every 15s as a keepalive.
        """
        def gen():
            last = -1
            # Initial snapshot.
            payload = json.dumps(_snapshot_payload())
            yield f"data: {payload}\n\n"
            last = app_state.sse_seq
            while True:
                new = app_state.wait_for_change(last, timeout=15.0)
                if new == last:
                    # keepalive
                    yield ": keepalive\n\n"
                    continue
                payload = json.dumps(_snapshot_payload())
                yield f"data: {payload}\n\n"
                last = new

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        m = app_state.metrics
        cp = app_state.current_pull
        snap = persistent.snapshot()
        # Bucket releases by status.
        buckets: dict[str, int] = {
            "pulled": 0,
            "bootstrap": 0,
            "cutoff_skipped": 0,
            "arr_skipped": 0,
            "manual_skip": 0,
            "pending": 0,
            "pulling": 1 if cp.key else 0,
        }
        for key, entry in snap.items():
            if key == "__meta__":
                continue
            st = _release_status(entry, False)
            if st in buckets:
                buckets[st] += 1
            else:
                buckets["pending"] += 1
        lines = [
            "# HELP fetcharr_remote_reachable Whether the rclone remote was reachable at last probe.",
            "# TYPE fetcharr_remote_reachable gauge",
            f"fetcharr_remote_reachable {int(m.remote_reachable)}",
            "# HELP fetcharr_paused Whether the poller is paused.",
            "# TYPE fetcharr_paused gauge",
            f"fetcharr_paused {int(app_state.paused)}",
            "# HELP fetcharr_poll_total Number of poll cycles started.",
            "# TYPE fetcharr_poll_total counter",
            f"fetcharr_poll_total {m.poll_total}",
            "# HELP fetcharr_poll_errors_total Number of poll cycles that raised.",
            "# TYPE fetcharr_poll_errors_total counter",
            f"fetcharr_poll_errors_total {m.poll_errors_total}",
            "# HELP fetcharr_pulls_succeeded_total Successful pulls.",
            "# TYPE fetcharr_pulls_succeeded_total counter",
            f"fetcharr_pulls_succeeded_total {m.pulls_succeeded_total}",
            "# HELP fetcharr_pulls_failed_total Failed pulls.",
            "# TYPE fetcharr_pulls_failed_total counter",
            f"fetcharr_pulls_failed_total {m.pulls_failed_total}",
            "# HELP fetcharr_pulls_cancelled_total Cancelled pulls.",
            "# TYPE fetcharr_pulls_cancelled_total counter",
            f"fetcharr_pulls_cancelled_total {m.pulls_cancelled_total}",
            "# HELP fetcharr_bytes_pulled_total Total bytes successfully pulled.",
            "# TYPE fetcharr_bytes_pulled_total counter",
            f"fetcharr_bytes_pulled_total {m.bytes_pulled_total}",
            "# HELP fetcharr_queue_depth Pending pull jobs.",
            "# TYPE fetcharr_queue_depth gauge",
            f"fetcharr_queue_depth {m.queue_depth}",
            "# HELP fetcharr_last_poll_timestamp_seconds Unix ts of last poll cycle.",
            "# TYPE fetcharr_last_poll_timestamp_seconds gauge",
            f"fetcharr_last_poll_timestamp_seconds {m.last_poll_ts}",
            "# HELP fetcharr_current_pull_bytes_total Bytes total of in-flight pull.",
            "# TYPE fetcharr_current_pull_bytes_total gauge",
            f"fetcharr_current_pull_bytes_total {cp.bytes_total}",
            "# HELP fetcharr_current_pull_bytes_transferred Bytes transferred so far.",
            "# TYPE fetcharr_current_pull_bytes_transferred gauge",
            f"fetcharr_current_pull_bytes_transferred {cp.bytes_transferred}",
            "# HELP fetcharr_current_pull_speed_bytes_per_sec Speed in B/s.",
            "# TYPE fetcharr_current_pull_speed_bytes_per_sec gauge",
            f"fetcharr_current_pull_speed_bytes_per_sec {cp.speed_bps}",
            "# HELP fetcharr_current_pull_percent Current pull percent.",
            "# TYPE fetcharr_current_pull_percent gauge",
            f"fetcharr_current_pull_percent {cp.percent}",
            "# HELP fetcharr_releases_total Count of releases by status.",
            "# TYPE fetcharr_releases_total gauge",
        ]
        for state, n in buckets.items():
            lines.append(f'fetcharr_releases_total{{state="{state}"}} {n}')
        return PlainTextResponse("\n".join(lines) + "\n")

    @api.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        if app_state.metrics.remote_reachable:
            return PlainTextResponse("ok")
        return PlainTextResponse("rclone remote not reachable", status_code=503)

    # ---------- action endpoints ----------

    async def _csrf_from(req: Request) -> str | None:
        # Accept token from header or form field.
        h = req.headers.get("X-CSRF-Token")
        if h:
            return h
        try:
            form = await req.form()
            return form.get("csrf_token")
        except Exception:
            return None

    @api.post("/api/actions/repull/{route_name}/{release_name:path}")
    async def action_repull(route_name: str, release_name: str, request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        key = f"{route_name}/{release_name}"
        if not persistent.get(key):
            raise HTTPException(404, f"unknown key {key!r}")
        # force=True so the next poll bypasses cutoff + arr-history filters.
        persistent.unmark_pulled(key, force=True)
        persistent.save()
        # Also remove from disk so a re-pull lands fresh.
        dest = None
        for r in cfg.routes:
            if r.name == route_name:
                dest = r.dest / release_name
                break
        if dest and dest.exists():
            import shutil
            try:
                shutil.rmtree(dest)
            except Exception as e:
                LOG.warning("could not remove %s: %s", dest, e)
        app_state.log("info", f"action: repull queued for {key}")
        return {"ok": True, "key": key, "action": "repull"}

    @api.post("/api/actions/mark_pulled/{route_name}/{release_name:path}")
    async def action_mark_pulled(route_name: str, release_name: str, request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        key = f"{route_name}/{release_name}"
        if not persistent.get(key):
            raise HTTPException(404, f"unknown key {key!r}")
        persistent.mark_pulled(key, manual_skip=True)
        persistent.save()
        app_state.log("info", f"action: marked pulled (manual_skip) for {key}")
        return {"ok": True, "key": key, "action": "mark_pulled"}

    @api.post("/api/actions/forget/{route_name}/{release_name:path}")
    async def action_forget(route_name: str, release_name: str, request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        key = f"{route_name}/{release_name}"
        persistent.forget(key)
        persistent.save()
        app_state.log("info", f"action: forgot {key}")
        return {"ok": True, "key": key, "action": "forget"}

    @api.post("/api/actions/cancel")
    async def action_cancel(request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        if not app_state.current_pull.key:
            raise HTTPException(409, "no pull in progress")
        app_state.request_cancel()
        app_state.log("warning",
                      f"action: cancel requested for {app_state.current_pull.key}")
        return {"ok": True, "action": "cancel"}

    @api.post("/api/actions/pause")
    async def action_pause(request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        app_state.set_paused(True)
        app_state.log("info", "action: poller paused")
        return {"ok": True, "paused": True}

    @api.post("/api/actions/resume")
    async def action_resume(request: Request) -> dict:
        _require_csrf(await _csrf_from(request))
        app_state.set_paused(False)
        app_state.log("info", "action: poller resumed")
        return {"ok": True, "paused": False}

    # ---------- config page ----------

    EDITABLE = {
        "poll_interval":     ("int", 5,    3600, "Seconds between seedbox scans"),
        "stability_seconds": ("int", 10,   3600, "How long a release must be size-stable before pulling"),
        "min_free_gb":       ("int", 0,    None, "Refuse to pull if free space below this"),
        "rclone_transfers":  ("int", 1,    32,   "Parallel file transfers per rclone copy"),
        "rclone_checkers":   ("int", 1,    64,   "Parallel checks during rclone copy"),
        "rclone_bwlimit":    ("str", None, None, "Bandwidth limit, e.g. '70M', '0' for unlimited, or schedule string"),
    }

    def _current_value(name: str) -> Any:
        rv = app_state.get_runtime(name, None)
        if rv is not None:
            return rv
        return {
            "poll_interval":     cfg.poll_interval,
            "stability_seconds": cfg.stability_seconds,
            "min_free_gb":       cfg.min_free_gb,
            "rclone_transfers":  cfg.rclone_transfers,
            "rclone_checkers":   cfg.rclone_checkers,
            "rclone_bwlimit":    cfg.rclone_bwlimit or "",
        }[name]

    @api.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> HTMLResponse:
        fields = []
        for name, (kind, lo, hi, help_text) in EDITABLE.items():
            fields.append({
                "name": name, "kind": kind, "min": lo, "max": hi,
                "help": help_text, "value": _current_value(name),
            })
        return templates.TemplateResponse(
            request=request, name="config.html",
            context={"csrf_token": _CSRF_TOKEN, "title": "fetcharr config", "fields": fields},
        )

    @api.get("/api/config")
    def api_config() -> JSONResponse:
        return JSONResponse({name: _current_value(name) for name in EDITABLE})

    @api.post("/api/config")
    async def api_config_set(request: Request) -> JSONResponse:
        _require_csrf(await _csrf_from(request))
        body = await request.json()
        updates: dict = {}
        errors: list[str] = []
        for name, raw in body.items():
            if name not in EDITABLE:
                errors.append(f"unknown field: {name}")
                continue
            kind, lo, hi, _ = EDITABLE[name]
            if kind == "int":
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    errors.append(f"{name}: must be an integer")
                    continue
                if lo is not None and v < lo:
                    errors.append(f"{name}: must be >= {lo}")
                    continue
                if hi is not None and v > hi:
                    errors.append(f"{name}: must be <= {hi}")
                    continue
                updates[name] = v
            else:
                s = (raw or "").strip() if isinstance(raw, str) else ""
                updates[name] = s or None
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        for name, v in updates.items():
            app_state.set_runtime(**{name: v})
            app_state.log("info", f"config: {name} = {v!r}")
        try:
            _persist_config_yml(updates)
        except Exception as e:
            app_state.log("warning",
                f"applied changes in-memory but failed to persist to config.yml: {e}")
            return JSONResponse({"ok": True, "applied": list(updates.keys()),
                                 "persist_warning": str(e)})
        return JSONResponse({"ok": True, "applied": list(updates.keys())})

    return api


def _persist_config_yml(updates: dict) -> None:
    """Rewrite /config/config.yml with the new tunable values."""
    import yaml
    path = Path(os.environ.get("CONFIG", "/config/config.yml"))
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    if "poll_interval" in updates:
        raw["poll_interval"] = updates["poll_interval"]
    if "stability_seconds" in updates:
        raw["stability_seconds"] = updates["stability_seconds"]
    if "min_free_gb" in updates:
        raw["min_free_gb"] = updates["min_free_gb"]
    rclone_section = raw.get("rclone", {}) or {}
    if "rclone_transfers" in updates:
        rclone_section["transfers"] = updates["rclone_transfers"]
    if "rclone_checkers" in updates:
        rclone_section["checkers"] = updates["rclone_checkers"]
    if "rclone_bwlimit" in updates:
        v = updates["rclone_bwlimit"]
        if v is None:
            rclone_section.pop("bwlimit", None)
        else:
            rclone_section["bwlimit"] = v
    raw["rclone"] = rclone_section
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    tmp.replace(path)


def run_web_in_thread(api: FastAPI, host: str, port: int) -> threading.Thread:
    config = uvicorn.Config(
        api, host=host, port=port,
        log_level="warning", access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        try:
            server.run()
        except Exception:
            LOG.exception("uvicorn died")

    t = threading.Thread(target=_serve, daemon=True, name="web")
    t.start()
    LOG.info("web UI listening on http://%s:%d", host, port)
    return t
