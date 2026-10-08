"""Flask application: the graph page, its API and the settings endpoint."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import __version__, chat, graph, preview, store
from .config import (
    GRAPH_MODES,
    Settings,
    SettingsStore,
    _coerce_enabled,
    check_password,
    enabled_snapshots,
    get_settings_store,
    validate,
)
from .mqtt_worker import CaptureWorker

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent

# Wrong settings-password attempts cost this much wall time (brute-force
# damping on an otherwise unauthenticated LAN UI).
PASSWORD_DELAY_SECONDS = 0.25


def create_app(
    settings_store: SettingsStore | None = None, start_worker: bool = True
) -> Flask:
    store_ref = settings_store or get_settings_store()
    settings = store_ref.get()
    store.ensure_ready(settings)
    # Every enabled server's database exists before anyone can switch to it
    # (the worker re-ensures this on each reconcile, but the UI may be
    # faster than the supervisor's first tick).
    for snapshot in enabled_snapshots(settings).values():
        store.ensure_ready(snapshot)

    app = Flask(
        __name__,
        template_folder=str(PACKAGE_DIR / "templates"),
        static_folder=str(PACKAGE_DIR / "static"),
    )
    app.config["JSON_SORT_KEYS"] = False

    worker = CaptureWorker(store_ref)
    if start_worker:
        worker.start()
    app.extensions["meshgraph_worker"] = worker
    app.extensions["meshgraph_settings"] = store_ref

    # ------------------------------------------------------------------
    # Settings password (no login: one password guards the dialog)
    # ------------------------------------------------------------------

    def settings_authorized() -> bool:
        """No password configured ⇒ everything is open (first-run setup)."""
        stored = store_ref.get().settings_password_hash
        if not stored:
            return True
        return check_password(request.headers.get("X-Settings-Password", ""), stored)

    def auth_denied():
        """None when allowed; a 401 JSON body when the password is missing/wrong."""
        if settings_authorized():
            return None
        time.sleep(PASSWORD_DELAY_SECONDS)
        return (
            jsonify(
                {"ok": False, "auth": "password", "errors": ["Wrong password."]}
            ),
            401,
        )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        current = store_ref.get()
        return render_template(
            "index.html",
            version=__version__,
            default_mode=current.default_graph_mode,
            default_hours=current.default_hours,
            modes=GRAPH_MODES,
        )

    # ------------------------------------------------------------------
    # Graph
    # ------------------------------------------------------------------

    @app.get("/api/graph")
    def api_graph():
        current = store_ref.get()
        args = request.args
        payload = graph.build_graph(
            settings=current,
            mode=args.get("mode", current.default_graph_mode),
            hours=args.get("hours", current.default_hours, type=int),
            min_snr=args.get("min_snr", -200.0, type=float),
            include_indirect=args.get("include_indirect", "false").lower()
            in ("1", "true", "on", "yes"),
            channel=args.get("channel", ""),
        )
        return jsonify(payload)

    @app.get("/api/channels")
    def api_channels():
        current = store_ref.get()
        since = time.time() - 7 * 24 * 3600
        return jsonify({"channels": store.distinct_channels(current.db_file, since)})

    @app.get("/api/chat")
    def api_chat():
        current = store_ref.get()
        args = request.args
        payload = chat.build_chat(
            settings=current,
            hours=args.get("hours", current.default_hours, type=int),
            channel=args.get("channel", ""),
            limit=args.get("limit", chat.DEFAULT_LIMIT, type=int),
        )
        return jsonify(payload)

    @app.get("/api/link_preview")
    def api_link_preview():
        # Public on purpose: chat is public, and the response only mirrors
        # metadata that any visitor could fetch from the target host anyway.
        try:
            url = preview.validate_url(request.args.get("url", ""))
        except ValueError as exc:
            return jsonify({"ok": False, "errors": [str(exc)]}), 400
        return jsonify({"ok": True, "preview": preview.get_preview(url)})

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        payload = worker.status()
        payload["version"] = __version__
        payload["settings_file"] = str(store_ref.path)
        current = store_ref.get()
        payload["connection"] = {
            "id": current.active_connection,
            "name": current.connection_name,
            "db_file": current.db_file,
        }
        return jsonify(payload)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @app.get("/api/settings")
    def api_settings_get():
        denied = auth_denied()
        if denied:
            return denied
        return jsonify(store_ref.get().masked())

    @app.post("/api/settings")
    def api_settings_post():
        denied = auth_denied()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "errors": ["Expected a JSON object."]}), 400

        # Never accept blank/unknown fields from the client.
        allowed = set(Settings.__dataclass_fields__)
        unknown = sorted(set(body) - allowed)
        if unknown:
            return (
                jsonify(
                    {"ok": False, "errors": [f"Unknown field: {f}" for f in unknown]}
                ),
                400,
            )

        changes = {k: v for k, v in body.items() if v is not None}
        try:
            updated = store_ref.update(**changes)
        except ValueError as exc:
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        except Exception as exc:  # noqa: BLE001
            logger.exception("Settings update failed")
            return jsonify({"ok": False, "errors": [str(exc)]}), 500

        graph.invalidate_cache()
        # A split (new broker typed into the dialog) created a new profile
        # with a brand-new database file: make sure its schema exists before
        # the UI asks for a graph, without waiting for the worker's reconnect.
        store.ensure_ready(updated)
        return jsonify(
            {
                "ok": True,
                "settings": updated.masked(),
                "reconnect": "reconnecting with the new broker settings",
            }
        )

    @app.post("/api/settings/validate")
    def api_settings_validate():
        denied = auth_denied()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        candidate = store_ref.get()
        for key, value in body.items():
            if key in Settings.__dataclass_fields__ and value is not None:
                setattr(candidate, key, value)
        errors = validate(candidate)
        return jsonify({"ok": not errors, "errors": errors})

    @app.post("/api/settings/password")
    def api_settings_password():
        """Set, change or clear (empty) the settings password."""
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "errors": ["Expected a JSON object."]}), 400
        provided = request.headers.get("X-Settings-Password", "") or str(
            body.get("current_password") or ""
        )
        try:
            # set_password re-verifies ``provided`` when a password exists;
            # the first password needs no old one.
            updated = store_ref.set_password(
                str(body.get("password") or ""), current_password=provided
            )
        except ValueError as exc:
            stored = store_ref.get().settings_password_hash
            if stored and not check_password(provided, stored):
                time.sleep(PASSWORD_DELAY_SECONDS)
                return (
                    jsonify(
                        {
                            "ok": False,
                            "auth": "password",
                            "errors": ["Wrong password."],
                        }
                    ),
                    401,
                )
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        return jsonify({"ok": True, "settings": updated.masked()})

    # ------------------------------------------------------------------
    # Connections (one server — one database file)
    # ------------------------------------------------------------------

    @app.post("/api/connections")
    def api_connections_create():
        denied = auth_denied()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "errors": ["Expected a JSON object."]}), 400
        try:
            created = store_ref.add_connection(**body)
        except ValueError as exc:
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        store.ensure_ready(created)
        graph.invalidate_cache()
        return jsonify({"ok": True, "settings": created.masked()})

    @app.post("/api/connections/select")
    def api_connections_select():
        body = request.get_json(silent=True) or {}
        pid = body.get("id") if isinstance(body, dict) else None
        if not pid:
            return jsonify({"ok": False, "errors": ["Connection id is required."]}), 400
        current = store_ref.get()
        profile = next(
            (p for p in current.connections if str(p.get("id") or "") == str(pid)),
            None,
        )
        if profile is None:
            return (
                jsonify({"ok": False, "errors": [f"Unknown connection: {pid}"]}),
                400,
            )
        # Switching is deliberately public: the header dropdown lets anyone
        # move between *enabled* servers without the settings password.
        # Reaching a disabled profile (to edit it) requires unlocking.
        if not settings_authorized() and not _coerce_enabled(
            profile.get("enabled", True)
        ):
            return (
                jsonify(
                    {
                        "ok": False,
                        "auth": "password",
                        "errors": [
                            f"Connection `{pid}` is disabled — unlock the "
                            "settings to select it."
                        ],
                    }
                ),
                403,
            )
        try:
            selected = store_ref.select_connection(str(pid))
        except ValueError as exc:
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        store.ensure_ready(selected)
        graph.invalidate_cache()
        return jsonify({"ok": True, "settings": selected.masked()})

    @app.post("/api/connections/<pid>")
    def api_connections_toggle(pid: str):
        """Enable/disable one server's collection (manager list)."""
        denied = auth_denied()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or "enabled" not in body:
            return (
                jsonify({"ok": False, "errors": ["`enabled` is required."]}),
                400,
            )
        try:
            updated = store_ref.set_connection_enabled(pid, body["enabled"])
        except ValueError as exc:
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        graph.invalidate_cache()
        return jsonify({"ok": True, "settings": updated.masked()})

    @app.delete("/api/connections/<pid>")
    def api_connections_delete(pid: str):
        denied = auth_denied()
        if denied:
            return denied
        try:
            remaining = store_ref.remove_connection(pid)
        except ValueError as exc:
            return jsonify({"ok": False, "errors": str(exc).split("; ")}), 400
        # The database file stays on disk on purpose (see remove_connection).
        graph.invalidate_cache()
        return jsonify({"ok": True, "settings": remaining.masked()})

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return "Not found", 404

    @app.errorhandler(500)
    def internal_error(error):
        # The frontend expects JSON from /api/*; an HTML crash page would
        # arrive as a string and break the UI silently.  Log the cause with a
        # traceback, calling out database failures separately.
        original = getattr(error, "original_exception", None) or error
        kind = "Database error" if isinstance(original, sqlite3.Error) else "Internal error"
        logger.error(
            "%s on %s: %s",
            kind,
            request.path,
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal error"}), 500
        return "Internal server error", 500

    return app


def main() -> None:
    """Console entry point (``meshgraph`` / ``uv run meshgraph``)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    store_ref = get_settings_store()
    settings = store_ref.get()
    app = create_app(store_ref)

    logger.info(
        "meshgraph %s → http://%s:%s  (settings: %s)",
        __version__,
        settings.web_host,
        settings.web_port,
        store_ref.path,
    )
    # threaded=True: API and polling requests must not queue behind each
    # other while the MQTT worker threads run in this same process.  The
    # reloader stays off deliberately — a Werkzeug reload restarts the whole
    # process and would duplicate the capture (two subscriptions racing the
    # same database), so keep exactly one worker alive (G-P2-4).
    app.run(
        host=settings.web_host,
        port=int(settings.web_port),
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
