import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import uuid

import httpx
import pytest

from app.store import Store


def retail_camera(logged, app, camera):
    store = app.state.store
    store.execute("INSERT INTO sites VALUES('shop','default','Магазин','retail','[\"person\",\"theft\"]')")
    assert logged.post('/api/cameras', json={**camera, 'site_id': 'shop'}).status_code == 201
    engine = app.state.engine
    engine.health.update(frigate='online', analyzer={'state': 'ready', 'models': [{'id': 'vadclip', 'state': 'ready'}]})
    return engine, store


def person(identifier='track', start=945, end=None):
    return {'id': identifier, 'label': 'person', 'camera': 'entrance', 'start_time': start,
            'end_time': end, 'has_clip': end is not None}


def response(*intervals, duration=12):
    return {'model': 'test-retail', 'duration_seconds': duration, 'checked': ['theft'],
            'detections': [{'kind': 'theft', 'label': 'Shoplifting', 'score': .94,
                            'start_seconds': start, 'end_seconds': end} for start, end in intervals]}


def test_live_theft_only_scopes_module_clip_and_evidence(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person(), person('another')])
    engine.schedule_theft(1000)
    engine.schedule_theft(1000)
    engine.schedule_fights(1000)
    jobs = store.rows('SELECT * FROM jobs')
    assert len(jobs) == 1
    job = jobs[0]
    assert (job['rolling_module'], job['clip_start'], job['clip_end']) == ('theft', 966, 978)
    assert engine.health['theft_monitor']['camera_ids'] == ['entrance']
    assert engine.health['fight_monitor']['state'] == 'no_cameras'
    requests = []
    def transport(request):
        requests.append(request.url.path)
        if request.method == 'GET':
            return httpx.Response(200, content=b'video from NVR')
        assert json.loads(request.content) == {'job_id': job['id'], 'mode': 'all', 'modules': ['theft']}
        return httpx.Response(200, json=response((2.5, 5.5)))
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    asyncio.run(engine.run_job(job))
    assert requests == [f"/api/entrance/start/{job['clip_start']}/end/{job['clip_end']}/clip.mp4", '/analyze']
    alert, = store.events(kind='theft')
    assert (alert['site_id'], alert['started'], alert['ended']) == ('shop', 968.5, 971.5)
    assert logged.get('/api/events/' + alert['id'] + '/media/clip').content == b'video from NVR'
    assert Store(engine.settings.data).rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'


def test_reprocessed_tracks_windows_and_clip_episodes(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person(), person('other')])
    engine.schedule_theft(1000)
    rolling, = store.rows('SELECT * FROM jobs')
    engine.apply_result(rolling, response((2, 6)))
    first, = store.events(kind='theft')
    assert logged.patch('/api/events/' + first['id'], json={'status': 'dismissed', 'note': 'обычная покупка'}).status_code == 200
    engine.ingest([person(start=945, end=980), person('other', start=945, end=980)])
    clips = store.rows('SELECT * FROM jobs WHERE source_id IS NOT NULL')
    assert len(clips) == 2
    # The completed tracks cover the same shelf episode and a later separate episode.
    def apply(job):
        engine.apply_result(job, response((24, 30), (31, 33), duration=35))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(apply, clips))
    latest, reviewed = store.events(kind='theft')
    assert reviewed['id'] == first['id'] and reviewed['status'] == 'dismissed'
    assert reviewed['details']['seen_until'] == 975
    assert reviewed['details']['job_id'] == rolling['id']
    assert latest['status'] == 'new' and latest['started'] == 976
    apply(clips[0])
    assert len(store.events(kind='theft')) == 2
    for job in clips:
        result = json.loads(store.rows('SELECT result FROM jobs WHERE id=?', (job['id'],))[0]['result'])
        assert result['deduplicated'][0]['event_id'] == first['id']


def test_theft_live_needs_site_module_camera_and_theft_model(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    engine.health['analyzer']['models'] = [{'id': 'fight', 'state': 'ready'}]
    engine.schedule_theft(1000)
    assert engine.health['theft_monitor']['state'] == 'model_missing'
    engine.health['analyzer']['models'][0]['id'] = 'vadclip'
    engine.health['frigate'] = 'offline'
    engine.schedule_theft(1000)
    assert engine.health['theft_monitor']['state'] == 'nvr_offline'
    engine.health['frigate'] = 'online'
    store.execute("UPDATE cameras SET site_id='default'")
    engine.schedule_theft(1000)
    assert engine.health['theft_monitor']['state'] == 'no_cameras'
    assert not store.rows('SELECT * FROM jobs')
    store.execute("UPDATE cameras SET site_id='shop',analyze=0")
    engine.schedule_theft(1000)
    assert not store.rows('SELECT * FROM jobs')
    store.execute('UPDATE cameras SET analyze=1')
    engine.schedule_theft(1000)
    engine.schedule_theft(1006)
    engine.schedule_theft(1012)
    assert len(store.rows('SELECT * FROM jobs')) == 2
    assert engine.health['theft_monitor']['busy_cameras'] == ['entrance']
    store.execute("UPDATE jobs SET state='succeeded'")
    engine.schedule_theft(1500)
    assert engine.health['theft_monitor']['skipped_windows'] > 0
    assert store.rows("SELECT * FROM audit WHERE action='theft.coverage_gap'")


def test_incomplete_result_does_not_become_clean_video(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    engine.schedule_theft(1000)
    job, = store.rows('SELECT * FROM jobs')
    outcome = response()
    outcome.update(checked=[], theft={'status': 'insufficient_samples'})
    engine.apply_result(job, outcome)
    engine.apply_result(job, outcome)
    failed = store.rows('SELECT * FROM jobs')[0]
    assert failed['state'] == 'failed'
    assert json.loads(failed['result'])['checked'] == []
    assert engine.health['theft_monitor']['skipped_windows'] == 1
    assert not store.events(kind='theft')


@pytest.mark.parametrize('interval', [(3, 3), (-1, 2), (6, 5), (2, 13), (float('nan'), 3)])
def test_invalid_interval_writes_no_partial_cards(logged, app, camera, interval):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    engine.schedule_theft(1000)
    job, = store.rows('SELECT * FROM jobs')
    with pytest.raises(ValueError):
        engine.apply_result(job, response((0, 2), interval))
    assert not store.events(kind='theft')


def test_distinct_uploads_have_independent_reviews(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    for _ in range(2):
        identifier = str(uuid.uuid4())
        store.enqueue(id=identifier, camera='upload', site_id='shop', now=1000)
        job, = store.rows('SELECT * FROM jobs WHERE id=?', (identifier,))
        engine.apply_result(job, response((0, 2), (4, 6)))
    assert len(store.events(kind='theft')) == 4


def test_site_move_filters_old_track_out_of_new_windows(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    store.execute("INSERT INTO sites VALUES('shop2','default','Второй магазин','retail','[\"theft\"]')")
    store.execute("UPDATE cameras SET site_id='shop2'")
    engine.schedule_theft(1000)
    assert not store.rows('SELECT * FROM jobs')
    engine.ingest([person('new-track')])
    engine.schedule_theft(1006)
    assert store.rows('SELECT site_id FROM jobs') == [{'site_id': 'shop2'}]


def test_gap_does_not_hide_valid_interval_or_claim_complete_coverage(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    engine.schedule_theft(1000)
    job, = store.rows('SELECT * FROM jobs')
    outcome = response((2, 3))
    outcome.update(checked=[], theft={'status': 'incomplete_coverage'})
    engine.apply_result(job, outcome)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'failed'
    assert [e['started'] for e in store.events(kind='theft')] == [968]
    assert engine.health['theft_monitor']['skipped_windows'] == 1


def test_truncated_camera_export_never_claims_complete_or_assigns_capture_time(logged, app, camera):
    engine, store = retail_camera(logged, app, camera)
    engine.ingest([person()])
    engine.schedule_theft(1000)
    job, = store.rows('SELECT * FROM jobs')
    engine.apply_result(job, response((3, 4), duration=5))
    stored, = store.rows('SELECT state,result FROM jobs')
    result = json.loads(stored['result'])
    assert stored['state'] == 'failed'
    assert result['checked'] == [] and result['theft']['status'] == 'incomplete_recording'
    assert not store.events(kind='theft')
