"""Boundaries between Frigate HTTP responses, tracker coverage, and asynchronous evidence."""
import asyncio
from unittest.mock import Mock
import time

import httpx
import pytest

from app import engine as runtime


def test_all_open_tracks_settle_before_watermark(app, monkeypatch):
    engine, store = app.state.engine, app.state.store
    with store.connect() as db:
        db.executemany("""INSERT INTO events(id,source_id,source,camera,kind,started)
            VALUES(?,?,'frigate','till','person',?)""",
            [(str(i), f'evt-{i:04}', 800) for i in range(505)])
    # Preserve this exact pre-existing cursor if the final track's refresh fails.
    store.execute("INSERT INTO meta(key,value) VALUES('poll_cursor','900')")
    engine.retail_watermark = 901
    missing, unavailable = 'evt-0002', 'evt-0504'
    visited = []
    resolved = []
    advanced = []
    for tracker in runtime.RETAIL_TRACKERS:
        monkeypatch.setattr(tracker, 'advance', lambda store, stamp: advanced.append(stamp))
        monkeypatch.setattr(tracker, 'invalidate_track', lambda *args: resolved.append(args[1:]))
    async def response(request):
        if request.url.path == '/api/events':
            return httpx.Response(200, json=[])
        identifier = request.url.path.split('/')[-1]
        visited.append(identifier)
        if identifier == missing:
            return httpx.Response(404)
        if identifier == unavailable:
            return httpx.Response(503)
        return httpx.Response(200, json={'id': identifier, 'label': 'person', 'camera': 'till',
                                         'start_time':800, 'end_time':None})
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(engine.sync())
    assert len(visited) == 505 and len(set(visited)) == 505
    assert advanced == [] and engine.retail_watermark is None
    assert store.rows("SELECT value FROM meta WHERE key='poll_cursor'")[0]['value'] == '900'
    assert [row[:3] for row in resolved] == [('till', missing, 800)] * len(runtime.RETAIL_TRACKERS)
    assert store.rows("SELECT ended FROM events WHERE source_id=?", (missing,))[0]['ended'] is not None


def test_configured_late_open_seen_even_before_cursor(app, logged, camera, monkeypatch):
    assert logged.post('/api/cameras', json=camera).status_code == 201
    engine, store = app.state.engine, app.state.store
    now = time.time()
    store.execute("INSERT INTO meta VALUES('poll_cursor',?)", (str(now-2),))
    old = {'id':'old', 'camera':camera['id'], 'label':'person', 'start_time':now-5000,
           'end_time':None, 'has_snapshot':True}
    recent = {**old, 'id':'delayed-final', 'start_time':now-300, 'end_time':now-290}
    calls, seen_at_advance = [], []
    monkeypatch.setattr(runtime.visits, 'configured_cameras', lambda store: {camera['id']})
    monkeypatch.setattr(runtime.visits, 'advance', lambda store, until: seen_at_advance.append(
        set(row['source_id'] for row in store.rows("SELECT source_id FROM events WHERE source='frigate'"))))
    def response(request):
        assert request.url.path == '/api/events'
        q = request.url.params
        calls.append(q)
        if q.get('in_progress') == '1':
            return httpx.Response(200, json=[old])
        if q.get('cameras') == camera['id'] and float(q['after']) < now-300:
            return httpx.Response(200, json=[recent])
        return httpx.Response(200, json=[])
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    asyncio.run(engine.sync())
    assert seen_at_advance == [{'old', 'delayed-final'}]
    assert sum(q.get('in_progress') == '1' for q in calls) == 1


def test_snapshot_transport_uses_only_verified_crops(app, monkeypatch):
    engine = app.state.engine
    stamp = time.time()-4
    job = {'camera':'till', 'source_id':'shopper', 'started':stamp-2,
           'snapshot_at':stamp, 'ended':None, 'box':[.1,.1,.2,.4]}
    engine.retail_frame_shapes['till'] = (640,480)
    signature = Mock(return_value=[2,3])
    monkeypatch.setattr(runtime, 'clothing_signature', signature)
    def request(job, *, status=200, headers, body=b'person'):
        paths = []
        def response(req):
            paths.append(req.url)
            return httpx.Response(status, headers=headers, content=body)
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        return asyncio.run(engine.snapshot_signature(job)), paths
    active = {'Content-Type':'image/jpeg','Cache-Control':'no-store','X-Frame-Time':str(stamp)}
    result, paths = request(job, headers=active)
    assert result == [2,3] and paths[0].params['crop'] == '1' and paths[0].params['bbox'] == '0'
    assert signature.call_args.kwargs == {'box':runtime.live_crop_box(job['box'], 640, 480)}
    assert request({**job, 'snapshot_at':None}, headers=active) == (None, [])
    assert request({**job, 'box':None}, headers=active) == (None, [])
    for headers in ({**active,'Cache-Control':'private, max-age=31536000'},
                    {**active,'X-Frame-Time':str(stamp-30)}, {**active,'X-Frame-Time':str(stamp+0.1)}, {**active,'Content-Length':str(runtime.MAX_IMAGE_BYTES+1)},
                    {**active,'Content-Type':'text/plain'}):
        assert request(job, headers=headers)[0] is None
    assert request(job, headers=active, status=404)[0] is None
    assert request(job, headers={**active, 'content-length':'0'},body=b'x'*(runtime.MAX_IMAGE_BYTES+1))[0] is None
    assert signature.call_count == 1
    complete = {**job,'ended':stamp+3}
    result, paths = request(complete, headers={'Content-Type':'image/webp'})
    assert result == [2,3] and paths[0].path.endswith('/snapshot-clean.webp')
    assert signature.call_args.kwargs == {'box':job['box']}
    assert request({**complete,'box':None},headers={}) == (None, [])
    assert not list((engine.settings.data/'jobs').iterdir())


def test_shared_snapshot_requested_once_and_saved_per_consumer(app, monkeypatch):
    engine = app.state.engine
    job = {'site_id':'shop','camera':'aisle','source_id':'person','ended':10,
           'snapshot_at':8,'box':[.1,.1,.2,.4]}
    calls, saved, advances = [], [], []
    for tracker in runtime.RETAIL_TRACKERS:
        monkeypatch.setattr(tracker, 'snapshot_jobs', lambda store, now, limit: [dict(job)])
        monkeypatch.setattr(tracker, 'snapshot_result', lambda store, job, value, now: saved.append(value))
        monkeypatch.setattr(tracker, 'advance', lambda store, at: advances.append(at))
    async def descriptor(request):
        calls.append(request)
        return [10]
    monkeypatch.setattr(engine, 'snapshot_signature', descriptor)
    engine.retail_watermark = 15
    asyncio.run(engine.sync_retail_snapshots())
    assert len(calls) == 1 and saved == [[10]]*2 and advances == [15,15]


@pytest.mark.parametrize('issue', ['none','stale','muted','offline','wrong_label','wrong_zone','zone_off','no_detector','stuck_detector','no_events'])
def test_coverage_requires_processing_and_live_commissioned_geometry(app, logged, camera, monkeypatch, issue):
    assert logged.post('/api/cameras',json=camera).status_code == 201
    engine = app.state.engine
    now = time.time()
    stamp = now-35 if issue=='stale' else now
    stats = {'service':{'last_updated':stamp},'cameras':{'entrance':{
        'camera_fps':5, 'detection_fps':0, 'process_fps':0 if issue=='offline' else 5,
        'detection_enabled':issue!='muted'}},
        'detectors':{'cpu':{'pid':None if issue=='no_detector' else 42,'detection_start':now-90 if issue=='stuck_detector' else 0}}}
    zone = {'enabled':issue!='zone_off','coordinates':'0,0,1,0,1,1,0,1' if issue!='wrong_zone' else '0,0,.4,0,.4,.4'}
    config = {'record':{'enabled':issue!='no_events'}, 'cameras':{'entrance':{'enabled':True,'detect':{'enabled':True},
              'objects':{'track':['dog' if issue=='wrong_label' else 'person']},'zones':{'restricted':zone}}}}
    samples = []
    for tracker in runtime.RETAIL_TRACKERS:
        monkeypatch.setattr(tracker,'configured_cameras',lambda store: {'entrance'})
        monkeypatch.setattr(tracker,'configured_zones',lambda store: {'entrance':{'restricted'}})
        monkeypatch.setattr(tracker,'record_health',lambda store,camera,stamp,healthy: samples.append(healthy))
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r:
        httpx.Response(200, json=stats if r.url.path=='/api/stats' else config)))
    asyncio.run(engine.sync_camera_signals())
    assert samples == [issue=='none']*2


def test_reclassified_person_invalidates_existing_evidence_only(app, logged, camera, monkeypatch):
    engine = app.state.engine
    assert logged.post('/api/cameras', json=camera).status_code == 201
    original = {'id':'reclassified','camera':camera['id'],'start_time':12,'end_time':20,'label':'person'}
    engine.ingest([original])
    affected = []
    for tracker in runtime.RETAIL_TRACKERS:
        monkeypatch.setattr(tracker,'invalidate_track',lambda *args: affected.append(args[1:]))
    engine.ingest([{**original,'id':'unknown','label':'cart'}, {**original,'label':'cart'}])
    assert affected == [(camera['id'], 'reclassified', 12, 20, 'default')]*2
