import asyncio
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def stats(app, data, updated=None):
    timestamp = time.time() if updated is None else updated
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'cameras': data, 'service': {'last_updated': timestamp}, 'cpu_usages': {'secret': 'must-not-leak'}})))
    asyncio.run(app.state.engine.sync_camera_signals())


def test_status_requires_login(client):
    assert client.get('/api/cameras/status').status_code == 401
    assert client.get('/api/events/unknown').status_code == 401


@pytest.mark.parametrize('fps,expected', [(5.5, 'online'), (0, 'offline'), (None, 'unknown'), (-1, 'unknown'), ('5', 'unknown'), (True, 'unknown')])
def test_measured_fps_drives_status(logged, app, camera, fps, expected):
    logged.post('/api/cameras', json=camera)
    stats(app, {camera['id']: {'camera_fps': fps}, 'not_registered': {'camera_fps': 15}})
    response = logged.get('/api/cameras/status')
    assert list(response.json()) == [camera['id']]
    assert response.json()[camera['id']]['signal'] == expected
    assert 'secret' not in response.text and 'stream' not in response.text


def test_missing_camera_is_not_claimed_online(logged, app, camera):
    logged.post('/api/cameras', json=camera)
    assert logged.get('/api/cameras/status').json()[camera['id']]['signal'] == 'unknown'
    stats(app, {})
    assert logged.get('/api/cameras/status').json()[camera['id']]['signal'] == 'unconfigured'


@pytest.mark.parametrize('timestamp', [0, -100, float('inf'), time.time() + 3600])
def test_stale_or_invalid_nvr_stats_are_unknown(logged, app, camera, timestamp):
    logged.post('/api/cameras', json=camera)
    # The cache is also defended when its producer cannot provide a timestamp.
    app.state.engine.camera_signal_snapshot = {'cameras': {camera['id']: 5}, 'checked_at': time.time(), 'updated_at': timestamp, 'reason': None}
    assert logged.get('/api/cameras/status').json()[camera['id']]['signal'] == 'unknown'


def test_stopped_poller_cannot_leave_a_green_camera(logged, app, camera):
    logged.post('/api/cameras', json=camera)
    stats(app, {camera['id']: {'camera_fps': 5}})
    assert app.state.engine.camera_signal(camera['id'])['signal'] == 'online'
    assert app.state.engine.camera_signal(camera['id'], now=time.time() + 31)['signal'] == 'unknown'
    app.state.engine.camera_signal_snapshot = {'cameras': {}, 'checked_at': time.time(), 'updated_at': None, 'reason': 'nvr_unavailable'}
    assert app.state.engine.camera_signal(camera['id'])['signal'] == 'unknown'


def test_alerts_latest_event_and_review_outside_global_page(logged, app, camera):
    logged.post('/api/cameras', json=camera)
    now = time.time()
    store = app.state.store
    alert_id = store.event(source='frigate', source_id='alert', camera=camera['id'], kind='fight', score=.9, started=now-1000)
    latest_id = store.event(source='frigate', source_id='latest', camera=camera['id'], kind='person', score=.9, started=now-900)
    for i in range(205):
        store.event(source='frigate', source_id=str(i), camera='other', kind='person', score=.8, started=now+i)
    activity = logged.get('/api/cameras/status').json()[camera['id']]
    assert activity['pending_alerts'] == 1  # People do not create a red alert marker.
    assert activity['latest_event']['id'] == latest_id
    assert latest_id not in {e['id'] for e in logged.get('/api/events?limit=200').json()}
    assert logged.get('/api/events/' + latest_id).json()['kind'] == 'person'
    assert logged.get('/api/events/not-found').status_code == 404
    logged.patch('/api/events/' + alert_id, json={'status': 'confirmed'})
    assert logged.get('/api/cameras/status').json()[camera['id']]['pending_alerts'] == 0


def test_demo_never_reports_real_signal(settings):
    with TestClient(create_app(replace(settings, demo=True), background=False)) as client:
        client.post('/api/login', json={'username': settings.username, 'password': settings.password})
        assert all(item['signal'] == 'demo' for item in client.get('/api/cameras/status').json().values())
