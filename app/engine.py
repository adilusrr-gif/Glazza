"""Durable ingestion and analysis queue. No simulated detections in live mode."""
import asyncio
import hashlib
import json
import logging
import re
import time
import uuid

import httpx

log = logging.getLogger('glazza.engine')
SAFE_ID = re.compile(r'^[a-zA-Z0-9_.-]{1,120}$')


class Engine:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.health = {'frigate': 'starting', 'last_sync': None, 'error': None, 'analyzer': {'state': 'unavailable', 'models': []}}
        self.health['fight_monitor'] = {'state': 'waiting', 'busy_cameras': [], 'skipped_windows': 0}
        # Service-to-service traffic must not leave the Docker network via ambient proxies.
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self.tasks = []

    async def start(self):
        self.store.execute("UPDATE jobs SET state='queued', updated=? WHERE state='running'", (time.time(),))
        self.tasks = [asyncio.create_task(self.poll()), asyncio.create_task(self.work())]

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
            restricted = {z['name'] for z in camera['zones'] if z['restricted']}
            kind = 'intrusion' if restricted.intersection(entered) else 'person'
            event_id = self.store.event(source='frigate', source_id=source_id, camera=camera['id'], kind=kind,
                                       score=score, started=started, ended=ended, details={'zones': entered, 'has_clip': bool(event.get('has_clip')), 'has_snapshot': bool(event.get('has_snapshot'))})
            # Record once per tracked object, including repeated /events polling and restarts.
            if camera['analyze'] and ended and event.get('has_clip'):
                job_id = str(uuid.UUID(hashlib.md5(('frigate:' + source_id).encode()).hexdigest()))
                self.store.execute('INSERT OR IGNORE INTO jobs(id,event_id,source_id,camera,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                                   (job_id, event_id, source_id, camera['id'], 'queued', time.time(), time.time()))

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
        self.health.update(frigate='online', last_sync=until, error=None)

    async def poll(self):
        while True:
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
                self.schedule_fights()
            self.cleanup()
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
        cameras = [c for c in self.store.cameras() if c['analyze']]
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
            with self.store.connect() as db:
                if people:
                    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'glazza:fight:{camera["id"]}:{end}'))
                    db.execute('INSERT OR IGNORE INTO jobs(id,camera,state,created,updated,clip_start,clip_end) VALUES(?,?,?,?,?,?,?)',
                               (job_id, camera['id'], 'queued', now, now, start, end))
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(end)))
        health.update(state='backlog' if busy else 'monitoring' if cameras else 'no_cameras', busy_cameras=busy,
                      window_seconds=12, stride_seconds=6, recording_delay_seconds=20)

    def cleanup(self):
        now = time.time()
        self.store.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        # A separate media retention window bounds uploads and downloaded clips.
        for job in self.store.rows("SELECT id,file FROM jobs WHERE updated < ? AND state IN ('succeeded','failed') AND file IS NOT NULL", (now - 86400,)):
            (self.settings.data / 'jobs' / f"{job['id']}.mp4").unlink(missing_ok=True)
            self.store.execute('UPDATE jobs SET file=NULL WHERE id=?', (job['id'],))
        cutoff = now - self.settings.retention_days * 86400
        self.store.execute("DELETE FROM jobs WHERE updated < ? AND state IN ('succeeded','failed')", (cutoff,))
        self.store.execute('DELETE FROM events WHERE started < ? AND ended IS NOT NULL', (cutoff,))
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
            if detection.get('kind') not in ('fight', 'theft'):
                continue
            start = job.get('clip_start')
            started = (start + detection.get('start_seconds', 0)) if start is not None else job['created']
            ended = (start + detection.get('end_seconds', 12)) if start is not None else job['created']
            # Overlapping recording windows should not flood the operator with the same signal.
            if start is not None and self.store.rows("SELECT id FROM events WHERE source='analysis' AND camera=? AND kind=? AND started BETWEEN ? AND ? LIMIT 1",
                                                    (job['camera'], detection['kind'], started - 30, started + 30)):
                continue
            self.store.event(source='analysis', source_id=job['id'] + ':' + detection['kind'], camera=job['camera'],
                             kind=detection['kind'], score=detection['score'], started=started, ended=ended,
                             details={'job_id': job['id'], 'origin_event': job.get('event_id'), 'model': detection.get('model', result.get('model')), 'label': detection.get('label'),
                                      'start_seconds': detection.get('start_seconds'), 'end_seconds': detection.get('end_seconds'),
                                      'rolling': start is not None, 'review_required': True})
        self.store.execute("UPDATE jobs SET state='succeeded',result=?,error=NULL,updated=? WHERE id=?", (json.dumps(result, ensure_ascii=False), time.time(), job['id']))

    async def work(self):
        while True:
            try:
                available = self.health['analyzer'].get('state') == 'ready'
                rows = self.store.rows("SELECT * FROM jobs WHERE state='queued' AND updated < ? ORDER BY (clip_start IS NOT NULL) DESC, created LIMIT 1", (time.time() - 2,)) if available else []
                if rows:
                    job = rows[0]
                    if self.store.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated=? WHERE id=? AND state='queued'", (time.time(), job['id'])):
                        await self.run_job(job)
            except Exception as exc:
                log.warning('Queue iteration failed: %s', type(exc).__name__)
            await asyncio.sleep(1)

    async def run_job(self, job):
        path = self.settings.data / 'jobs' / (job['id'] + '.mp4')
        try:
            if not path.exists():
                if not job['source_id'] and job.get('clip_start') is None:
                    raise RuntimeError('Видео больше не доступно')
                await self.download_clip(job, path)
            self.store.execute('UPDATE jobs SET file=? WHERE id=?', (path.name, job['id']))
            response = await self.client.post(self.settings.analyzer_url + '/analyze', json={'job_id': job['id'], 'mode': 'fight' if job.get('clip_start') is not None else 'all'},
                                              headers={'X-Worker-Key': self.settings.worker_key}, timeout=600)
            response.raise_for_status()
            self.apply_result(job, response.json())
        except Exception as exc:
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
