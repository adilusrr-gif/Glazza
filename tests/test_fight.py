import asyncio
import json
import time
import uuid

import httpx
import pytest

from analyzer.fight import fight_intervals
from app.store import Store


def test_gate_rejects_spikes_and_reports_timed_sustained_intervals():
    assert fight_intervals([.1, .99, .1, .99, .1]) == []
    result = fight_intervals([.1, .91, .95, .88, .2, .9, .92, .93])
    assert [(d['start_seconds'], d['end_seconds']) for d in result] == [(0.5, 2.5), (2.5, 4.5)]
    assert result[0]['score'] == .88
    assert all(d['review_required'] for d in result)


@pytest.mark.parametrize('scores', [[float('nan')], [float('inf')], [-.1], [1.1]])
def test_gate_rejects_invalid_scores(scores):
    with pytest.raises(ValueError):
        fight_intervals(scores)


def ready_monitor(app):
    engine = app.state.engine
    engine.health.update(frigate='online', analyzer={'state': 'ready', 'models': [{'id': 'fight', 'state': 'ready'}]})
    return engine


def test_active_person_schedules_before_event_ends_and_dedupes(logged, app, camera):
    logged.post('/api/cameras', json=camera)
    engine = ready_monitor(app)
    now = time.time()
    engine.ingest([{'id': 'live-person', 'camera': 'entrance', 'label': 'person', 'start_time': now - 60, 'end_time': None}])
    engine.schedule_fights(now)
    engine.schedule_fights(now)
    jobs = app.state.store.rows('SELECT * FROM jobs')
    assert len(jobs) == 1
    assert jobs[0]['clip_end'] - jobs[0]['clip_start'] == 12
    assert 20 <= now - jobs[0]['clip_end'] < 26
    assert jobs[0]['event_id'] is None  # Camera window, not one job per person.
    engine.schedule_fights(now + 6)
    engine.schedule_fights(now + 12)
    assert len(app.state.store.rows('SELECT * FROM jobs')) == 2
    assert engine.health['fight_monitor']['state'] == 'backlog'


def test_face_only_and_disabled_camera_do_not_claim_fight_monitoring(logged, app, camera):
    camera['analyze'] = False
    logged.post('/api/cameras', json=camera)
    engine = ready_monitor(app)
    engine.schedule_fights()
    assert engine.health['fight_monitor']['state'] == 'no_cameras'
    engine.health['analyzer']['models'] = [{'id': 'face', 'state': 'ready'}]
    engine.schedule_fights()
    assert engine.health['fight_monitor']['state'] == 'model_missing'
    assert app.state.store.rows('SELECT * FROM jobs') == []


def test_empty_window_advances_cursor_and_cursor_survives_restart(logged, app, camera, settings):
    logged.post('/api/cameras', json=camera)
    engine = ready_monitor(app)
    engine.schedule_fights(1000)
    assert app.state.store.rows('SELECT * FROM jobs') == []
    reopened = Store(settings.data)
    cursor = reopened.rows("SELECT value FROM meta WHERE key='fight_cursor:entrance'")
    assert float(cursor[0]['value']) == 978
    engine.schedule_fights(1300)
    assert engine.health['fight_monitor']['skipped_windows'] > 0
    assert reopened.rows("SELECT * FROM audit WHERE action='fight.coverage_gap'")


def test_rolling_download_uses_recording_range_and_fight_mode(logged, app, camera, settings):
    logged.post('/api/cameras', json=camera)
    engine = ready_monitor(app)
    now = time.time()
    job = {'id': str(uuid.uuid4()), 'camera': 'entrance', 'state': 'running', 'created': now, 'updated': now,
           'clip_start': 100.0, 'clip_end': 112.0, 'source_id': None, 'attempts': 0}
    app.state.store.execute('INSERT INTO jobs(id,camera,state,created,updated,clip_start,clip_end) VALUES(?,?,?,?,?,?,?)',
                           (job['id'], job['camera'], 'running', now, now, 100, 112))
    paths = []
    def transport(request):
        paths.append(request.url.path)
        if request.method == 'GET':
            return httpx.Response(200, content=b'0000ftypvideodata')
        assert json.loads(request.content)['mode'] == 'fight'
        return httpx.Response(200, json={'model': 'test', 'detections': [{'kind': 'fight', 'score': .9, 'start_seconds': 2, 'end_seconds': 5}]})
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    asyncio.run(engine.run_job(job))
    assert paths[0] == '/api/entrance/start/100.0/end/112.0/clip.mp4'
    event = app.state.store.events()[0]
    assert (event['started'], event['ended']) == (102, 105)
    assert logged.get('/api/events/' + event['id'] + '/media/clip').status_code == 200
    assert logged.get('/api/overview').json()['pending_fights'] == 1
    logged.patch('/api/events/' + event['id'], json={'status': 'dismissed', 'note': 'Игра'})
    job['clip_start'] = 106
    engine.apply_result(job, {'detections': [{'kind': 'fight', 'score': .95, 'start_seconds': 1, 'end_seconds': 3}]})
    assert len(app.state.store.events()) == 1
    assert app.state.store.events()[0]['status'] == 'dismissed'
    assert logged.get('/api/overview').json()['pending_fights'] == 0


def test_existing_database_migration_is_idempotent(settings):
    import sqlite3
    settings.data.mkdir()
    with sqlite3.connect(settings.data / 'glazza.db') as db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, camera TEXT, state TEXT, created REAL, updated REAL, file TEXT)')
        db.execute("INSERT INTO jobs(id,state) VALUES('old','succeeded')")
    store = Store(settings.data)
    Store(settings.data)
    assert store.rows('SELECT clip_start,clip_end FROM jobs')[0] == {'clip_start': None, 'clip_end': None}
