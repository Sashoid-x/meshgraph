"""HTTP surface: page, graph API, status and the settings endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshgraph import store, web
from meshgraph.decoder import DecodedPacket

from .conftest import make_packet

NODE_A, NODE_B = 0x1001, 0x1002
GATEWAY = 0x2001


@pytest.fixture
def client(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "web.db"))
    app = web.create_app(settings_store, start_worker=False)
    app.testing = True
    return app.test_client()


@pytest.fixture(autouse=True)
def _seed(client):
    """Put one traceroute and one direct reception into the test database."""
    from meshgraph import graph

    graph.invalidate_cache()
    db_file = client.application.extensions["meshgraph_settings"].get().db_file

    from meshtastic import mesh_pb2

    route = mesh_pb2.RouteDiscovery()
    route.route.append(NODE_B)
    route.snr_towards.append(44)

    store.insert_packet(
        db_file,
        make_packet(
            from_node_id=NODE_A,
            to_node_id=0x8888,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=route.SerializeToString(),
        ),
    )
    store.insert_packet(
        db_file,
        make_packet(
            from_node_id=NODE_B,
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )
    graph.invalidate_cache()
    yield


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def test_index_serves_the_graph_page(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Граф сети Meshtastic" in body
    assert "settingsBtn" in body
    assert "d3.v7.min.js" in body
    # Чат — часть страницы, не отдельный роут.
    assert 'id="chatWindow"' in body
    assert 'id="chatList"' in body


def test_index_embeds_defaults_from_settings(client, settings_store):
    settings_store.update(default_graph_mode="rssi", default_hours=6)
    body = client.get("/").get_data(as_text=True)
    assert 'value="rssi"' in body
    assert 'value="6"' in body


def test_sidebar_sections_are_foldable(client):
    """Сворачиваются все разделы, кроме помеченных data-nofold."""
    body = client.get("/").get_data(as_text=True)
    for section_id in ("secModes", "secFilters", "secStats", "secLegend"):
        assert f'id="{section_id}"' in body
    # Компактные разделы («Поиск узла», «Выделено») не сворачиваются.
    assert body.count("data-nofold") == 2
    assert '<section class="panel" id="secSearch" data-nofold>' in body
    assert '<section class="panel" id="detailsPanel" data-nofold hidden>' in body
    # Разметка под кликабельные заголовки: h2.panel-title у каждой секции.
    assert body.count('class="panel-title"') >= 6
    # app.js действительно пропускает секции с data-nofold.
    app_js = (
        Path(__file__).resolve().parents[1] / "meshgraph" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert 'hasAttribute("data-nofold")' in app_js


def test_theme_toggle(client):
    """Переключатель темы: кнопка в шапке, выбор до отрисовки, светлый блок CSS."""
    body = client.get("/").get_data(as_text=True)
    assert 'id="themeBtn"' in body
    assert "theme-icon-sun" in body and "theme-icon-moon" in body
    # Тема применяется в <head> до отрисовки — без мигания при загрузке.
    assert "meshgraph.theme" in body
    assert "document.documentElement.dataset.theme" in body

    root = Path(__file__).resolve().parents[1] / "meshgraph"
    css = (root / "static" / "style.css").read_text(encoding="utf-8")
    assert ':root[data-theme="light"]' in css
    assert "--canvas-bg: #0a0d13" in css  # тёмная канва…
    assert "--canvas-bg: #eef1f6" in css  # …и светлая отличаются

    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "function initTheme()" in app_js
    assert "storeSet(THEME_KEY, theme)" in app_js  # выбор запоминается


def test_chat_endpoint_carries_pixel_art(client):
    from .test_pixelart import GOLDEN_PAYLOAD

    db_file = client.application.extensions["meshgraph_settings"].get().db_file
    store.insert_packet(
        db_file,
        make_packet(
            from_node_id=NODE_A,
            portnum_name="PRIVATE_APP",
            mesh_packet_id=78,
            raw_payload=GOLDEN_PAYLOAD,
        ),
    )

    payload = client.get("/api/chat?hours=24").get_json()

    message = payload["messages"][0]
    assert message["text"] == ""
    image = message["image"]
    assert (image["w"], image["h"], image["theme"], image["grid"]) == (32, 48, 8, False)


def test_page_wires_pixel_art(client):
    body = client.get("/").get_data(as_text=True)
    assert "pixelart.js" in body

    root = Path(__file__).resolve().parents[1] / "meshgraph"
    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "function pixelArtEl(" in app_js
    assert "MESHGRAPH_PIXEL_PALETTES" in app_js
    assert "m.image" in app_js  # подпись чата учитывает картинку

    pixel_js = (root / "static" / "pixelart.js").read_text(encoding="utf-8")
    assert pixel_js.count("name:") == 24  # все палитры на месте


# ---------------------------------------------------------------------------
# Graph API
# ---------------------------------------------------------------------------

def test_graph_api_returns_both_modes(client):
    traceroute = client.get("/api/graph?mode=traceroute&hours=24").get_json()
    assert traceroute["mode"] == "traceroute"
    assert {n["id"] for n in traceroute["nodes"]} == {NODE_A, NODE_B}
    assert len(traceroute["links"]) == 1
    assert traceroute["links"][0]["avg_snr"] == 11.0

    rssi = client.get("/api/graph?mode=rssi&hours=24").get_json()
    assert rssi["mode"] == "rssi"
    pairs = {frozenset((l["source"], l["target"])) for l in rssi["links"]}
    assert frozenset((GATEWAY, NODE_B)) in pairs


def test_graph_api_combined_mode(client):
    payload = client.get("/api/graph?mode=combined&hours=24").get_json()

    assert payload["mode"] == "combined"
    # The seed holds one traceroute (A–B) and one reception (GATEWAY–B).
    assert {n["id"] for n in payload["nodes"]} == {NODE_A, NODE_B, GATEWAY}
    pairs = {frozenset((l["source"], l["target"])) for l in payload["links"]}
    assert frozenset((NODE_A, NODE_B)) in pairs  # traceroute part
    assert frozenset((GATEWAY, NODE_B)) in pairs  # reception part
    assert payload["stats"]["mode"] == "combined"
    assert payload["stats"]["traceroute"]["mode"] == "traceroute"
    assert payload["stats"]["rssi"]["mode"] == "rssi"


def test_index_offers_the_combined_mode(client):
    body = client.get("/").get_data(as_text=True)
    assert 'value="combined"' in body  # radio in the mode switch
    assert '<option value="combined">Вместе</option>' in body  # settings select


def test_graph_api_accepts_filters(client):
    payload = client.get(
        "/api/graph?mode=traceroute&hours=6&min_snr=-10&include_indirect=true"
    ).get_json()
    assert payload["filters"]["hours"] == 6
    assert payload["filters"]["min_snr"] == -10.0
    assert payload["filters"]["include_indirect"] is True


def test_graph_api_survives_junk_query_params(client):
    payload = client.get(
        "/api/graph?mode=traceroute&hours=banana&min_snr=whatever"
    ).get_json()
    assert payload["filters"]["hours"] == 24
    assert payload["filters"]["min_snr"] == -200.0


def test_channels_endpoint(client):
    payload = client.get("/api/channels").get_json()
    assert payload["channels"] == ["LongFast"]


def test_chat_endpoint_lists_channel_messages(client):
    db_file = client.application.extensions["meshgraph_settings"].get().db_file
    store.insert_packet(
        db_file,
        make_packet(
            from_node_id=NODE_A,
            portnum_name="TEXT_MESSAGE_APP",
            mesh_packet_id=77,
            raw_payload="привет".encode(),
        ),
    )

    payload = client.get("/api/chat?hours=24").get_json()

    assert [m["text"] for m in payload["messages"]] == ["привет"]
    assert payload["messages"][0]["from"] == NODE_A
    assert payload["messages"][0]["reply_to"] is None
    assert "generated_at" in payload


def test_chat_endpoint_survives_junk_query_params(client):
    payload = client.get("/api/chat?hours=banana&limit=nope").get_json()

    assert payload["messages"] == []  # сид содержит только не-текстовые пакеты
    assert payload["generated_at"] > 0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_status_reports_broker_and_counters(client):
    payload = client.get("/api/status").get_json()
    assert payload["connected"] is False
    assert payload["broker"].startswith("127.0.0.1:")
    assert payload["keys_configured"] == 1
    assert payload["stats"]["messages"] == 0
    assert payload["stats"]["rate_1m"] == 0.0
    assert payload["stats"]["rate_5m"] == 0.0
    assert payload["db"]["packets"] == 2
    assert "version" in payload


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_are_masked_on_read(client):
    client.post("/api/settings", json={"mqtt_password": "hunter2"})
    payload = client.get("/api/settings").get_json()
    assert payload["mqtt_password"] == "••••••••"
    assert payload["mqtt_password_set"] is True
    assert "hunter2" not in str(payload)


def test_settings_save_writes_config_file(client, settings_store):
    response = client.post(
        "/api/settings",
        json={
            "mqtt_broker_address": "mqtt.lan",
            "mqtt_port": 1883,
            "mqtt_username": "ops",
            "mqtt_password": "pw",
            "decryption_keys": "1PG7OiApB1nwvP+rz05pAQ==",
            "default_graph_mode": "rssi",
            "default_hours": 12,
            "retention_hours": 0,
            "graph_packet_limit": 5000,
            "mqtt_topic_prefix": "msh",
            "mqtt_topic_suffix": "/+/+/+/#",
            "mqtt_client_id": "",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    reloaded = settings_store.get()
    assert reloaded.mqtt_broker_address == "mqtt.lan"
    assert reloaded.default_graph_mode == "rssi"
    assert settings_store.path.exists()


def test_settings_reject_invalid_port(client):
    response = client.post("/api/settings", json={"mqtt_port": 70000})
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert any("port" in e.lower() for e in errors)
    # Nothing was written.
    assert client.get("/api/settings").get_json()["mqtt_port"] == 1883


def test_settings_reject_unknown_field(client):
    response = client.post("/api/settings", json={"broker": "nope"})
    assert response.status_code == 400
    assert "Unknown field: broker" in response.get_json()["errors"]


def test_settings_reject_invalid_channel_key(client):
    response = client.post("/api/settings", json={"decryption_keys": "@@@"})
    assert response.status_code == 400
    assert any("base64" in e for e in response.get_json()["errors"])


def test_settings_validate_endpoint(client):
    ok = client.post("/api/settings/validate", json={"mqtt_broker_address": "a.b"})
    assert ok.get_json() == {"ok": True, "errors": []}

    bad = client.post("/api/settings/validate", json={"mqtt_broker_address": ""})
    assert bad.get_json()["ok"] is False


def test_password_mask_is_ignored_on_save(client):
    client.post("/api/settings", json={"mqtt_password": "real-password"})
    client.post("/api/settings", json={"mqtt_password": "••••••••"})
    assert client.get("/api/settings").get_json()["mqtt_password_set"] is True
    assert settings_store_password(client) == "real-password"


def settings_store_password(client) -> str:
    return client.application.extensions["meshgraph_settings"].get().mqtt_password


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_unknown_api_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json() == {"error": "not found"}
