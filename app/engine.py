"""Durable ingestion and analysis queue. No simulated detections in live mode."""
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
import uuid

import httpx
from . import admission, retail, retail_multicam, visits
from .retail_appearance import MAX_IMAGE_BYTES, clothing_signature, live_crop_box
from .restaurant import RestaurantService
from .restaurant_feed import RestaurantFeed
from .platform import modules_for, camera_site

log = logging.getLogger('glazza.engine')
ACTION_MODELS = {'theft': {'vadclip'}, 'fight': {'fight', 'vadclip'}, 'face': {'face'}}
SAFE_ID = re.compile(r'^[a-zA-Z0-9_.-]{1,120}$')
RETAIL_TRACKERS = (retail_multicam, visits)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def zone_matches(live_zone, polygon):
    if not isinstance(live_zone, dict) or live_zone.get('enabled', True) is not True or not polygon:
        return False
    allowed = live_zone.get('objects', [])
    if not isinstance(allowed, list) or (allowed and 'person' not in allowed):
        return False
    raw = live_zone.get('coordinates')
    if not isinstance(raw, str):
        return False
    try:
        coords = [float(n) for n in raw.split(',')]
    except ValueError:
        return False
    expected = [v for point in polygon for v in point]
    return len(coords) == len(expected) and all(abs(a-b) < 0.00001 for a,b in zip(coords, expected))


class Engine:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        admission.initialize(store)
        retail.initialize(store)
        for tracker in RETAIL_TRACKERS:
            tracker.initialize(store)
        self.retail_watermark = None
        self.last_retail_replay = 0
        self.retail_frame_shapes = {}
        self.restaurant = RestaurantService(store, settings)
        self.restaurant_feed = RestaurantFeed(settings)
        self.health = {'frigate': 'starting', 'last_sync': None, 'error': None, 'analyzer': {'state': 'unavailable', 'models': []}}
        for module in ('fight', 'theft'):
            self.health[module + '_monitor'] = {'state': 'waiting', 'camera_ids': [], 'busy_cameras': [],
                'skipped_windows': 0, 'window_seconds': 12, 'stride_seconds': 6, 'recording_delay_seconds': 20}
        # Service-to-service traffic must not leave the Docker network via ambient proxies.
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self.tasks = []
        self.health['maintenance'] = {'state': 'waiting', 'error': None}
        self.health['background'] = {}
        self.camera_signal_snapshot = {'checked_at': None, 'updated_at': None, 'cameras': {}, 'reason': 'waiting'}

    async def start(self):
        self.store.execute("UPDATE jobs SET state='queued', updated=? WHERE state='running'", (time.time(),))
        self.restaurant_feed.set_tables(self.restaurant.configuration_tables())
        self.restaurant_feed.start()
        self.tasks = [asyncio.create_task(self.supervise(name, worker)) for name, worker in
                      [('poll',self.poll),('analysis',self.work),('cameras',self.poll_camera_signals),('admission',self.admission_work),
                       ('restaurant', self.restaurant_work), ('restaurant_delivery', self.restaurant_delivery),
                       ('retail_snapshots', self.retail_snapshot_work)]]

    async def restaurant_work(self):
        while True:
            if not self.settings.demo:
                now = time.time()
                self.restaurant_feed.pump(self.restaurant, self.camera_signal, now=now)
                self.restaurant.tick(now=now)
            await asyncio.sleep(1)

    async def restaurant_delivery(self):
        while True:
            if not self.settings.demo:
                await self.restaurant.deliver(self.client)
            await asyncio.sleep(2)

    async def supervise(self, name, worker):
        while True:
            self.health['background'][name] = 'running'
            try:
                await worker()
                raise RuntimeError('Background worker returned')
            except Exception as exc:
                self.health['background'][name] = 'restarting'
                if name == 'poll':
                    self.health.update(frigate='offline', error='Обработка событий перезапускается')
                log.exception('Background worker %s failed: %s', name, type(exc).__name__)
            await asyncio.sleep(1)

    def health_snapshot(self):
        health = dict(self.health)
        if self.tasks and (any(task.done() for task in self.tasks) or
                           time.time() - (self.health.get('poll_heartbeat') or 0) > max(60,self.settings.poll_seconds * 3)):
            health.update(frigate='offline', error='Обработка событий не отвечает')
        return health

    async def admission_work(self):
        while True:
            if not self.settings.demo:
                try:
                    await admission.process(self)
                except Exception as error:
                    log.warning('Admission processing failed: %s', type(error).__name__)
            await asyncio.sleep(5)

    async def sync_camera_signals(self):
        response = await self.client.get(self.settings.frigate_url + '/api/stats')
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('cameras'), dict):
            raise ValueError('Invalid camera statistics')
        service = payload.get('service')
        updated = service.get('last_updated') if isinstance(service, dict) else None
        cameras = {}
        for identifier, data in payload['cameras'].items():
            fps = data.get('camera_fps') if isinstance(data, dict) else None
            cameras[identifier] = fps if finite(fps) and fps >= 0 else None
        # Publish only measured FPS and timestamps; raw NVR statistics can contain secrets.
        self.camera_signal_snapshot = {'checked_at': time.time(), 'updated_at': updated if finite(updated) else None,
                                       'cameras': cameras, 'reason': None}

        await self.record_retail_health(payload, updated)

    def mark_retail_unavailable(self, now):
        self.retail_frame_shapes.clear()
        for tracker in RETAIL_TRACKERS:
            for camera in tracker.configured_cameras(self.store):
                tracker.record_health(self.store, camera, now, False)

    async def record_retail_health(self, stats, updated):
        now = time.time()
        scopes = [(tracker, tracker.configured_cameras(self.store)) for tracker in RETAIL_TRACKERS]
        if not any(cameras for _, cameras in scopes):
            self.retail_frame_shapes.clear()
            return
        if not finite(updated) or not 0 <= now - updated <= 30:
            self.mark_retail_unavailable(now)
            return
        try:
            response = await self.client.get(self.settings.frigate_url + '/api/config', timeout=5)
            response.raise_for_status()
            config = response.json()
            if not isinstance(config, dict) or not isinstance(config.get('cameras'), dict):
                raise ValueError('Missing live camera configuration')
        except (httpx.HTTPError, ValueError):
            self.mark_retail_unavailable(now)
            return
        # Retain only boolean health samples. The live configuration contains stream addresses.
        polygons = {c['id']: {z['name']: z['points'] for z in c['zones']} for c in self.store.cameras()}
        shapes = {}
        detectors = stats.get('detectors')
        functioning_detector = isinstance(detectors, dict) and any(
            isinstance(device, dict) and finite(device.get('pid')) and device['pid'] > 0
            and finite(device.get('detection_start'))
            and (device['detection_start'] == 0 or -1 <= updated - device['detection_start'] <= 10)
            for device in detectors.values())
        for tracker, cameras in scopes:
            zones = tracker.configured_zones(self.store)
            for camera in cameras:
                data = stats['cameras'].get(camera) or {}
                live = config['cameras'].get(camera) or {}
                camera_fps, process_fps = data.get('camera_fps'), data.get('process_fps')
                objects = live.get('objects', config.get('objects', {})).get('track', [])
                snapshots = live.get('snapshots', config.get('snapshots', {}))
                recording = live.get('record', config.get('record', {}))
                # Frigate writes tracked objects to its event DB only once they can carry media.
                observes_events = (recording.get('enabled') is True
                                   or snapshots.get('enabled') is True and not snapshots.get('required_zones'))
                # detection_fps may legitimately be zero in an empty checkout.
                healthy = (functioning_detector and observes_events and finite(camera_fps) and camera_fps > 0 and finite(process_fps) and process_fps > 0
                    and data.get('detection_enabled') is True and live.get('enabled') is True
                    and live.get('detect', {}).get('enabled') is True and live.get('motion', {}).get('enabled', True) is True
                    and isinstance(objects, list) and 'person' in objects and data.get('connection_quality') != 'unusable')
                if camera in zones:
                    names = zones[camera]
                    if isinstance(names, str):
                        names = [names]
                    healthy = healthy and all(zone_matches(live.get('zones', {}).get(name), polygons.get(camera, {}).get(name))
                                              for name in names)
                tracker.record_health(self.store, camera, updated, healthy)
                width, height = live.get('detect', {}).get('width'), live.get('detect', {}).get('height')
                if healthy and type(width) is int and type(height) is int and width > 0 and height > 0:
                    shapes[camera] = (width, height)
        self.retail_frame_shapes = shapes

    async def poll_camera_signals(self):
        while True:
            if not self.settings.demo:
                try:
                    await self.sync_camera_signals()
                except Exception:
                    now = time.time()
                    self.camera_signal_snapshot = {'checked_at': now, 'updated_at': None, 'cameras': {}, 'reason': 'nvr_unavailable'}
                    self.mark_retail_unavailable(now)
            await asyncio.sleep(self.settings.poll_seconds)

    def camera_signal(self, camera_id, now=None):
        snapshot = self.camera_signal_snapshot
        result = {'signal': 'unknown', 'checked_at': snapshot['checked_at'], 'reason': snapshot['reason'], 'fps': None}
        if self.settings.demo:
            return {**result, 'signal': 'demo', 'reason': 'demo'}
        if snapshot['reason']:
            return result
        now = time.time() if now is None else now
        # Never keep a camera blue after polling stops or Frigate's statistics freeze.
        if any(at is None or not -5 <= now - at <= 30 for at in (snapshot['checked_at'], snapshot['updated_at'])):
            return {**result, 'reason': 'stale'}
        if camera_id not in snapshot['cameras']:
            return {**result, 'signal': 'unconfigured', 'reason': 'not_configured'}
        fps = snapshot['cameras'][camera_id]
        if fps is None:
            return {**result, 'reason': 'invalid_stats'}
        return {**result, 'signal': 'online' if fps > 0 else 'offline', 'reason': None if fps > 0 else 'no_frames', 'fps': fps}

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.to_thread(self.restaurant_feed.close)
        await self.client.aclose()

    async def retail_snapshot_work(self):
        while True:
            if not self.settings.demo:
                await self.sync_retail_snapshots()
            await asyncio.sleep(1)

    async def sync_retail_snapshots(self):
        now = time.time()
        requests = {}
        for tracker in RETAIL_TRACKERS:
            for job in tracker.snapshot_jobs(self.store, now, limit=8):
                key = (job['site_id'], job['camera'], job['source_id'], job['snapshot_at'], job['ended'],
                       tuple(job['box']) if job.get('box') is not None else None)
                requests.setdefault(key, []).append((tracker, job))
        groups = list(requests.values())
        # Fair bounded batches across trackers. Both may be waiting for the same Frigate image.
        for start in range(0, len(groups), 4):
            batch = groups[start:start + 4]
            signatures = await asyncio.gather(*(self.snapshot_signature(group[0][1]) for group in batch))
            finished = time.time()
            for group, descriptor in zip(batch, signatures):
                for tracker, job in group:
                    tracker.snapshot_result(self.store, job, descriptor, finished)
        if groups and self.retail_watermark is not None:
            for tracker in RETAIL_TRACKERS:
                tracker.advance(self.store, self.retail_watermark)

    async def snapshot_signature(self, job):
        source_id = job['source_id']
        if not isinstance(source_id, str) or not SAFE_ID.fullmatch(source_id) or source_id in ('.', '..'):
            return None
        active = job['ended'] is None
        box = job.get('box')
        if active:
            shape = self.retail_frame_shapes.get(job['camera'])
            if not shape or not finite(job.get('snapshot_at')):
                return None
            # Frigate pads live crops to at least 300 px. Locate the actual person inside that crop.
            box = live_crop_box(box, *shape)
        if box is None:
            return None
        suffix = 'snapshot.jpg' if active else 'snapshot-clean.webp'
        params = {'crop': 1, 'bbox': 0, 'timestamp': 0, 'height': 320} if active else None
        expected_type = 'image/jpeg' if active else 'image/webp'
        try:
            async with asyncio.timeout(7):
                async with self.client.stream('GET', self.settings.frigate_url + f'/api/events/{source_id}/{suffix}',
                                              params=params, timeout=5) as response:
                    if response.status_code != 200 or response.headers.get('content-type', '').split(';')[0] != expected_type:
                        return None
                    if active:
                        # A completed legacy JPEG may ignore crop=1. Only the live tracked-object
                        # path in Frigate 0.18.2 responds no-store with the selected frame timestamp.
                        frame = float(response.headers.get('x-frame-time', 'nan'))
                        if response.headers.get('cache-control') != 'no-store' or not math.isfinite(frame) or frame <= 0:
                            return None
                        if abs(frame - job['snapshot_at']) > 0.001 or self.retail_frame_shapes.get(job['camera']) != shape:
                            return None  # A newer image cannot be paired with an older person's box.
                    elif 'no-cache' in response.headers.get('cache-control', ''):
                        return None  # Frigate considers the event active but the saved box is final.
                    if int(response.headers.get('content-length', '0')) > MAX_IMAGE_BYTES:
                        return None
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            return None
                        data.extend(chunk)
                if not data:
                    return None
                return await asyncio.to_thread(clothing_signature, bytes(data), box=box)
        except (httpx.HTTPError, TimeoutError, ValueError, OSError):
            return None

    def ingest(self, events):
        cameras = {c['id']: c for c in self.store.cameras()}
        for event in events:
            camera = cameras.get(event.get('camera'))
            source_id = str(event.get('id', ''))
            if not camera or not SAFE_ID.fullmatch(source_id) or source_id in ('.', '..'):
                continue
            if event.get('false_positive') or event.get('label') != 'person':
                # A late NVR correction must invalidate any previously accepted person evidence.
                prior = self.store.rows("SELECT camera,source_id,started,site_id FROM events WHERE source='frigate' AND source_id=? AND camera=? AND started=? LIMIT 1",
                                        (source_id, camera['id'], event.get('start_time')))
                if prior:
                    end = event.get('end_time')
                    self.invalidate_track(prior[0], end if finite(end) and end >= prior[0]['started'] else time.time())
                continue
            started = float(event['start_time'])
            ended = event.get('end_time')
            data = event.get('data')
            score = data.get('top_score', event.get('top_score')) if isinstance(data, dict) else event.get('top_score')
            entered = event.get('zones', event.get('entered_zones', [])) or []
            previous = self.store.rows("SELECT site_id FROM events WHERE source='frigate' AND source_id=? ORDER BY started LIMIT 1", (source_id,))
            site_id = previous[0]['site_id'] if previous else camera['site_id']
            restricted = {z['name'] for z in camera['zones'] if z['restricted']}
            kind = 'intrusion' if restricted.intersection(entered) and 'intrusion' in modules_for(self.store,site_id) else 'person'
            event_id = self.store.event(source='frigate', source_id=source_id, camera=camera['id'], kind=kind,
                                       score=score, started=started, ended=ended, details={'zones': entered, 'has_clip': bool(event.get('has_clip')), 'has_snapshot': bool(event.get('has_snapshot'))}, site_id=site_id)
            retail.observe(self.store, camera, event, event_id, site_id)
            for tracker in RETAIL_TRACKERS:
                tracker.observe(self.store, camera, event, event_id, site_id)
            if ended is not None:
                self.store.execute("UPDATE events SET ended=? WHERE source='frigate' AND source_id=?", (ended,source_id))
            admission.ingest(self.store, event_id, source_id, camera['id'], entered, started, site_id=site_id)
            # Record once per tracked object, including repeated /events polling and restarts.
            if camera['analyze'] and ended and event.get('has_clip') and modules_for(self.store,site_id) & {'fight','theft','face'}:
                job_id = str(uuid.UUID(hashlib.md5(('frigate:' + source_id).encode()).hexdigest()))
                self.store.enqueue(id=job_id,event_id=event_id,source_id=source_id,camera=camera['id'],site_id=site_id)

    def enqueue_completed(self):
        # Events are the durable overflow buffer: retry after capacity frees, even
        # when the Frigate cursor has advanced beyond the completed object.
        events = self.store.rows('''SELECT e.* FROM events e JOIN cameras c ON c.id=e.camera JOIN sites s ON s.id=e.site_id
            WHERE e.source='frigate' AND e.ended IS NOT NULL AND c.analyze=1
            AND EXISTS(SELECT 1 FROM json_each(s.modules) WHERE value IN ('fight','theft','face'))
            AND json_extract(e.details,'$.has_clip')=1
            AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.source_id=e.source_id)
            ORDER BY e.started LIMIT 100''')
        for event in events:
            job_id = str(uuid.UUID(hashlib.md5(('frigate:' + event['source_id']).encode()).hexdigest()))
            if not self.store.enqueue(id=job_id,event_id=event['id'],source_id=event['source_id'],camera=event['camera'],site_id=event['site_id']):
                break

    async def sync(self):
        # A partial refresh must never release decisions using a previous batch's watermark.
        self.retail_watermark = None
        if self.settings.demo:
            self.health.update(frigate='demo', last_sync=time.time(), error=None)
            self.health['analyzer'] = {'state': 'demo', 'models': []}
            return
        # Ascending time windows with inclusive overlap; never silently skip a full page.
        cursor = self.store.rows("SELECT value FROM meta WHERE key='poll_cursor'")
        after = float(cursor[0]['value']) if cursor else time.time() - 3600
        until = time.time()
        configured = set().union(*(tracker.configured_cameras(self.store) for tracker in RETAIL_TRACKERS))
        async def window(start, end, filters, depth=0, open_ids=None):
            response = await self.client.get(self.settings.frigate_url + '/api/events', params={
                'after': start, 'before': end, 'limit': 250, 'include_thumbnails': 0, **filters})
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                raise ValueError('Invalid Frigate event list')
            if len(events) >= 250:
                if depth >= 48 or end - start < 0.001:
                    raise RuntimeError('Слишком много событий: уменьшите интервал опроса')
                midpoint = (start + end) / 2
                await window(start, midpoint, filters, depth + 1, open_ids)
                await window(midpoint - 0.000001, end, filters, depth + 1, open_ids)
                return
            for event in events:
                if event.get('camera') not in configured or event.get('label') != 'person' or event.get('false_positive'):
                    continue
                source = event.get('id')
                if (not isinstance(source, str) or not SAFE_ID.fullmatch(source) or source in ('.', '..')
                        or not finite(event.get('start_time')) or ('end_time' not in event)
                        or event['end_time'] is not None and (not finite(event['end_time']) or event['end_time'] < event['start_time'])):
                    raise ValueError('Incomplete retail person-track observation')
                if open_ids is not None and event['end_time'] is None:
                    open_ids.add(source)
            self.ingest(events)
        await window(after, until, {})
        open_ids = set()
        replay = bool(configured and (not self.last_retail_replay or until - self.last_retail_replay >= 30))
        cameras = sorted(configured)
        for offset in range(0, len(cameras), 32):
            filters = {'cameras': ','.join(cameras[offset:offset+32]), 'labels': 'person'}
            # Event lists filter by start time. A delayed database insert or an older
            # still-active checkout shopper can otherwise slip past the main cursor.
            await window(0, until, {**filters, 'in_progress': 1}, open_ids=open_ids)
            if replay and after > until - 1260:
                await window(until - 1260, after + 0.000001, {'cameras': filters['cameras']})
        await self.refresh_open_tracks(until, open_ids)
        self.enqueue_completed()
        for tracker in RETAIL_TRACKERS:
            tracker.advance(self.store, until)
        self.store.execute("INSERT INTO meta(key,value) VALUES('poll_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(until - 1),))
        self.retail_watermark = until
        if replay:
            self.last_retail_replay = until
        self.health.update(frigate='online', last_sync=until, error=None)

    async def refresh_open_tracks(self, until, open_ids):
        # Keyset paging covers all tracks, with a bounded number of in-flight requests.
        # Advancing after just the first page could silently miss an active checkout track.
        last_id = ''
        while True:
            tracks = self.store.rows("""SELECT source_id,camera,site_id,MIN(started) AS started FROM events
                WHERE source='frigate' AND ended IS NULL AND source_id>? GROUP BY source_id,camera,site_id
                ORDER BY source_id LIMIT 8""", (last_id,))
            if not tracks:
                return
            last_id = tracks[-1]['source_id']
            tracks = [track for track in tracks if track['source_id'] not in open_ids]
            responses = await asyncio.gather(*[
                self.client.get(self.settings.frigate_url + f"/api/events/{track['source_id']}") for track in tracks
            ], return_exceptions=True)
            for track, response in zip(tracks, responses):
                if isinstance(response, BaseException):
                    raise response
                source_id = track['source_id']
                if response.status_code == 404:
                    self.invalidate_track(track, until)
                    continue
                response.raise_for_status()
                event = response.json()
                if not isinstance(event, dict) or event.get('id') != source_id or event.get('camera') != track['camera']:
                    raise ValueError('Unexpected Frigate track response')
                if event.get('false_positive') or event.get('label') != 'person':
                    self.invalidate_track(track, until)
                else:
                    self.ingest([event])

    def invalidate_track(self, track, until):
        for tracker in RETAIL_TRACKERS:
            tracker.invalidate_track(self.store, track['camera'], track['source_id'], track['started'], until, track['site_id'])
        self.store.execute("""UPDATE events SET ended=? WHERE source='frigate' AND source_id=?
            AND camera=? AND site_id=? AND ended IS NULL""", (until, track['source_id'], track['camera'], track['site_id']))

    async def poll(self):
        while True:
            self.health['poll_heartbeat'] = time.time()
            try:
                await self.sync()
            except Exception as exc:
                self.health.update(frigate='offline', error='Не удалось получить события Frigate')
                log.warning('Frigate sync failed: %s', type(exc).__name__)
            if not self.settings.demo:
                try:
                    response = await self.client.get(self.settings.analyzer_url + '/health', headers={'X-Worker-Key': self.settings.worker_key})
                    response.raise_for_status()
                    self.health['analyzer'] = response.json()
                except Exception:
                    self.health['analyzer'] = {'state': 'unavailable', 'models': []}
                for module, schedule in [('fight', self.schedule_fights), ('theft', self.schedule_theft)]:
                    try:
                        schedule()
                    except Exception as exc:
                        self.health[module + '_monitor']['state'] = 'error'
                        log.warning('%s scheduling failed: %s', module, type(exc).__name__)
            try:
                self.cleanup()
                self.health['maintenance'] = {'state': 'ok', 'error': None}
            except Exception as exc:
                self.health['maintenance'] = {'state': 'error', 'error': 'Очистка хранилища не выполнена'}
                log.warning('Storage cleanup failed: %s', type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    def schedule_fights(self, now=None):
        self.schedule_windows('fight', now)

    def schedule_theft(self, now=None):
        self.schedule_windows('theft', now)

    def schedule_windows(self, module, now):
        """Analyze one scene per camera/window, even if multiple people are present."""
        health = self.health[module + '_monitor']
        retail_only = " AND s.profile='retail'" if module == 'theft' else ''
        cameras = self.store.rows("""SELECT c.id,c.site_id FROM cameras c JOIN sites s ON s.id=c.site_id
            WHERE c.analyze=1 AND EXISTS(SELECT 1 FROM json_each(s.modules) WHERE value=?)""" + retail_only +
            " ORDER BY c.id", (module,))
        health.update(camera_ids=[c['id'] for c in cameras], busy_cameras=[])
        analyzer = self.health['analyzer']
        ready = {m['id'] for m in analyzer.get('models', []) if m.get('state') == 'ready'}
        if self.settings.demo or analyzer.get('state') != 'ready' or not ACTION_MODELS[module] & ready:
            health['state'] = 'model_missing'
            return
        if self.health['frigate'] != 'online':
            health['state'] = 'nvr_offline'
            return
        now = time.time() if now is None else now
        stride = health['stride_seconds']
        latest = int((now - health['recording_delay_seconds']) // stride) * stride
        busy = []
        for camera in cameras:
            key = module + '_cursor:' + camera['id']
            if module == 'theft':
                key += ':' + camera['site_id']
            cursor = self.store.rows('SELECT value FROM meta WHERE key=?', (key,))
            previous = float(cursor[0]['value']) if cursor else latest - stride
            if previous >= latest:
                continue
            pending = self.store.rows("""SELECT count(*) AS n FROM jobs WHERE state IN ('queued','running')
                AND camera=? AND site_id=? AND clip_start IS NOT NULL AND COALESCE(rolling_module,'fight')=?""",
                (camera['id'], camera['site_id'], module))[0]['n']
            if pending >= 2:
                busy.append(camera['id'])
                continue
            skipped = 0
            if latest - previous > 120:
                skipped = int((latest - previous) // stride) - 1
                previous = latest - stride
            end = previous + stride
            start = end - health['window_seconds']
            people = self.store.rows("""SELECT 1 FROM events WHERE source='frigate' AND camera=? AND site_id=?
                AND started < ? AND (ended IS NULL OR ended > ?) LIMIT 1""", (camera['id'], camera['site_id'], end, start))
            if people:
                identity = f'glazza:{module}:{camera["id"]}:{end}'
                if module == 'theft':
                    identity += ':' + camera['site_id']
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
                if not self.store.enqueue(id=job_id, camera=camera['id'], site_id=camera['site_id'], rolling_module=module,
                                          clip_start=start, clip_end=end, now=now):
                    busy.append(camera['id'])
                    continue
            with self.store.connect() as db:
                if skipped:
                    db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                               (now, 'system', module + '.coverage_gap', camera['id'], f'{skipped} windows skipped after backlog/outage'))
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(end)))
            health['skipped_windows'] += skipped
        health.update(state='backlog' if busy else 'monitoring' if cameras else 'no_cameras', busy_cameras=busy)

    def cleanup(self):
        now = time.time()
        self.store.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        # A separate media retention window bounds uploads and downloaded clips.
        for job in self.store.rows("SELECT id,file FROM jobs WHERE updated < ? AND state IN ('succeeded','failed') AND file IS NOT NULL", (now - 86400,)):
            (self.settings.data / 'jobs' / job['file']).unlink(missing_ok=True)
            self.store.execute('UPDATE jobs SET file=NULL WHERE id=?', (job['id'],))
        cutoff = now - self.settings.retention_days * 86400
        self.store.execute('DELETE FROM admission_entries WHERE at < ?', (cutoff,))
        self.store.execute('UPDATE admission_people SET photo=NULL,embedding=NULL,model=NULL WHERE valid_until < ?', (now - 86400,))
        self.store.execute("DELETE FROM jobs WHERE updated < ? AND state IN ('succeeded','failed')", (cutoff,))
        # At most 100 active jobs may pin their capture provenance beyond retention.
        self.store.execute("""DELETE FROM events WHERE started < ? AND ended IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM jobs WHERE event_id=events.id AND state IN ('queued','running'))""", (cutoff,))
        self.store.execute('DELETE FROM audit WHERE at < ?', (cutoff,))
        self.restaurant.cleanup(now=now)
        for tracker in RETAIL_TRACKERS:
            tracker.cleanup(self.store, now)

    async def download_clip(self, job, path):
        temporary = path.with_suffix('.part')
        if job.get('clip_start') is not None:
            route = f"/api/{job['camera']}/start/{job['clip_start']}/end/{job['clip_end']}/clip.mp4"
        else:
            route = f"/api/events/{job['source_id']}/clip.mp4"
        try:
            async with self.client.stream('GET', self.settings.frigate_url + route, timeout=60) as response:
                response.raise_for_status()
                size = 0
                with temporary.open('wb') as handle:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_upload:
                            raise RuntimeError('Видео превышает лимит 200 МБ')
                        handle.write(chunk)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def apply_result(self, job, result):
        if not isinstance(result.get('detections'), list):
            raise ValueError('Invalid analyzer response')
        modules = self.job_modules(job)
        theft_status = result.get('theft', {}).get('status')
        partial_theft = theft_status in ('insufficient_samples', 'incomplete_coverage') and 'theft' not in result.get('checked', [])
        if modules == {'theft'} and 'theft' not in result.get('checked', []) and not partial_theft:
            raise ValueError('Theft was not evaluated')
        site_id = job.get('site_id') or camera_site(self.store, job['camera'])
        rolling = job.get('clip_start') is not None
        origin = self.store.rows('SELECT started,ended FROM events WHERE id=?', (job.get('event_id'),))
        if job.get('event_id') and not origin:
            raise ValueError('Capture provenance is unavailable')
        if rolling:
            base, duration = job['clip_start'], job['clip_end'] - job['clip_start']
        elif origin:
            base = origin[0]['started']
            duration = (origin[0]['ended'] or base) - base
        else:
            # An upload has no trustworthy recording time and never participates in camera deduplication.
            base, duration = job['created'], 0
        media_duration = result.get('duration_seconds', duration)
        if not isinstance(media_duration, (int, float)) or not math.isfinite(media_duration) or media_duration < 0:
            raise ValueError('Invalid video duration')
        if modules == {'theft'} and (not media_duration or not result.get('duration_seconds')):
            raise ValueError('Missing theft analysis duration')

        if rolling and modules == {'theft'} and abs(duration - media_duration) > 1:
            # An incomplete Frigate export cannot anchor a model timestamp to this camera window.
            result['checked'] = [kind for kind in result.get('checked', []) if kind != 'theft']
            result.setdefault('theft', {})['status'] = 'incomplete_recording'
            self.fail_job(job, 'Длительность фрагмента Frigate отличается от запрошенного интервала.', result)
            return

        detections = []
        strongest_fight = None
        for detection in result['detections']:
            kind = detection.get('kind')
            if kind not in modules or kind not in ('fight', 'theft'):
                continue
            if 'checked' in result and kind not in result['checked']:
                if kind != 'theft' or theft_status != 'incomplete_coverage':
                    raise ValueError('Detection from an unchecked module')
                # A valid local interval can still warrant review when other footage is missing.
            score = detection['score']
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('Invalid detection score')
            if kind == 'fight':
                if strongest_fight is None or score > strongest_fight['score']:
                    strongest_fight = detection
            else:
                # A clip may contain distinct, non-overlapping retail episodes.
                start, end = detection.get('start_seconds'), detection.get('end_seconds')
                if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                            for v in (start, end)) or not 0 <= start < end <= media_duration + 0.001):
                    raise ValueError('Invalid theft interval')
                detections.append(detection)
        detections.sort(key=lambda item: item['start_seconds'])
        if strongest_fight:
            detections.append(strongest_fight)

        # Check overlaps and commit cards and job result in one write transaction.
        # Competing workers and interrupted retries cannot create partial or duplicate reviews.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not saved:
                raise ValueError('Missing analysis job')
            if saved['state'] in ('succeeded', 'failed'):
                return
            for index, detection in enumerate(detections):
                kind = detection['kind']
                offset = detection.get('start_seconds', 0)
                started = base + offset
                ended = base + detection.get('end_seconds', max(duration, offset))
                source_id = job['id'] + ':' + kind
                if kind == 'theft' and index:
                    source_id += ':' + str(index)
                if (kind == 'theft' and (rolling or origin)) or (kind == 'fight' and rolling):
                    duplicate = self.overlapping_signal(db, job['camera'], site_id, kind, started, ended)
                    if duplicate:
                        if kind == 'theft':
                            # Extend only the deduplication range. The original evidence clip,
                            # timestamps and any existing operator decision remain intact.
                            db.execute("""UPDATE events SET details=json_set(details,'$.seen_since',?,'$.seen_until',?)
                                WHERE id=?""", (min(duplicate['first'], started), max(duplicate['last'], ended), duplicate['id']))
                        result.setdefault('deduplicated', []).append({'kind': kind, 'event_id': duplicate['id'],
                                                                      'started': started, 'ended': ended})
                        continue
                self.store.event(source='analysis', source_id=source_id, camera=job['camera'], kind=kind,
                    score=detection['score'], started=started, ended=ended, site_id=site_id, connection=db,
                    details={'job_id': job['id'], 'origin_event': job.get('event_id'), 'model': detection.get('model', result.get('model')),
                        'label': detection.get('label'), 'start_seconds': detection.get('start_seconds'),
                        'end_seconds': detection.get('end_seconds'), 'rolling': rolling, 'review_required': True})
            if partial_theft and modules == {'theft'}:
                self.fail_job(job, 'Недостаточно видеокадров для полной проверки возможной кражи.', result, db)
            else:
                db.execute("UPDATE jobs SET state='succeeded',result=?,error=NULL,updated=? WHERE id=?",
                           (json.dumps(result, ensure_ascii=False, allow_nan=False), time.time(), job['id']))

    def fail_job(self, job, message, result=None, connection=None):
        with (contextlib.nullcontext(connection) if connection is not None else self.store.connect()) as db:
            changed = db.execute("""UPDATE jobs SET state='failed',result=?,error=?,updated=?
                WHERE id=? AND state NOT IN ('failed','succeeded')""",
                (json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None,
                 message, time.time(), job['id'])).rowcount
            if changed and job.get('rolling_module') == 'theft':
                db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                    (time.time(), 'system', 'theft.coverage_gap', job['camera'], f'{job["clip_start"]}–{job["clip_end"]}: {message}'))
        if changed and job.get('rolling_module') == 'theft':
            self.health['theft_monitor']['skipped_windows'] += 1

    @staticmethod
    def overlapping_signal(db, camera, site_id, kind, started, ended):
        # Legacy clip-wide theft alarms had no localized evidence and must not absorb new signals.
        if kind == 'theft':
            scope = """AND json_extract(details,'$.start_seconds') IS NOT NULL
                AND (json_extract(details,'$.rolling')=1 OR json_extract(details,'$.origin_event') IS NOT NULL)"""
        else:
            scope = "AND json_extract(details,'$.rolling')=1"
        return db.execute("""SELECT id,COALESCE(json_extract(details,'$.seen_since'),started) AS first,
                COALESCE(json_extract(details,'$.seen_until'),ended) AS last FROM events
            WHERE source='analysis' AND camera=? AND site_id=? AND kind=? AND first < ? AND last > ? """ + scope +
            " ORDER BY started,id LIMIT 1", (camera, site_id, kind, ended, started)).fetchone()

    def job_modules(self, job):
        requested = modules_for(self.store,job.get('site_id') or camera_site(self.store,job['camera'])) & {'fight','theft','face'}
        # Rolling jobs created before the module column existed were always fight-only.
        return requested & {job.get('rolling_module') or 'fight'} if job.get('clip_start') is not None else requested

    def models_ready(self, job):
        analyzer = self.health['analyzer']
        ready = {m['id'] for m in analyzer.get('models', []) if m.get('state') == 'ready'}
        requested = self.job_modules(job)
        # Existing partial-analysis mode remains explicit in analyzer warnings.
        # An unrelated ready model must never consume a single-module job.
        return analyzer.get('state') == 'ready' and any(ready & ACTION_MODELS[m] for m in requested)

    async def work(self):
        while True:
            try:
                rows = self.store.rows("SELECT * FROM jobs WHERE state='queued' AND updated < ? ORDER BY (clip_start IS NOT NULL) DESC, created LIMIT 100", (time.time() - 2,))
                for row in rows:
                    if not self.job_modules(row):
                        self.store.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=? AND state='queued'",
                                           ('Модули анализа задания отключены. Включите модуль перед повтором.',time.time(),row['id']))
                rows = [row for row in rows if self.job_modules(row)]
                job = next((row for row in rows if self.models_ready(row)), None)
                if job:
                    if self.store.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated=? WHERE id=? AND state='queued'", (time.time(), job['id'])):
                        await self.run_job(job)
            except Exception as exc:
                log.warning('Queue iteration failed: %s', type(exc).__name__)
            await asyncio.sleep(1)

    async def run_job(self, job):
        path = self.settings.data / 'jobs' / (job.get('file') or (job['id'] + '.mp4'))
        try:
            if not path.exists():
                if not job['source_id'] and job.get('clip_start') is None:
                    raise RuntimeError('Видео больше не доступно')
                await self.download_clip(job, path)
            self.store.execute('UPDATE jobs SET file=? WHERE id=?', (path.name, job['id']))
            modules = self.job_modules(job)
            mode = 'fight' if job.get('clip_start') is not None and modules == {'fight'} else 'all'
            response = await self.client.post(self.settings.analyzer_url + '/analyze',
                json={'job_id': job['id'], 'mode': mode, 'modules': sorted(modules)},
                headers={'X-Worker-Key': self.settings.worker_key}, timeout=600)
            response.raise_for_status()
            self.apply_result(job, response.json())
        except Exception as exc:
            if isinstance(exc, httpx.TransportError) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429,503)):
                self.store.execute("UPDATE jobs SET state='queued',attempts=MAX(0,attempts-1),error=?,updated=? WHERE id=?",
                                   ('Сервис временно недоступен или занят. Ожидаем повтор.',time.time()+30,job['id']))
                return
            attempts = job['attempts'] + 1
            message = 'Анализ недоступен или файл не поддерживается. Проверьте модели и видео.'
            if attempts >= 3:
                self.fail_job(job, message)
            else:
                self.store.execute("UPDATE jobs SET state='queued',error=?,updated=? WHERE id=?",
                                   (message, time.time() + 30, job['id']))
            log.warning('Analysis %s failed: %s', job['id'], type(exc).__name__)


def seed_demo(store):
    if store.rows("SELECT key FROM meta WHERE key='demo_seeded'"):
        return
    for id_, name in [('entrance', 'Главный вход'), ('hall', 'Коридор · 1 этаж'), ('warehouse', 'Лаборатория')]:
        store.execute('INSERT OR IGNORE INTO cameras(id,name,stream,zones) VALUES(?,?,?,?)', (id_, name, '', '[]'))
    for i, (camera, kind, score, ago) in enumerate([('warehouse', 'intrusion', 0.96, 240), ('hall', 'theft', 0.81, 1200), ('entrance', 'person', 0.94, 2400), ('hall', 'fight', 0.87, 5600)]):
        store.event(source='demo', source_id=str(i), camera=camera, kind=kind, score=score, started=time.time() - ago, ended=time.time() - ago + 15, details={'demonstration': True})
    store.execute("INSERT INTO meta(key,value) VALUES('demo_seeded','1')")
