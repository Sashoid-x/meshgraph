#!/usr/bin/env python3
"""Publish a synthetic Meshtastic scene to an MQTT broker.

Feeds the graph with enough data to exercise **both** modes:

``traceroute``
    ``TRACEROUTE_APP`` packets with ``RouteDiscovery`` payloads (forward hops,
    a direct hop and a return path).

``rssi``
    Direct receptions (``hop_start == hop_limit``) from every node through a
    gateway, plus one relayed packet that the RSSI mode must ignore.

Also publishes node names, positions and one AES-256-CTR encrypted packet so
the decryption path is covered.  Usage::

    uv run python scripts/demo_traffic.py                # one shot
    uv run python scripts/demo_traffic.py --interval 5   # keep it alive
"""

from __future__ import annotations

import argparse
import base64
import random
import sys
import threading
import time

import paho.mqtt.client as mqtt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic import config_pb2, mesh_pb2, mqtt_pb2, portnums_pb2

from meshgraph.config import DEFAULT_CHANNEL_KEY

# id, long name, short name, role, lat, lon
NODES: list[tuple[int, str, str, str, float, float]] = [
    (0x1001, "Mayak", "MYK", "ROUTER", 55.751, 37.618),
    (0x1002, "Retranslator", "RTR", "CLIENT", 55.755, 37.625),
    (0x1003, "Shturman", "SHT", "CLIENT", 55.748, 37.630),
    (0x1004, "Kuryer", "KRY", "CLIENT", 55.744, 37.612),
    (0x1005, "Baza", "BZA", "ROUTER", 55.758, 37.610),
]
NODE_IDS = [n[0] for n in NODES]
GATEWAYS = [n[0] for n in NODES if n[3] == "ROUTER"]

BROADCAST = 0xFFFFFFFF


def hex_id(node_id: int) -> str:
    return f"!{node_id:08x}"


def encrypt_payload(data: mesh_pb2.Data, packet_id: int, sender_id: int, key: bytes) -> bytes:
    raw = data.SerializeToString()
    nonce = packet_id.to_bytes(8, "little") + sender_id.to_bytes(8, "little")
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(raw) + enc.finalize()


class Scene:
    """Builds ServiceEnvelope messages for one round of the demo."""

    def __init__(self, region: str, channel: str) -> None:
        self.region = region
        self.channel = channel
        self.packet_seq = random.randint(1, 0x7FFF)

    def next_id(self) -> int:
        self.packet_seq = (self.packet_seq + 1) & 0x7FFFFFFF or 1
        return self.packet_seq

    def topic(self, sender_id: int) -> str:
        return f"msh/{self.region}/2/e/{self.channel}/{hex_id(sender_id)}"

    def envelope(
        self,
        *,
        sender: int,
        gateway: int,
        data: mesh_pb2.Data,
        destination: int = BROADCAST,
        hop_start: int = 3,
        hop_limit: int = 3,
        snr: float = 6.5,
        rssi: int = -95,
        encrypt: bool = False,
        packet_id: int | None = None,
    ) -> tuple[str, bytes]:
        env = mqtt_pb2.ServiceEnvelope()
        env.channel_id = self.channel
        env.gateway_id = hex_id(gateway)
        packet = env.packet
        setattr(packet, "from", sender)
        packet.to = destination
        packet.id = packet_id if packet_id is not None else self.next_id()
        packet.rx_snr = snr
        packet.rx_rssi = rssi
        packet.hop_start = hop_start
        packet.hop_limit = hop_limit
        packet.rx_time = int(time.time())

        if encrypt:
            key = base64.b64decode(DEFAULT_CHANNEL_KEY)
            packet.encrypted = encrypt_payload(data, packet.id, sender, key)
        else:
            packet.decoded.CopyFrom(data)

        return self.topic(sender), env.SerializeToString()

    # --- payload helpers -------------------------------------------------

    @staticmethod
    def nodeinfo(node_id: int, long_name: str, short_name: str, role: str) -> mesh_pb2.Data:
        user = mesh_pb2.User(
            id=hex_id(node_id),
            long_name=long_name,
            short_name=short_name,
            hw_model=mesh_pb2.HardwareModel.TBEAM,
        )
        user.role = getattr(config_pb2.Config.DeviceConfig.Role, role)
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.NODEINFO_APP)
        data.payload = user.SerializeToString()
        return data

    @staticmethod
    def position(node_id: int, lat: float, lon: float) -> mesh_pb2.Data:
        pos = mesh_pb2.Position(
            latitude_i=int(round(lat * 1e7)),
            longitude_i=int(round(lon * 1e7)),
            altitude=120,
        )
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.POSITION_APP)
        data.payload = pos.SerializeToString()
        return data

    @staticmethod
    def traceroute(
        route: list[int],
        snr_towards: list[float],
        route_back: list[int] | None = None,
        snr_back: list[float] | None = None,
    ) -> mesh_pb2.Data:
        msg = mesh_pb2.RouteDiscovery()
        msg.route.extend(route)
        # Protobuf stores SNR in quarter-dB units.
        msg.snr_towards.extend(int(round(s * 4)) for s in snr_towards)
        if route_back:
            msg.route_back.extend(route_back)
        if snr_back:
            msg.snr_back.extend(int(round(s * 4)) for s in snr_back)
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TRACEROUTE_APP)
        data.payload = msg.SerializeToString()
        return data

    @staticmethod
    def text(text: str, reply_id: int = 0, emoji: int = 0) -> mesh_pb2.Data:
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        data.payload = text.encode("utf-8")
        if reply_id:
            data.reply_id = reply_id
        if emoji:
            data.emoji = emoji
        return data


def build_messages(scene: Scene) -> list[tuple[str, bytes]]:
    """One full round: names, positions, traceroutes, receptions, crypto."""
    out: list[tuple[str, bytes]] = []

    # 1. Names and coordinates (side data → nodes table).
    for node_id, long_name, short_name, role, lat, lon in NODES:
        gateway = node_id if node_id in GATEWAYS else GATEWAYS[0]
        out.append(
            scene.envelope(
                sender=node_id,
                gateway=gateway,
                data=scene.nodeinfo(node_id, long_name, short_name, role),
            )
        )
        out.append(
            scene.envelope(
                sender=node_id,
                gateway=gateway,
                data=scene.position(node_id, lat, lon),
                snr=7.25,
                rssi=-88,
            )
        )

    # 2. Traceroutes: unicast requests, so the last hop is a real node link.
    traceroutes = [
        # (sender, destination, gateway, payload, hop_start, hop_limit)
        (0x1003, 0x1001, GATEWAYS[0], scene.traceroute([0x1002], [9.5, 6.5]), 4, 2),
        (
            0x1004,
            0x1001,
            GATEWAYS[1],
            scene.traceroute([0x1003, 0x1002], [7.5, 5.5, 4.5]),
            4,
            1,
        ),
        (0x1005, 0x1001, GATEWAYS[0], scene.traceroute([], [8.5]), 3, 2),
        (
            0x1004,
            0x1001,
            GATEWAYS[0],
            scene.traceroute([0x1003], [7.0, 6.0], route_back=[0x1002], snr_back=[5.0, 4.0]),
            5,
            1,
        ),
    ]
    for sender, destination, gateway, payload, hop_start, hop_limit in traceroutes:
        out.append(
            scene.envelope(
                sender=sender,
                gateway=gateway,
                data=payload,
                destination=destination,
                hop_start=hop_start,
                hop_limit=hop_limit,
                snr=5.5,
                rssi=-101,
            )
        )

    # 3. Direct receptions (RSSI mode). Every node hears through a gateway.
    for node_id, long_name, *_ in NODES:
        if node_id in GATEWAYS:
            continue
        gateway = GATEWAYS[node_id % len(GATEWAYS)]
        out.append(
            scene.envelope(
                sender=node_id,
                gateway=gateway,
                data=scene.text(f"ping from {long_name}"),
                hop_start=3,
                hop_limit=3,
                snr=round(random.uniform(1.0, 9.5), 2),
                rssi=random.randint(-115, -70),
            )
        )

    # A relayed packet: hop budget already spent, must not become an RSSI edge.
    out.append(
        scene.envelope(
            sender=0x1004,
            gateway=GATEWAYS[0],
            data=scene.text("relayed hop"),
            hop_start=3,
            hop_limit=2,
            snr=2.5,
            rssi=-108,
        )
    )

    # 4. One AES-encrypted packet on the default channel key.
    out.append(
        scene.envelope(
            sender=0x1002,
            gateway=GATEWAYS[0],
            data=scene.text("encrypted hello"),
            hop_start=3,
            hop_limit=3,
            snr=8.0,
            rssi=-84,
            encrypt=True,
        )
    )

    # 5. Chat: a message, an answer to it and an emoji reaction — the three
    # shapes the chat window renders (text, quote, reaction pill).
    hello_id = scene.next_id()
    out.append(
        scene.envelope(
            sender=0x1003,
            gateway=GATEWAYS[0],
            data=scene.text("Всем привет! Как там погода?"),
            packet_id=hello_id,
        )
    )
    out.append(
        scene.envelope(
            sender=0x1004,
            gateway=GATEWAYS[1],
            data=scene.text("Дубак, −15 и ветер", reply_id=hello_id),
        )
    )
    out.append(
        scene.envelope(
            sender=0x1005,
            gateway=GATEWAYS[0],
            data=scene.text("👍", reply_id=hello_id, emoji=1),
        )
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--region", default="US", help="topic region segment")
    parser.add_argument("--channel", default="LongFast", help="channel name segment")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="seconds between rounds (0 = publish once and exit)",
    )
    args = parser.parse_args()

    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or str(reason_code) in ("Success", "Normal Connection"):
            connected.set()
        else:
            print(f"broker refused connection: {reason_code}", file=sys.stderr)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="meshgraph-demo")
    client.on_connect = on_connect
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    if not connected.wait(timeout=10):
        print(f"no CONNACK from {args.host}:{args.port}", file=sys.stderr)
        client.loop_stop()
        return 1

    scene = Scene(region=args.region, channel=args.channel)
    try:
        while True:
            messages = build_messages(scene)
            for topic, payload in messages:
                info = client.publish(topic, payload, qos=0)
                info.wait_for_publish(timeout=5)
            print(f"published {len(messages)} messages on msh/{args.region}/2/e/{args.channel}")
            if args.interval <= 0:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
