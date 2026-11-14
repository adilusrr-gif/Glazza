"""Opt-in real MQTT/TCP smoke using a synthetic Frigate publisher.

Run: .venv/bin/python tests/restaurant_mqtt_smoke.py
Requires optional amqtt==0.12.2 in the verification environment only.
This validates protocol transport, not physical camera/detector accuracy.
"""
import asyncio
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from amqtt.broker import Broker
import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.restaurant_feed import RestaurantFeed


ZONE = "restaurant_table_one"


class Service:
    def __init__(self):
        self.tables = []
        self.latest = None

    def configuration_tables(self):
        return self.tables

    def observe(self, camera, counts, *, available, now):
        self.latest = {"camera": camera, "counts": counts, "available": available}

    def set_health(self, health):
        self.health = health


async def until(predicate, description, action=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if action:
            action()
        if predicate():
            return
        await asyncio.sleep(0.025)
    raise AssertionError("Timed out: " + description)


async def run():
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:0"}},
        "sys_interval": 0,
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    })
    await broker.start()
    port = broker._servers["default"].instance.sockets[0].getsockname()[1]
    settings = SimpleNamespace(mqtt_host="127.0.0.1", mqtt_port=port, mqtt_prefix="frigate", demo=False)
    nvr = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="synthetic-frigate")
    fixture = {"connected": False, "detect": True, "objects": [
        {"id": "guest-one", "label": "person", "current_zones": [ZONE], "stationary": True, "active": False}
    ]}
    requests = []

    def respond():
        nvr.publish("frigate/camera_activity", json.dumps({"dining": {
            "objects": fixture["objects"], "config": {"enabled": True, "detect": fixture["detect"]}
        }}), qos=1, retain=False)

    def connected(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe("frigate/onConnect", qos=1)
            for topic, value in {
                "frigate/available": "online",
                "frigate/dining/enabled/state": "ON",
                "frigate/dining/detect/state": "ON",
                f"frigate/dining/zone/{ZONE}/state": "ON",
            }.items():
                client.publish(topic, value, qos=1, retain=True)
            fixture["connected"] = True

    def message(client, userdata, packet):
        if packet.topic == "frigate/onConnect":
            requests.append(packet.topic)
            respond()

    nvr.on_connect, nvr.on_message = connected, message
    nvr.connect_async("127.0.0.1", port)
    nvr.loop_start()
    feed = RestaurantFeed(settings)
    service = Service()
    signal = lambda camera, **kwargs: {"signal": "online"}

    def pump():
        feed.pump(service, signal)

    def observed(count):
        return service.latest and service.latest["available"] and service.latest["counts"] == {ZONE: count}

    try:
        await until(lambda: fixture["connected"], "synthetic NVR connects")
        feed.start()  # Deliberately connect before any tables are configured.
        await until(lambda: feed.connected and feed.online, "feed connects with no configured tables")
        service.tables = [{"camera": "dining", "zone": ZONE}]
        await until(lambda: observed(1), "late table binding + retained zone replay + stationary snapshot", pump)
        assert requests, "Feed did not request a complete current snapshot"

        info = nvr.publish(f"frigate/{ZONE}/person/active", "0", qos=1, retain=False)
        await asyncio.to_thread(info.wait_for_publish, 3)
        await asyncio.sleep(0.05)
        pump()
        assert observed(1), "Inactive/stationary guest was lost"

        fixture["objects"] = [*fixture["objects"], {"id": "guest-two", "label": "person", "current_zones": [ZONE]}]
        nvr.publish(f"frigate/{ZONE}/person", "2", qos=1, retain=False)
        await until(lambda: observed(2), "live non-retained zone count reaches service", pump)

        fixture["detect"] = False
        nvr.publish("frigate/dining/detect/state", "OFF", qos=1, retain=True)
        await until(lambda: service.latest and not service.latest["available"], "disabled detection becomes unknown", pump)
        assert service.latest["counts"] == {}

        fixture["detect"] = True
        fixture["objects"] = []
        nvr.publish("frigate/dining/detect/state", "ON", qos=1, retain=True)
        respond()
        await until(lambda: observed(0), "complete current empty snapshot", pump)

        # No retained counts exist. A process restart must recover from the
        # explicit snapshot request and replayed configuration alone.
        await asyncio.to_thread(feed.close)
        feed = RestaurantFeed(settings)
        feed.set_tables(service.tables)
        pump()
        assert service.latest["available"] is False
        feed.start()
        await until(lambda: observed(0), "restart bootstrap without retained counts", pump)

        nvr.publish("frigate/available", "offline", qos=1, retain=True)
        await until(lambda: service.latest and not service.latest["available"], "offline NVR becomes unknown", pump)
        assert service.latest["counts"] == {}
        return {"result": "PASS", "protocol": "real TCP MQTT", "source": "synthetic Frigate publisher",
                "checks": ["late configuration retained replay", "stationary people", "active ignored",
                           "live zone counts", "detection disabled", "empty snapshot", "restart bootstrap", "NVR offline"],
                "snapshot_requests": len(requests)}
    finally:
        await asyncio.to_thread(feed.close)
        nvr.disconnect()
        await asyncio.to_thread(nvr.loop_stop)
        await broker.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
