"""Durable ingestion and analysis queue. No simulated detections in live mode."""
import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid

import httpx
from . import admission
from .platform import camera_enabled, modules_for, camera_site

log = logging.getLogger('glazza.engine')
SAFE_ID = re.compile(r'^[a-zA-Z0-9_.-]{1,120}$')


class Engine:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        admission.initialize(store)
        self.health = {'frigate': 'starting', 'last_sync': None, 'error': None, 'analyzer': {'state': 'unavailable', 'models': []}}
        self.health['fight_monitor'] = {'state': 'waiting', 'busy_cameras': [], 'skipped_windows': 0}
        # Service-to-service traffic must not leave the Docker network via ambient proxies.
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self.tasks = []
        self.health['maintenance'] = {'state': 'waiting', 'error': None}
        self.health['background'] = {}
        self.camera_signal_snapshot = {'checked_at': None, 'updated_at': None, 'cameras': {}, 'reason': 'waiting'}

    async def start(self):
        self.store.execute("UPDATE jobs SET state='queued', updated=? WHERE state='running'", (time.time(),))
        self.tasks = [asyncio.create_task(self.supervise(name, worker)) for name, worker in
                      [('poll',self.poll),('analysis',self.work),('cameras',self.poll_camera_signals),('admission',self.admission_work)]]

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
        valid_number = lambda n: isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
        cameras = {}
        for identifier, data in payload['cameras'].items():
            fps = data.get('camera_fps') if isinstance(data, dict) else None
            cameras[identifier] = fps if valid_number(fps) and fps >= 0 else None
        # Publish only measured FPS and timestamps; raw NVR statistics can contain secrets.
        self.camera_signal_snapshot = {'checked_at': time.time(), 'updated_at': updated if valid_number(updated) else None,
                                       'cameras': cameras, 'reason': None}

    async def poll_camera_signals(self):
        while True:
            if not self.settings.demo:
                try:
                    await self.sync_camera_signals()
                except Exception:
                    self.camera_signal_snapshot = {'checked_at': time.time(), 'updated_at': None, 'cameras': {}, 'reason': 'nvr_unavailable'}
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
        await self.client.aclose()

    def ingest(self, events):
        cameras = {c['id']: c for c in self.store.cameras()}
        for event in events:
            camera = cameras.get(event.get('camera'))
            source_id = str(event.get('id', ''))
            if not camera or event.get('label') != 'person' or not SAFE_ID.fullmatch(source_id) or event.get('false_positive'):
                continue
            started = float(event['start_time'])
            ended = event.get('end_time')
            score = event.get('data', {}).get('top_score', event.get('top_score'))
            entered = event.get('zones', event.get('entered_zones', [])) or []
            previous = self.store.rows("SELECT site_id FROM events WHERE source='frigate' AND source_id=? ORDER BY started LIMIT 1", (source_id,))
            site_id = previous[0]['site_id'] if previous else camera['site_id']
            restricted = {z['name'] for z in camera['zones'] if z['restricted']}
            kind = 'intrusion' if restricted.intersection(entered) and 'intrusion' in modules_for(self.store,site_id) else 'person'
            event_id = self.store.event(source='frigate', source_id=source_id, camera=camera['id'], kind=kind,
                                       score=score, started=started, ended=ended, details={'zones': entered, 'has_clip': bool(event.get('has_clip')), 'has_snapshot': bool(event.get('has_snapshot'))}, site_id=site_id)
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
        if self.settings.demo:
            self.health.update(frigate='demo', last_sync=time.time(), error=None)
            self.health['analyzer'] = {'state': 'demo', 'models': []}
            return
        # Ascending time windows with inclusive overlap; never silently skip a full page.
        cursor = self.store.rows("SELECT value FROM meta WHERE key='poll_cursor'")
        after = float(cursor[0]['value']) if cursor else time.time() - 3600
        until = time.time()
        async def window(start, end, depth=0):
            response = await self.client.get(self.settings.frigate_url + '/api/events', params={'after': start, 'before': end, 'limit': 250})
            response.raise_for_status()
            events = response.json()
            if len(events) >= 250:
                if depth >= 18 or end - start < 0.001:
                    raise RuntimeError('Слишком много событий: уменьшите интервал опроса')
                midpoint = (start + end) / 2
                await window(start, midpoint, depth + 1)
                await window(midpoint - 0.000001, end, depth + 1)
            else:
                self.ingest(events)
        await window(after, until)
        # Revisit tracked objects still running, independently from the main cursor.
        active = self.store.rows("SELECT DISTINCT source_id FROM events WHERE source='frigate' AND ended IS NULL LIMIT 500")
        for item in active:
            response = await self.client.get(self.settings.frigate_url + f"/api/events/{item['source_id']}")
            if response.status_code == 200:
                self.ingest([response.json()])
            elif response.status_code == 404:
                self.store.execute('UPDATE events SET ended=? WHERE source_id=? AND ended IS NULL', (until, item['source_id']))
        self.store.execute("INSERT INTO meta(key,value) VALUES('poll_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(until - 1),))
        self.enqueue_completed()
        self.health.update(frigate='online', last_sync=until, error=None)

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
                try:
                    self.schedule_fights()
                except Exception as exc:
                    self.health['fight_monitor']['state'] = 'error'
                    log.warning('Fight scheduling failed: %s', type(exc).__name__)
            try:
                self.cleanup()
                self.health['maintenance'] = {'state': 'ok', 'error': None}
            except Exception as exc:
                self.health['maintenance'] = {'state': 'error', 'error': 'Очистка хранилища не выполнена'}
                log.warning('Storage cleanup failed: %s', type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    def schedule_fights(self, now=None):
        """Overlapping 12s recordings, every 6s, with 20s allowance for NVR flush.

        Bounded backlog, durable per-camera cursor, independent of tracked-object end.
        Only requested cameras and time spans containing a detected person are eligible.
        """
        health = self.health['fight_monitor']
        ready = {m['id'] for m in self.health['analyzer'].get('models', []) if m.get('state') == 'ready'}
        if self.settings.demo or not ({'fight', 'vadclip'} & ready):
            health.update(state='model_missing', busy_cameras=[])
            return
        if self.health['frigate'] != 'online':
            health.update(state='nvr_offline', busy_cameras=[])
            return
        now = time.time() if now is None else now
        latest = int((now - 20) // 6) * 6
        busy = []
        cameras = [c for c in self.store.cameras() if c['analyze'] and camera_enabled(self.store,c['id'],'fight')]
        for camera in cameras:
            key = 'fight_cursor:' + camera['id']
            cursor = self.store.rows('SELECT value FROM meta WHERE key=?', (key,))
            previous = float(cursor[0]['value']) if cursor else latest - 6
            if previous >= latest:
                continue
            pending = self.store.rows("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running') AND camera=? AND clip_start IS NOT NULL", (camera['id'],))[0]['n']
            total = self.store.rows("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running')")[0]['n']
            if pending >= 2 or total >= 100:
                busy.append(camera['id'])
                continue
            if latest - previous > 120:
                skipped = int((latest - previous) // 6) - 1
                health['skipped_windows'] += skipped
                self.store.audit('system', 'fight.coverage_gap', camera['id'], f'{skipped} windows skipped after backlog/outage')
                previous = latest - 6
            end, start = previous + 6, previous - 6
            people = self.store.rows("SELECT id FROM events WHERE source='frigate' AND camera=? AND started < ? AND (ended IS NULL OR ended > ?) LIMIT 1", (camera['id'], end, start))
            if people:
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'glazza:fight:{camera["id"]}:{end}'))
                if not self.store.enqueue(id=job_id,camera=camera['id'],site_id=camera['site_id'],clip_start=start,clip_end=end,now=now):
                    busy.append(camera['id'])
                    continue
            with self.store.connect() as db:
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(end)))
        health.update(state='backlog' if busy else 'monitoring' if cameras else 'no_cameras', busy_cameras=busy,
                      window_seconds=12, stride_seconds=6, recording_delay_seconds=20)

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
        # One card per kind/clip. Keep the strongest interval and expose its timestamps.
        by_kind = {}
        for detection in result['detections']:
            if detection.get('kind') not in by_kind or detection['score'] > by_kind[detection['kind']]['score']:
                by_kind[detection['kind']] = detection
        for detection in by_kind.values():
            if detection.get('kind') not in ('fight', 'theft') or detection['kind'] not in modules_for(self.store,job.get('site_id') or camera_site(self.store,job['camera'])):
                continue
            start = job.get('clip_start')
            origin = self.store.rows('SELECT started,ended FROM events WHERE id=?', (job.get('event_id'),))
            if start is None and job.get('event_id') and not origin:
                raise ValueError('Capture provenance is unavailable')
            if start is not None:
                base, duration = start, job['clip_end'] - start
            elif origin:
                base = origin[0]['started']
                duration = (origin[0]['ended'] or base) - base
            else:
                # Uploaded recordings have no trustworthy capture timestamp.
                base, duration = job['created'], 0
            started = base + detection.get('start_seconds', 0)
            ended = base + detection.get('end_seconds', max(duration, detection.get('start_seconds', 0)))
            # Overlapping recording windows should not flood the operator with the same signal.
            if start is not None:
                duplicate = self.store.rows("""SELECT id FROM events WHERE source='analysis' AND camera=? AND site_id=? AND kind=?
                    AND json_extract(details,'$.rolling')=1 AND started < ? AND ended > ? LIMIT 1""",
                    (job['camera'],job.get('site_id') or camera_site(self.store,job['camera']),detection['kind'],ended,started))
                if duplicate:
                    result.setdefault('deduplicated', []).append({'kind':detection['kind'],'event_id':duplicate[0]['id'],
                                                                 'started':started,'ended':ended})
                    continue
            self.store.event(source='analysis', source_id=job['id'] + ':' + detection['kind'], camera=job['camera'],
                             kind=detection['kind'], score=detection['score'], started=started, ended=ended,
                             details={'job_id': job['id'], 'origin_event': job.get('event_id'), 'model': detection.get('model', result.get('model')), 'label': detection.get('label'),
                                      'start_seconds': detection.get('start_seconds'), 'end_seconds': detection.get('end_seconds'),
                                      'rolling': start is not None, 'review_required': True}, site_id=job.get('site_id'))
        self.store.execute("UPDATE jobs SET state='succeeded',result=?,error=NULL,updated=? WHERE id=?", (json.dumps(result, ensure_ascii=False), time.time(), job['id']))

    def job_modules(self, job):
        requested = modules_for(self.store,job.get('site_id') or camera_site(self.store,job['camera'])) & {'fight','theft','face'}
        return requested & {'fight'} if job.get('clip_start') is not None else requested

    def models_ready(self, job):
        analyzer = self.health['analyzer']
        ready = {m['id'] for m in analyzer.get('models', []) if m.get('state') == 'ready'}
        requested = self.job_modules(job)
        required = {'theft':{'vadclip'}, 'fight':{'fight','vadclip'}, 'face':{'face'}}
        # Existing partial-analysis mode remains explicit in analyzer warnings.
        # An unrelated ready model must never consume a single-module job.
        return analyzer.get('state') == 'ready' and any(ready & required[m] for m in requested)

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
            response = await self.client.post(self.settings.analyzer_url + '/analyze', json={'job_id': job['id'], 'mode': 'fight' if job.get('clip_start') is not None else 'all', 'modules': sorted(modules_for(self.store,job.get('site_id') or camera_site(self.store,job['camera'])) & {'fight','theft','face'})},
                                              headers={'X-Worker-Key': self.settings.worker_key}, timeout=600)
            response.raise_for_status()
            self.apply_result(job, response.json())
        except Exception as exc:
            if isinstance(exc, httpx.TransportError) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429,503)):
                self.store.execute("UPDATE jobs SET state='queued',attempts=MAX(0,attempts-1),error=?,updated=? WHERE id=?",
                                   ('Сервис временно недоступен или занят. Ожидаем повтор.',time.time()+30,job['id']))
                return
            attempts = job['attempts'] + 1
            state = 'failed' if attempts >= 3 else 'queued'
            self.store.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=?',
                               (state, 'Анализ недоступен или файл не поддерживается. Проверьте модели и видео.', time.time() + (30 if state == 'queued' else 0), job['id']))
            log.warning('Analysis %s failed: %s', job['id'], type(exc).__name__)


def seed_demo(store):
    if store.rows("SELECT key FROM meta WHERE key='demo_seeded'"):
        return
    for id_, name in [('entrance', 'Главный вход'), ('hall', 'Коридор · 1 этаж'), ('warehouse', 'Лаборатория')]:
        store.execute('INSERT OR IGNORE INTO cameras(id,name,stream,zones) VALUES(?,?,?,?)', (id_, name, '', '[]'))
    for i, (camera, kind, score, ago) in enumerate([('warehouse', 'intrusion', 0.96, 240), ('hall', 'theft', 0.81, 1200), ('entrance', 'person', 0.94, 2400), ('hall', 'fight', 0.87, 5600)]):
        store.event(source='demo', source_id=str(i), camera=camera, kind=kind, score=score, started=time.time() - ago, ended=time.time() - ago + 15, details={'demonstration': True})
    store.execute("INSERT INTO meta(key,value) VALUES('demo_seeded','1')")
