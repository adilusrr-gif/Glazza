"""Protocol and integration boundaries for live restaurant observations.

Frigate zone counts are change-only, not retained. A room cannot be called empty
from a missing count, an old event, a frozen snapshot, or disabled detection.
"""
from dataclasses import replace
import asyncio
import json

import httpx
import paho.mqtt.client as mqtt
import pytest
import yaml
from fastapi.testclient import TestClient

from app.frigate import make_config
from app.main import create_app
from app.restaurant_feed import RestaurantFeed


POINTS = [[0.1, 0.2], [0.4, 0.2], [0.4, 0.7], [0.1, 0.7]]
TABLE = {"id": "table-one", "camera": "dining", "zone": "restaurant_table_one", "points": POINTS}


class Observations:
    def __init__(self, tables=None):
        self.tables = [dict(TABLE)] if tables is None else tables
        self.observations = []
        self.health = None

    def configuration_tables(self):
        return self.tables

    def observe(self, camera, counts, *, available, now):
        self.observations.append({"camera": camera, "counts": counts, "available": available, "now": now})

    def set_health(self, health):
        self.health = health


def signal(camera, *, now):
    return {"signal": "online"}


def connected_feed(settings, service=None, now=100):
    service = service or Observations()
    feed = RestaurantFeed(replace(settings, mqtt_host="mqtt"))
    feed.set_tables(service.tables)
    feed.transport(True)
    feed.message("frigate/available", "online", retained=True, now=now)
    for table in service.tables:
        camera, zone = table["camera"], table["zone"]
        feed.message(f"frigate/{camera}/zone/{zone}/state", "ON", retained=True, now=now)
        feed.message(f"frigate/{camera}/enabled/state", "ON", retained=True, now=now)
        feed.message(f"frigate/{camera}/detect/state", "ON", retained=True, now=now)
    return feed, service


def snapshot(feed, objects, now=100, *, camera="dining", config=None, retained=False):
    payload = {camera: {"objects": objects, "config": config or {"enabled": True, "detect": True}}}
    feed.message("frigate/camera_activity", json.dumps(payload), now=now, retained=retained)


def person(*zones, **other):
    return {"id": "guest-1", "label": "person", "current_zones": list(zones), **other}


def test_bootstrap_requires_complete_current_snapshot_not_missing_or_retained_counts(settings):
    feed, service = connected_feed(settings)
    feed.message("frigate/restaurant_table_one/person", "0", retained=True, now=100)
    feed.pump(service, signal, now=100)
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}

    # Even a fresh non-retained count does not establish a full camera snapshot.
    feed.message("frigate/restaurant_table_one/person", "0", now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["available"] is False

    snapshot(feed, [], now=102)
    feed.pump(service, signal, now=102)
    assert service.observations[-1] == {
        "camera": "dining", "counts": {TABLE["zone"]: 0}, "available": True, "now": 102
    }


def test_stationary_people_count_and_only_current_seating_zones_apply(settings):
    feed, service = connected_feed(settings)
    snapshot(feed, [
        person(TABLE["zone"], stationary=True, active=False),
        person("aisle", id="past-guest", entered_zones=[TABLE["zone"]]),
        person(TABLE["zone"], id="false-positive", false_positive=True),
        {"label": "cat", "current_zones": [TABLE["zone"]]},
    ])
    feed.pump(service, signal, now=100)
    assert service.observations[-1]["counts"] == {TABLE["zone"]: 1}

    # Activity-only counts and historical events cannot empty an occupied table.
    feed.message(f"frigate/{TABLE['zone']}/person/active", "0", now=101)
    feed.message("frigate/events", json.dumps({"type": "end", "after": {
        "camera": "dining", "label": "person", "entered_zones": [TABLE["zone"]]
    }}), now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["counts"] == {TABLE["zone"]: 1}


def test_count_changes_work_but_retained_counts_and_snapshots_cannot_revive_old_state(settings):
    feed, service = connected_feed(settings)
    snapshot(feed, [person(TABLE["zone"])])
    feed.message(f"frigate/{TABLE['zone']}/person", "2", now=101)
    feed.message(f"frigate/{TABLE['zone']}/person", "0", retained=True, now=102)
    snapshot(feed, [], now=102, retained=True)
    feed.pump(service, signal, now=102)
    assert service.observations[-1]["counts"] == {TABLE["zone"]: 2}

    # An active broker/stream does not make an old camera snapshot current.
    feed.message(f"frigate/{TABLE['zone']}/person", "3", now=120)
    feed.pump(service, signal, now=120)
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}


@pytest.mark.parametrize("state_topic", ["dining/detect/state", "dining/enabled/state"])
def test_disabled_camera_or_detection_is_unknown_not_empty(settings, state_topic):
    feed, service = connected_feed(settings)
    snapshot(feed, [])
    feed.message("frigate/" + state_topic, "OFF", retained=True, now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}


def test_disabled_or_unconfirmed_zone_is_omitted_not_zero(settings):
    feed, service = connected_feed(settings)
    snapshot(feed, [])
    feed.message(f"frigate/dining/zone/{TABLE['zone']}/state", "OFF", now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["counts"] == {}

    # A similarly named zone on another camera cannot enable this table.
    feed.message(f"frigate/other_camera/zone/{TABLE['zone']}/state", "ON", retained=True, now=102)
    feed.pump(service, signal, now=102)
    assert service.observations[-1]["counts"] == {}


@pytest.mark.parametrize("camera_status", ["offline", "unknown"])
def test_fresh_mqtt_cannot_override_missing_live_camera_signal(settings, camera_status):
    feed, service = connected_feed(settings)
    snapshot(feed, [])
    feed.pump(service, lambda camera, **kwargs: {"signal": camera_status}, now=100)
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}


@pytest.mark.parametrize("payload", [
    {}, {"dining": {}}, {"dining": {"objects": None}},
    {"dining": {"objects": [{"label": "person", "entered_zones": [TABLE["zone"]]}]}},
    {"dining": {"objects": [{"label": "person", "current_zones": "not-a-list"}]}},
])
def test_missing_or_invalid_camera_snapshot_invalidates_previous_observation(settings, payload):
    feed, service = connected_feed(settings)
    snapshot(feed, [person(TABLE["zone"])])
    feed.message("frigate/camera_activity", json.dumps(payload), now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}


@pytest.mark.parametrize("payload", ["-1", "1001", "2.5", "true", "", b"\xff"])
def test_malformed_counts_cannot_replace_known_occupancy(settings, payload):
    feed, service = connected_feed(settings)
    snapshot(feed, [person(TABLE["zone"])])
    feed.message(f"frigate/{TABLE['zone']}/person", payload, now=101)
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["counts"] == {TABLE["zone"]: 1}


def test_transport_reset_and_config_invalidation_require_fresh_observation(settings):
    feed, service = connected_feed(settings)
    snapshot(feed, [person(TABLE["zone"])])
    feed.invalidate()
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["available"] is False

    snapshot(feed, [], now=102)
    feed.transport(False)
    feed.pump(service, signal, now=103)
    assert service.observations[-1]["available"] is False
    # Traffic arriving after disconnect is not allowed to restore occupancy.
    snapshot(feed, [], now=103)
    feed.transport(True)
    feed.message("frigate/available", "online", now=104)
    feed.pump(service, signal, now=104)
    assert service.observations[-1]["available"] is False


def test_rebinding_zone_does_not_reuse_another_cameras_count(settings):
    feed, service = connected_feed(settings)
    snapshot(feed, [person(TABLE["zone"])])
    service.tables = [{**TABLE, "camera": "terrace"}]
    feed.pump(service, signal, now=101)
    assert service.observations[-1]["camera"] == "terrace"
    assert service.observations[-1]["available"] is False
    assert service.observations[-1]["counts"] == {}


def test_snapshot_requests_are_read_only_and_repeat_without_browser(settings):
    feed, service = connected_feed(settings)

    class Client:
        def __init__(self):
            self.published = []

        def publish(self, *args, **kwargs):
            self.published.append((args, kwargs))

    client = Client()
    feed.client = client
    feed.pump(service, signal, now=100)
    feed.pump(service, signal, now=104)
    feed.pump(service, signal, now=105)
    assert client.published == [
        (("frigate/onConnect", ""), {"qos": 1, "retain": False}),
        (("frigate/onConnect", ""), {"qos": 1, "retain": False}),
    ]


def test_late_table_binding_replays_retained_zone_state_without_nvr_restart(settings):
    service = Observations(tables=[])
    feed = RestaurantFeed(replace(settings, mqtt_host="mqtt"))

    class RetainedBroker:
        retained = {
            "frigate/available": "online",
            f"frigate/dining/zone/{TABLE['zone']}/state": "ON",
        }

        def subscribe(self, filters, qos=0):
            if isinstance(filters, str):
                filters = [(filters, qos)]
            for pattern, _qos in filters:
                for topic, value in self.retained.items():
                    if mqtt.topic_matches_sub(pattern, topic):
                        feed.message(topic, value, retained=True, now=100)

        def publish(self, topic, value, **kwargs):
            assert topic == "frigate/onConnect"
            # Pinned Frigate replies with camera_activity, not zone settings.
            snapshot(feed, [person(TABLE["zone"], stationary=True)])

    client = RetainedBroker()
    feed.client = client
    feed._on_connect(client, None, None, 0, None)
    service.tables = [dict(TABLE)]
    feed.pump(service, signal, now=100)
    assert service.observations[-1]["available"] is True
    assert service.observations[-1]["counts"] == {TABLE["zone"]: 1}


def test_restaurant_zones_are_exported_separately_from_security_zones(camera):
    table = {**TABLE, "camera": camera["id"]}
    config = make_config([camera], restaurant_tables=[table], mqtt_config={
        "enabled": True, "host": "mqtt", "port": 1883, "topic_prefix": "frigate"
    })
    exported = config["cameras"][camera["id"]]["zones"]
    assert set(exported) == {"restricted", TABLE["zone"]}
    assert exported[TABLE["zone"]]["objects"] == ["person"]
    assert exported[TABLE["zone"]]["coordinates"] == "0.1,0.2,0.4,0.2,0.4,0.7,0.1,0.7"
    assert config["mqtt"]["enabled"] is True
    assert all(z["name"] != TABLE["zone"] for z in camera["zones"])


@pytest.mark.parametrize("changed", [
    {"zone": "restricted"}, {"zone": "entrance"}, {"camera": "missing"}
])
def test_invalid_table_config_cannot_overwrite_security_or_other_camera(camera, changed):
    table = {**TABLE, "camera": camera["id"], **changed}
    with pytest.raises(ValueError):
        make_config([camera], restaurant_tables=[table])


def test_camera_assignment_and_deletion_guards_apply_to_both_routes(logged, app, camera, monkeypatch):
    assert logged.post("/api/cameras", json=camera).status_code == 201
    org = logged.post("/api/platform/organizations", json={"name": "Ресторанная сеть"}).json()["id"]
    site = logged.post("/api/platform/sites", json={
        "name": "Другой ресторан", "organization_id": org, "profile": "restaurant"
    })
    assert site.status_code == 201, site.text
    new_site = site.json()["id"]
    monkeypatch.setattr(app.state.engine.restaurant, "camera_in_use", lambda camera_id: camera_id == camera["id"])

    assert logged.delete("/api/cameras/entrance").status_code == 409
    assert logged.put("/api/platform/cameras/entrance/site", json={"site_id": new_site}).status_code == 409
    assert logged.put("/api/cameras/entrance", json={**camera, "site_id": new_site}).status_code == 409
    assert logged.get("/api/cameras").json()[0]["site_id"] == "default"
    # A routine camera edit by an older client does not move the table's site.
    assert logged.put("/api/cameras/entrance", json=camera).status_code == 200


def test_apply_rejects_tables_without_mqtt(logged, app, camera, settings, monkeypatch):
    assert logged.post("/api/cameras", json=camera).status_code == 201
    tables = [{**TABLE, "camera": camera["id"]}]
    monkeypatch.setattr(app.state.engine.restaurant, "configuration_tables", lambda: tables)
    assert logged.post("/api/config/apply").status_code == 422
    assert not settings.frigate_config.exists()


def test_apply_saves_restaurant_zones_and_mqtt_and_invalidates_observations(settings, camera, monkeypatch):
    app = create_app(replace(settings, mqtt_host="mqtt"), background=False)
    asyncio.run(app.state.engine.client.aclose())
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    tables = [{**TABLE, "camera": camera["id"]}]
    monkeypatch.setattr(app.state.engine.restaurant, "configuration_tables", lambda: tables)
    feed = app.state.engine.restaurant_feed
    feed.counts[TABLE["zone"]] = 1
    feed.snapshot_at[camera["id"]] = 100
    with TestClient(app) as client:
        login = client.post("/api/login", json={"username": settings.username, "password": settings.password})
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        assert client.post("/api/cameras", json=camera).status_code == 201
        result = client.post("/api/config/apply")
        assert result.status_code == 200, result.text
        config = yaml.safe_load(settings.frigate_config.read_text())
        assert config["mqtt"] == {"enabled": True, "host": "mqtt", "port": 1883,
                                  "topic_prefix": "frigate", "client_id": "glazza-frigate"}
        assert TABLE["zone"] in config["cameras"][camera["id"]]["zones"]
        assert feed.snapshot_at == {}
        assert feed.counts == {}

