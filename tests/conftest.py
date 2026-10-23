from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data=tmp_path / 'data', frigate_config=tmp_path / 'config/config.yml',
                    frigate_url='http://frigate:5000', analyzer_url='http://analyzer:8090',
                    worker_key='test-worker-key-' * 3, username='admin', password='test-password-12345',
                    demo=False, secure_cookie=False)


@pytest.fixture
def app(settings):
    return create_app(settings, background=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def logged(client):
    response = client.post('/api/login', json={'username': 'admin', 'password': 'test-password-12345'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return client


@pytest.fixture
def camera():
    return {'id': 'entrance', 'name': 'Главный вход', 'stream': 'rtsp://operator:secret@192.168.1.2:554/stream',
            'analyze': True, 'zones': [{'name': 'restricted', 'points': [[0,0],[1,0],[1,1],[0,1]], 'restricted': True}]}
