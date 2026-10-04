"""Flask application: the graph page, its API and the settings endpoint."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import __version__, chat, graph, store
from .config import GRAPH_MODES, Settings, SettingsStore, get_settings_store, validate
from .mqtt_worker import CaptureWorker

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(
    settings_store: SettingsStore | None = None, start_worker: bool = True
) -> Flask:
    store_ref = settings_store or get_settings_store()
    settings = store_ref.get()
    store.ensure_ready(settings)

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

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        payload = worker.status()
        payload["version"] = __version__
        payload["settings_file"] = str(store_ref.path)
        return jsonify(payload)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(store_ref.get().masked())

    @app.post("/api/settings")
    def api_settings_post():
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
        return jsonify(
            {
                "ok": True,
                "settings": updated.masked(),
                "reconnect": "reconnecting with the new broker settings",
            }
        )

    @app.post("/api/settings/validate")
    def api_settings_validate():
        body = request.get_json(silent=True) or {}
        candidate = store_ref.get()
        for key, value in body.items():
            if key in Settings.__dataclass_fields__ and value is not None:
                setattr(candidate, key, value)
        errors = validate(candidate)
        return jsonify({"ok": not errors, "errors": errors})

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return "Not found", 404

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
    # Use the Werkzeug server: the MQTT worker is a thread in this process, so
    # a single threaded server with the reloader disabled is what we want.
    app.run(
        host=settings.web_host,
        port=int(settings.web_port),
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
