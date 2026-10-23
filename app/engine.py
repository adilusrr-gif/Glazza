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
            self.cleanup()
            await asyncio.sleep(self.settings.poll_seconds)

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
        try:
            async with self.client.stream('GET', self.settings.frigate_url + f"/api/events/{job['source_id']}/clip.mp4", timeout=60) as response:
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
        for detection in result['detections']:
            if detection.get('kind') not in ('fight', 'theft'):
                continue
            self.store.event(source='analysis', source_id=job['id'] + ':' + detection['kind'], camera=job['camera'],
                             kind=detection['kind'], score=detection['score'], started=job['created'], ended=job['created'],
                             details={'job_id': job['id'], 'origin_event': job.get('event_id'), 'model': result.get('model'), 'label': detection.get('label'), 'review_required': True})
        self.store.execute("UPDATE jobs SET state='succeeded',result=?,error=NULL,updated=? WHERE id=?", (json.dumps(result, ensure_ascii=False), time.time(), job['id']))

    async def work(self):
        while True:
            try:
                available = self.health['analyzer'].get('state') == 'ready'
                rows = self.store.rows("SELECT * FROM jobs WHERE state='queued' AND updated < ? ORDER BY created LIMIT 1", (time.time() - 2,)) if available else []
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
                if not job['source_id']:
                    raise RuntimeError('Видео больше не доступно')
                await self.download_clip(job, path)
            self.store.execute('UPDATE jobs SET file=? WHERE id=?', (path.name, job['id']))
            response = await self.client.post(self.settings.analyzer_url + '/analyze', json={'job_id': job['id']},
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
    for id_, name in [('entrance', 'Главный вход'), ('hall', 'Торговый зал'), ('warehouse', 'Склад')]:
        store.execute('INSERT OR IGNORE INTO cameras(id,name,stream,zones) VALUES(?,?,?,?)', (id_, name, '', '[]'))
    for i, (camera, kind, score, ago) in enumerate([('warehouse', 'intrusion', 0.96, 240), ('hall', 'theft', 0.81, 1200), ('entrance', 'person', 0.94, 2400), ('hall', 'fight', 0.87, 5600)]):
        store.event(source='demo', source_id=str(i), camera=camera, kind=kind, score=score, started=time.time() - ago, ended=time.time() - ago + 15, details={'demonstration': True})
    store.execute("INSERT INTO meta(key,value) VALUES('demo_seeded','1')")
