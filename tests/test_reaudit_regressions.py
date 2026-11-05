"""Cross-boundary regressions from the repeat audit."""
import asyncio
from dataclasses import replace
import json
import time
import uuid

import httpx
import pytest

from app.store import Store


def rolling(store, start, *, site='default'):
    identifier = str(uuid.uuid4())
    assert store.enqueue(id=identifier,camera='entrance',site_id=site,clip_start=start,clip_end=start+12)
    return store.rows('SELECT * FROM jobs WHERE id=?',(identifier,))[0]


def result(start=2,end=4):
    return {'detections':[{'kind':'fight','score':.92,'start_seconds':start,'end_seconds':end}]}


def test_distinct_fight_after_dismissal_creates_new_pending_signal(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    engine,store = app.state.engine,app.state.store
    first,second = rolling(store,1000),rolling(store,1020)
    engine.apply_result(first,result())
    previous = store.events(kind='fight')[0]
    logged.patch('/api/events/'+previous['id'],json={'status':'dismissed','note':'Проверено'})
    engine.apply_result(second,result())
    assert len(store.events(kind='fight')) == 2
    assert logged.get('/api/overview').json()['pending_fights'] == 1
    assert all(j['state']=='succeeded' for j in store.rows('SELECT state FROM jobs'))


def test_overlap_is_linked_without_reopening_reviewed_signal(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    engine,store = app.state.engine,app.state.store
    first,second = rolling(store,1000),rolling(store,1001)
    engine.apply_result(first,result())
    previous = store.events(kind='fight')[0]
    logged.patch('/api/events/'+previous['id'],json={'status':'dismissed','note':'Проверено'})
    engine.apply_result(second,result())
    assert len(store.events(kind='fight')) == 1
    assert store.events(kind='fight')[0]['status'] == 'dismissed'
    stored = json.loads(store.rows('SELECT result FROM jobs WHERE id=?',(second['id'],))[0]['result'])
    assert stored['deduplicated'][0]['event_id'] == previous['id']
    assert logged.get('/api/overview').json()['pending_fights'] == 0


def test_overlap_on_another_site_is_not_suppressed(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    store,engine = app.state.store,app.state.engine
    site = logged.post('/api/platform/sites',json={'name':'Другой объект','organization_id':'default','profile':'education'}).json()['id']
    engine.apply_result(rolling(store,1000),result())
    engine.apply_result(rolling(store,1000,site=site),result())
    assert {e['site_id'] for e in store.events(kind='fight')} == {'default',site}


@pytest.mark.parametrize('deferred',[False,True])
def test_mid_event_camera_move_keeps_capture_site_even_on_kind_change(logged,app,camera,deferred):
    logged.post('/api/cameras',json=camera)
    engine,store = app.state.engine,app.state.store
    site = logged.post('/api/platform/sites',json={'name':'Новый объект','organization_id':'default','profile':'retail'}).json()['id']
    event = {'id':'moving-track','camera':camera['id'],'label':'person','start_time':1000,'end_time':None,'zones':[]}
    engine.ingest([event])
    logged.put('/api/platform/cameras/'+camera['id']+'/site',json={'site_id':site})
    if deferred:
        for i in range(100):store.enqueue(id=f'filler-{i}',camera='upload',site_id='default')
    engine.ingest([{**event,'end_time':1030,'zones':['restricted'],'has_clip':True}])
    if deferred:
        assert not store.rows("SELECT * FROM jobs WHERE source_id='moving-track'")
        store.execute("UPDATE jobs SET state='succeeded' WHERE id='filler-0'")
        engine.enqueue_completed()
    job = store.rows("SELECT * FROM jobs WHERE source_id='moving-track'")[0]
    assert job['site_id'] == 'default'
    assert {e['site_id'] for e in store.rows("SELECT site_id FROM events WHERE source_id='moving-track'")} == {'default'}
    assert all(e['ended']==1030 for e in store.rows("SELECT ended FROM events WHERE source_id='moving-track'"))
    engine.apply_result(job,result())
    assert store.events(kind='fight')[0]['site_id'] == 'default'
    engine.ingest([{**event,'id':'new-track','end_time':1030,'has_clip':True}])
    assert store.rows("SELECT site_id FROM jobs WHERE source_id='new-track'")[0]['site_id'] == site


def test_pending_origin_survives_retention_then_expires_when_terminal(logged,app,camera,monkeypatch):
    logged.post('/api/cameras',json=camera)
    engine,store = app.state.engine,app.state.store
    now = time.time()
    captured = now-2*86400
    engine.settings = replace(engine.settings,retention_days=1)
    engine.ingest([{'id':'old-capture','camera':camera['id'],'label':'person','start_time':captured,'end_time':captured+30,'has_clip':True}])
    job = store.rows('SELECT * FROM jobs')[0]
    engine.cleanup()
    assert store.rows('SELECT id FROM events WHERE id=?',(job['event_id'],))
    engine.apply_result(job,result())
    alert = store.events(kind='fight')[0]
    assert alert['started'] == captured+2 and alert['ended'] == captured+4
    engine.cleanup()
    assert not store.rows('SELECT id FROM events WHERE id=?',(job['event_id'],))


def test_disabled_modules_release_queue_slot_without_execution(app,monkeypatch):
    engine,store = app.state.engine,app.state.store
    store.execute("UPDATE sites SET modules='[\"person\"]' WHERE id='default'")
    store.enqueue(id='disabled',camera='upload',site_id='default',now=time.time()-10)
    async def once(_):raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio,'sleep',once)
    async def run():
        with pytest.raises(asyncio.CancelledError):await engine.work()
    asyncio.run(run())
    assert store.rows("SELECT state,attempts FROM jobs WHERE id='disabled'")[0] == {'state':'failed','attempts':0}


def test_legacy_missing_capture_never_invents_recording_time(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    store,engine = app.state.store,app.state.engine
    store.enqueue(id='legacy',camera=camera['id'],site_id='default',event_id='deleted-origin',source_id='old')
    job = store.rows("SELECT * FROM jobs WHERE id='legacy'")[0]
    with pytest.raises(ValueError,match='provenance'):
        engine.apply_result(job,result())
    assert store.events(kind='fight') == []


@pytest.mark.parametrize('requested,ready,expected',[
    (['fight'],['face'],False),(['theft'],['fight'],False),(['face'],['fight'],False),
    (['fight'],['vadclip'],True),(['fight'],['fight'],True),(['theft'],['vadclip'],True),(['face'],['face'],True)])
def test_queue_matches_requested_modules_not_unrelated_model(app,requested,ready,expected):
    store,engine = app.state.store,app.state.engine
    store.execute('UPDATE sites SET modules=? WHERE id=?',(json.dumps(requested),'default'))
    engine.health['analyzer'] = {'state':'ready','models':[{'id':m,'state':'ready'} for m in ready]}
    assert engine.models_ready({'camera':'upload','site_id':'default'}) is expected


def test_waiting_job_resumes_and_does_not_block_other_ready_site(app,monkeypatch):
    engine,store = app.state.engine,app.state.store
    store.execute("UPDATE sites SET modules='[\"fight\"]' WHERE id='default'")
    store.execute("INSERT INTO sites VALUES('face-site','default','Face','office','[\"face\"]')")
    store.enqueue(id='waiting',camera='upload',site_id='default',now=time.time()-20)
    store.enqueue(id='ready',camera='upload',site_id='face-site',now=time.time()-10)
    engine.health['analyzer']={'state':'ready','models':[{'id':'face','state':'ready'}]}
    completed=[]
    async def run(job):
        completed.append(job['id']);store.execute("UPDATE jobs SET state='succeeded' WHERE id=?",(job['id'],))
    async def once(_):raise asyncio.CancelledError()
    monkeypatch.setattr(engine,'run_job',run)
    monkeypatch.setattr(asyncio,'sleep',once)
    async def iteration():
        with pytest.raises(asyncio.CancelledError):await engine.work()
    asyncio.run(iteration())
    assert completed == ['ready']
    assert store.rows("SELECT state,attempts FROM jobs WHERE id='waiting'")[0] == {'state':'queued','attempts':0}
    engine.health['analyzer']['models']=[{'id':'fight','state':'ready'}]
    asyncio.run(iteration())
    assert completed == ['ready','waiting']


@pytest.mark.parametrize('status',[429,503,'network'])
def test_transient_unavailability_does_not_exhaust_attempts(app,monkeypatch,status,settings):
    engine,store = app.state.engine,app.state.store
    identifier = str(uuid.uuid4());(settings.data/'jobs'/f'{identifier}.mp4').write_bytes(b'fixture')
    store.enqueue(id=identifier,camera='upload',site_id='default',file=identifier+'.mp4')
    def transport(request):
        if status == 'network':raise httpx.ConnectError('offline',request=request)
        return httpx.Response(status)
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    async def run():
        for _ in range(5):
            job=store.rows('SELECT * FROM jobs WHERE id=?',(identifier,))[0]
            store.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?",(identifier,))
            await engine.run_job(job)
        await engine.client.aclose()
    asyncio.run(run())
    assert store.rows('SELECT state,attempts FROM jobs WHERE id=?',(identifier,))[0] == {'state':'queued','attempts':0}
