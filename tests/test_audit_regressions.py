"""Regressions for the independently reproduced November audit findings."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import time

import httpx
from PIL import Image, GifImagePlugin, XbmImagePlugin
import pytest

from analyzer.identity import IdentityModel
from app.store import Store


def fill_queue(store, count=100):
    for i in range(count):
        assert store.enqueue(id=f'queued-{i}', camera='upload', site_id='default')


def test_concurrent_producers_share_atomic_queue_limit(app):
    store = app.state.store
    fill_queue(store, 99)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: store.enqueue(id=f'race-{i}', camera='upload', site_id='default'), range(8)))
    assert sum(results) == 1
    assert store.rows("SELECT count(*) AS n FROM jobs WHERE state='queued'")[0]['n'] == 100
    assert store.enqueue(id='queued-0', camera='upload', site_id='default')  # Existing id is harmless.


def test_completed_events_wait_for_capacity_and_resume_after_reopen(logged, app, camera, settings):
    assert logged.post('/api/cameras', json=camera).status_code == 201
    store, engine = app.state.store, app.state.engine
    fill_queue(store)
    engine.ingest([{'id':'deferred', 'camera':camera['id'], 'label':'person',
                    'start_time':1000, 'end_time':1030, 'has_clip':True}])
    assert len(store.rows('SELECT * FROM jobs')) == 100
    store.execute("UPDATE jobs SET state='succeeded' WHERE id='queued-0'")
    engine.store = Store(settings.data)
    engine.enqueue_completed()
    engine.enqueue_completed()
    jobs = store.rows("SELECT * FROM jobs WHERE source_id='deferred'")
    assert len(jobs) == 1 and jobs[0]['site_id'] == 'default'
    assert store.rows("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','running')")[0]['n'] == 100


def test_retry_respects_global_limit(logged, app):
    store = app.state.store
    store.enqueue(id='failed', camera='upload', site_id='default', file='failed.mp4')
    store.execute("UPDATE jobs SET state='failed' WHERE id='failed'")
    fill_queue(store)
    assert logged.post('/api/jobs/failed/retry').status_code == 429
    assert store.rows("SELECT state FROM jobs WHERE id='failed'")[0]['state'] == 'failed'
    store.execute("UPDATE jobs SET state='succeeded' WHERE id='queued-0'")
    assert logged.post('/api/jobs/failed/retry').status_code == 200


def test_upload_rechecks_capacity_after_stream_and_removes_file(logged, app, monkeypatch, settings):
    monkeypatch.setattr(app.state.store, 'enqueue', lambda **kwargs: False)
    response = logged.post('/api/jobs/upload', content=b'0000ftyp'+b'0'*24, headers={'Content-Type':'video/mp4'})
    assert response.status_code == 429
    assert not list((settings.data/'jobs').iterdir())


@pytest.mark.parametrize('failure', ['cleanup', 'schedule_fights'])
def test_poll_survives_maintenance_and_scheduler_failures(app, monkeypatch, failure):
    engine = app.state.engine
    calls = []
    async def sync():
        calls.append('sync')
        engine.health.update(frigate='online', error=None)
        if len(calls) == 2:
            raise asyncio.CancelledError()
    def broken():
        raise PermissionError('fixture')
    original_sleep = asyncio.sleep
    async def fast_sleep(_):
        await original_sleep(0)
    monkeypatch.setattr(engine, 'sync', sync)
    monkeypatch.setattr(engine, failure, broken)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'state':'ready','models':[]})))
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await engine.poll()
        await engine.client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    if failure == 'cleanup':
        assert engine.health['maintenance']['state'] == 'error'
    else:
        assert engine.health['fight_monitor']['state'] == 'error'


def test_supervisor_restarts_failed_worker_and_propagates_cancellation(app, monkeypatch):
    calls = []
    async def worker():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('fixture')
        raise asyncio.CancelledError()
    original_sleep = asyncio.sleep
    async def fast_sleep(_):
        await original_sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await app.state.engine.supervise('poll', worker)
    asyncio.run(run())
    assert len(calls) == 2
    assert app.state.engine.health['frigate'] == 'offline'


def test_dead_or_stale_poll_is_not_reported_online(logged, app):
    engine = app.state.engine
    engine.health.update(frigate='online', poll_heartbeat=time.time()-1000)
    class Task:
        def done(self): return False
    engine.tasks = [Task()]
    assert logged.get('/api/overview').json()['health']['frigate'] == 'offline'
    engine.tasks = []


def test_legacy_trigger_removed_and_upload_site_preserved(logged, app, settings):
    store = app.state.store
    store.execute("INSERT INTO cameras(id,name,stream,site_id) VALUES('upload','Legacy camera','rtsp://localhost/live','default')")
    store.execute('''CREATE TRIGGER jobs_assign_site AFTER INSERT ON jobs BEGIN
        UPDATE jobs SET site_id='default' WHERE id=NEW.id; END''')
    organization = logged.post('/api/platform/organizations',json={'name':'Сеть'}).json()['id']
    site = logged.post('/api/platform/sites',json={'name':'Магазин','organization_id':organization,'profile':'retail'}).json()['id']
    Store(settings.data)  # Migrate the existing installation.
    response = logged.post('/api/jobs/upload?site_id='+site, content=b'0000ftyp'+b'0'*24,headers={'Content-Type':'video/mp4'})
    assert response.status_code == 202
    assert store.rows('SELECT site_id FROM jobs WHERE id=?',(response.json()['id'],))[0]['site_id'] == site


@pytest.mark.parametrize('identifier', ['upload','webcam'])
def test_synthetic_source_names_are_reserved(logged, camera, identifier):
    camera['id'] = identifier
    assert logged.post('/api/cameras',json=camera).status_code == 422


def make_entry(logged, app, camera):
    assert logged.post('/api/cameras',json=camera).status_code == 201
    assert logged.put('/api/admission/entrances',json={'camera':camera['id'],'zone':'restricted'}).status_code == 200
    app.state.engine.ingest([{'id':'entry-test','camera':camera['id'],'label':'person','start_time':time.time(),'zones':['restricted']}])
    return logged.get('/api/admission').json()['entries'][0]['id']


def make_person(logged, **overrides):
    body = {'name':'Проверенный человек','category':'student','valid_from':time.time()-60,'valid_until':time.time()+3600,'basis':'Тестовое согласие',**overrides}
    response = logged.post('/api/admission/people',json=body)
    assert response.status_code == 201
    return response.json()['id']


def test_deleting_candidate_preserves_separately_verified_person(logged, app, camera):
    entry = make_entry(logged, app, camera)
    a, b = make_person(logged), make_person(logged)
    app.state.store.execute('UPDATE admission_entries SET candidate_id=? WHERE id=?',(a,entry))
    assert logged.post(f'/api/admission/entries/{entry}/decision',json={'decision':'allowed','person_id':b,'note':'Проверен документ'}).status_code == 200
    logged.delete('/api/admission/people/'+a)
    row = app.state.store.rows('SELECT * FROM admission_entries WHERE id=?',(entry,))[0]
    assert row['candidate_id'] is None and row['person_id'] == b
    assert row['decision'] == 'allowed'


@pytest.mark.parametrize('decision',['denied','dismissed'])
@pytest.mark.parametrize('foreign',['other-site','nonexistent'])
def test_all_decisions_validate_person_scope(logged, app, camera, decision, foreign):
    entry = make_entry(logged, app, camera)
    person_id = 'nonexistent'
    if foreign == 'other-site':
        site = logged.post('/api/platform/sites',json={'name':'Другой объект','organization_id':'default','profile':'education'}).json()['id']
        person_id = make_person(logged,site_id=site)
    assert logged.post(f'/api/admission/entries/{entry}/decision',json={'decision':decision,'person_id':person_id,'note':'Проверка'}).status_code == 422
    assert logged.post(f'/api/admission/entries/{entry}/decision',json={'decision':decision,'note':'Без привязки личности'}).status_code == 200


def test_completed_clip_uses_capture_time_and_detection_offsets(logged, app, camera):
    logged.post('/api/cameras',json=camera)
    app.state.engine.ingest([{'id':'captured','camera':camera['id'],'label':'person','start_time':1000,'end_time':1030,'has_clip':True}])
    job = app.state.store.rows('SELECT * FROM jobs')[0]
    app.state.engine.apply_result(job, {'detections':[{'kind':'fight','score':.9,'start_seconds':4.5,'end_seconds':9}]})
    alert = app.state.store.rows("SELECT * FROM events WHERE source='analysis'")[0]
    assert alert['started'] == 1004.5 and alert['ended'] == 1009


@pytest.mark.parametrize('raw', [b'GIF89a'+b'\x01\x00\x01\x00\x00\x00\x00;', b'#define test_width 1\n#define test_height 1\nstatic char test_bits[] = {0};'])
def test_identity_never_invokes_disallowed_parsers(raw, monkeypatch):
    def forbidden(*args):
        pytest.fail('Disallowed parser was invoked')
    monkeypatch.setattr(GifImagePlugin.GifImageFile,'_open',forbidden)
    monkeypatch.setattr(XbmImagePlugin.XbmImageFile,'_open',forbidden)
    model = IdentityModel.__new__(IdentityModel)
    with pytest.raises((ValueError,Image.UnidentifiedImageError)):
        model.encode(raw)


@pytest.mark.parametrize('format',['JPEG','PNG'])
def test_identity_still_accepts_permitted_formats(format):
    raw = io.BytesIO()
    Image.new('RGB',(64,64)).save(raw,format=format)
    class CV:
        IMREAD_COLOR = 1
        def imdecode(self,*args): return None
    model = IdentityModel.__new__(IdentityModel)
    model.cv = CV()
    with pytest.raises(ValueError,match='Invalid image'):
        model.encode(raw.getvalue())
