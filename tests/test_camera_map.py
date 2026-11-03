import json
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def test_old_database_migrates_without_losing_camera(tmp_path, camera):
    path = tmp_path / 'glazza.db'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY,name TEXT NOT NULL,stream TEXT NOT NULL,zones TEXT NOT NULL,analyze INTEGER NOT NULL)")
        db.execute('INSERT INTO cameras VALUES(?,?,?,?,?)', (camera['id'], camera['name'], camera['stream'], json.dumps(camera['zones']), 1))
    for _ in range(2):  # Restarts must not repeat or damage the migration.
        store = Store(tmp_path)
        assert store.cameras(private=True) == [{**camera, 'latitude': None, 'longitude': None}]


def test_location_save_persists_and_preserves_connection(logged, app, camera):
    assert logged.post('/api/cameras', json=camera).status_code == 201
    location = {'latitude': 51.1694, 'longitude': 71.4491}
    response = logged.put('/api/cameras/entrance/location', json=location)
    assert response.status_code == 200
    assert response.json()['apply_required'] is False
    assert app.state.store.cameras(private=True) == [{**camera, **location}]
    assert Store(app.state.settings.data).cameras()[0]['latitude'] == location['latitude']
    assert 'stream' not in logged.get('/api/cameras').json()[0]
    assert app.state.store.rows("SELECT action FROM audit WHERE action='camera.location'")
    assert not app.state.settings.frigate_config.exists()
    # Legacy connection edits and zone edits must preserve the saved location.
    assert logged.put('/api/cameras/entrance', json=camera).status_code == 200
    assert logged.put('/api/cameras/entrance/zones', json={'zones': camera['zones']}).status_code == 200
    assert logged.get('/api/cameras').json()[0]['longitude'] == location['longitude']
    assert logged.put('/api/cameras/entrance/location', json={'latitude': None, 'longitude': None}).status_code == 200
    assert app.state.store.cameras(private=True) == [{**camera, 'latitude': None, 'longitude': None}]


def test_create_and_edit_camera_with_location(logged, camera):
    assert logged.post('/api/cameras', json={**camera, 'latitude': 43.238, 'longitude': 76.945}).status_code == 201
    assert logged.get('/api/cameras').json()[0]['latitude'] == 43.238
    assert logged.put('/api/cameras/entrance', json={**camera, 'latitude': 0, 'longitude': 0}).status_code == 200
    assert logged.get('/api/cameras').json()[0]['longitude'] == 0


@pytest.mark.parametrize('location', [
    {'latitude': 91, 'longitude': 70}, {'latitude': -91, 'longitude': 70},
    {'latitude': 50, 'longitude': 181}, {'latitude': 50, 'longitude': -181},
    {'latitude': 50}, {'longitude': 70}, {'latitude': None, 'longitude': 70},
    {'latitude': 'NaN', 'longitude': 70}, {'latitude': 50, 'longitude': 'Infinity'},
])
def test_invalid_coordinates_leave_location_unchanged(logged, camera, location):
    initial = {'latitude': 51.1, 'longitude': 71.4}
    assert logged.post('/api/cameras', json={**camera, **initial}).status_code == 201
    assert logged.put('/api/cameras/entrance/location', json=location).status_code == 422
    assert logged.get('/api/cameras').json()[0]['latitude'] == initial['latitude']
    assert logged.post('/api/cameras', json={**camera, 'id': 'other_camera', **location}).status_code == 422


def test_location_requires_authentication_csrf_and_existing_camera(client, logged, camera):
    location = {'latitude': 50, 'longitude': 70}
    logged.post('/api/cameras', json=camera)
    assert logged.put('/api/cameras/entrance/location', json=location, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert logged.put('/api/cameras/entrance/location', json=location, headers={'Origin': 'https://other.example'}).status_code == 403
    assert logged.put('/api/cameras/missing/location', json=location).status_code == 404
    logged.post('/api/logout')
    assert client.put('/api/cameras/entrance/location', json=location).status_code == 401


def test_demo_cannot_change_location(settings):
    with TestClient(create_app(replace(settings, demo=True), background=False)) as client:
        response = client.post('/api/login', json={'username': settings.username, 'password': settings.password})
        client.headers['X-CSRF-Token'] = response.json()['csrf']
        assert client.put('/api/cameras/entrance/location', json={'latitude': 50, 'longitude': 70}).status_code == 409
