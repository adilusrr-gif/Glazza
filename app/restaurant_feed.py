"""Live restaurant occupancy from Frigate MQTT, never historical event zones.

Frigate 0.18.2 publishes zone counts only on change (not retained). A periodic
onConnect request supplies a complete camera_activity snapshot, including
stationary people, after API/broker restarts. Unknown/missing data stays unknown.
"""
import json
import math
import re
import threading
import time
import uuid

import paho.mqtt.client as mqtt


class RestaurantFeed:
    SNAPSHOT_INTERVAL = 5
    SNAPSHOT_TTL = 15

    def __init__(self, settings):
        self.settings = settings
        self.prefix = settings.mqtt_prefix
        self.lock = threading.RLock()
        self.client = None
        self.connected = False
        self.online = False
        self.tables = {}
        self.counts = {}
        self.zone_enabled = {}
        self.camera_enabled = {}
        self.snapshot_at = {}
        self.last_message_at = None
        self.last_request = 0

    def set_tables(self, tables):
        replay = False
        with self.lock:
            current = {t['zone']: t['camera'] for t in tables}
            if current != self.tables:
                self.counts = {z: n for z, n in self.counts.items() if current.get(z) == self.tables.get(z)}
                self.zone_enabled = {z: v for z, v in self.zone_enabled.items() if current.get(z) == self.tables.get(z)}
                self.tables = current
                self.last_request = 0
                replay = bool(self.connected and self.client)
        if replay:
            # Retained zone settings may have arrived before a table/module was
            # configured. onConnect only sends camera_activity, not zone states.
            self.client.subscribe(f'{self.prefix}/+/zone/+/state', qos=1)

    def invalidate(self):
        with self.lock:
            self.counts.clear()
            self.snapshot_at.clear()
            self.last_request = 0

    def transport(self, connected):
        with self.lock:
            self.connected = connected
            self.online = False
            self.counts.clear()
            self.snapshot_at.clear()
            self.zone_enabled.clear()
            self.camera_enabled.clear()
            self.last_request = 0

    def start(self):
        if not self.settings.mqtt_host or self.settings.demo or self.client:
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id='glazza-restaurant-' + uuid.uuid4().hex[:12])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=15)
        client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=20)
        self.client = client
        client.loop_start()

    def close(self):
        client, self.client = self.client, None
        if client:
            client.disconnect()
            client.loop_stop()
        self.transport(False)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.transport(reason_code == 0)
        if reason_code == 0:
            client.subscribe([(f'{self.prefix}/available', 1),
                              (f'{self.prefix}/camera_activity', 1),
                              (f'{self.prefix}/+/person', 1),
                              (f'{self.prefix}/+/detect/state', 1),
                              (f'{self.prefix}/+/enabled/state', 1),
                              (f'{self.prefix}/+/zone/+/state', 1)])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.transport(False)

    def _on_message(self, client, userdata, message):
        self.message(message.topic, message.payload, retained=message.retain)

    def message(self, topic, payload, *, retained=False, now=None):
        """Bounded parser shared by the transport and protocol regression tests."""
        now = time.time() if now is None else now
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            return
        if not isinstance(payload, (bytes, str)) or len(payload) > 2_000_000:
            return
        try:
            value = payload.decode('utf-8') if isinstance(payload, bytes) else payload
        except UnicodeDecodeError:
            return
        if not topic.startswith(self.prefix + '/'):
            return
        parts = topic[len(self.prefix) + 1:].split('/')
        with self.lock:
            if not self.connected:
                return
            self.last_message_at = now
            if parts == ['available']:
                if value not in ('online', 'offline', 'stopped'):
                    return
                self.online = value == 'online'
                if not self.online:
                    self.counts.clear()
                    self.snapshot_at.clear()
                self.last_request = 0
                return
            if len(parts) == 4 and parts[1] == 'zone' and parts[3] == 'state':
                camera, _, zone, _ = parts
                if self.tables.get(zone) == camera and value in ('ON', 'OFF'):
                    self.zone_enabled[zone] = value == 'ON'
                return
            if len(parts) == 3 and parts[1] in ('detect', 'enabled') and parts[2] == 'state':
                if parts[0] in self.tables.values() and value in ('ON', 'OFF'):
                    self.camera_enabled[(parts[0], parts[1])] = value == 'ON'
                return
            if retained:
                # Neither counts nor camera_activity have source timestamps.
                # Never revive old observations from retained messages.
                return
            if len(parts) == 2 and parts[1] == 'person' and parts[0] in self.tables:
                if re.fullmatch(r'\d{1,4}', value) and int(value) <= 1000:
                    self.counts[parts[0]] = int(value)
                return
            if parts != ['camera_activity']:
                return
            try:
                cameras = json.loads(value)
            except (ValueError, TypeError):
                return
            if not isinstance(cameras, dict):
                return
            for camera in set(self.tables.values()):
                data = cameras.get(camera)
                # An empty initial camera record is not proof of an empty room.
                if not isinstance(data, dict) or not isinstance(data.get('objects'), list):
                    self.snapshot_at.pop(camera, None)
                    continue
                objects = data['objects']
                if len(objects) > 1000 or any(not isinstance(o, dict) or not isinstance(o.get('label'), str)
                                             or not isinstance(o.get('current_zones'), list)
                                             or any(not isinstance(z, str) for z in o['current_zones']) for o in objects):
                    self.snapshot_at.pop(camera, None)
                    continue
                config = data.get('config', {})
                if isinstance(config, dict):
                    for key in ('detect', 'enabled'):
                        if isinstance(config.get(key), bool):
                            self.camera_enabled[(camera, key)] = config[key]
                for zone, owner in self.tables.items():
                    if owner == camera:
                        self.counts[zone] = sum(1 for o in objects if o['label'] in ('person', 'person-verified')
                                                and not o.get('false_positive', False) and zone in o['current_zones'])
                self.snapshot_at[camera] = now

    def pump(self, service, camera_signal, now=None):
        now = time.time() if now is None else now
        tables = service.configuration_tables()
        self.set_tables(tables)
        with self.lock:
            if self.client and self.connected and tables and now - self.last_request >= self.SNAPSHOT_INTERVAL:
                self.last_request = now
                # This is Frigate's read-only request for current client state.
                self.client.publish(f'{self.prefix}/onConnect', '', qos=1, retain=False)
            connected, online = self.connected, self.online
            counts, enabled = dict(self.counts), dict(self.zone_enabled)
            camera_enabled, observed = dict(self.camera_enabled), dict(self.snapshot_at)
            last_message = self.last_message_at
        healthy = 0
        for camera in {t['camera'] for t in tables}:
            stamp = observed.get(camera)
            available = (connected and online and stamp is not None and 0 <= now - stamp <= self.SNAPSHOT_TTL
                         and camera_enabled.get((camera, 'enabled'), False)
                         and camera_enabled.get((camera, 'detect'), False)
                         and camera_signal(camera, now=now)['signal'] == 'online')
            values = {t['zone']: counts[t['zone']] for t in tables if t['camera'] == camera
                      and enabled.get(t['zone']) and t['zone'] in counts}
            healthy += int(bool(available and values))
            service.observe(camera, values if available else {}, available=bool(available), now=now)
        state = ('idle' if not tables else 'not_configured' if not self.settings.mqtt_host else
                 'offline' if not connected or not online else 'online' if healthy else 'waiting')
        service.set_health({'state': state, 'connected': connected, 'nvr_online': online,
                            'checked_at': now, 'last_message_at': last_message, 'cameras_observed': healthy})
