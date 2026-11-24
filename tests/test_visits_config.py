import pytest

from app.main import create_app
from app.visits_config import decode


@pytest.fixture
def graph(logged, camera):
    site = logged.post('/api/platform/sites', json={'organization_id':'default','name':'Магазин','profile':'retail'}).json()['id']
    cameras = ['entry_cam','aisle_cam','cash_cam']
    for index, cam in enumerate(cameras):
        zones = [{'name':'entry_area','points':[[.1,.1],[.6,.1],[.6,.5]],'restricted':False}] if not index else []
        assert logged.post('/api/cameras', json={**camera,'id':cam,'site_id':site,'zones':zones}).status_code == 201
    transitions = [{'from_camera':'entry_cam','to_camera':'aisle_cam'},
                   {'from_camera':'aisle_cam','to_camera':'cash_cam','min_seconds':0,'max_seconds':600},
                   {'from_camera':'cash_cam','to_camera':'entry_cam'}]
    return site, '/api/retail/visits/' + site + '/config', dict(
        entrance_camera='entry_cam', entrance_zone='entry_area', camera_ids=cameras, transitions=transitions)


def test_retry_reorder_restart_disable_are_independent_of_route_modes(graph, logged, app, settings):
    site, path, body = graph
    assert logged.get(path).json() == {'config':None}
    first = logged.put(path, json=body).json()['config']
    assert first['state'] == 'active' and first['camera_ids'] == sorted(body['camera_ids'])
    assert logged.put(path, json={**body, 'camera_ids':body['camera_ids'][::-1],
                                 'transitions':body['transitions'][::-1]}).json()['config'] == first
    restart = create_app(settings, background=False)
    stored, = restart.state.store.rows('SELECT * FROM retail_visit_configs WHERE site_id=?', (site,))
    assert decode(stored) == first
    assert logged.get('/api/retail/multicam/' + site).json() == {'rule':None}
    changed = logged.put(path, json={**body,'transitions':body['transitions'][:2]}).json()['config']
    assert changed['activated_at'] > first['activated_at']
    assert logged.delete(path).json() == {'config':None}
    assert logged.delete(path).json() == {'config':None}
    assert len(app.state.store.rows("SELECT action FROM audit WHERE action LIKE 'retail.visits.%'")) == 3


def test_large_branched_graph_and_bidirectional_transitions(graph, camera, logged):
    site, path, body = graph
    for i in range(12):
        cam = 'rear_' + str(i)
        assert logged.post('/api/cameras', json={**camera,'id':cam,'site_id':site,'zones':[]}).status_code == 201
        body['camera_ids'].append(cam)
        body['transitions'].append({'from_camera':'aisle_cam','to_camera':cam})
        body['transitions'].append({'from_camera':cam,'to_camera':'aisle_cam'})
    result = logged.put(path, json=body)
    assert result.is_success, result.text
    assert len(result.json()['config']['camera_ids']) == 15


@pytest.mark.parametrize('error', ['duplicate_camera','repeat_edge','self_edge','unreachable','outside',
                                   'virtual','wrong_bounds','missing_entrance','colliding_zone','gap_bool'])
def test_reject_invalid_graph(graph, logged, error):
    _, path, body = graph
    first = logged.put(path, json=body).json()
    if error == 'duplicate_camera':
        body['camera_ids'].append('cash_cam')
    elif error == 'repeat_edge':
        body['transitions'].append(body['transitions'][0])
    elif error == 'self_edge':
        body['transitions'][0]['to_camera'] = 'entry_cam'
    elif error == 'unreachable':
        body['transitions'] = [body['transitions'][0], body['transitions'][2]]
    elif error == 'outside':
        body['transitions'][0]['to_camera'] = 'other_cam'
    elif error == 'virtual':
        body['camera_ids'].append('upload')
    elif error == 'wrong_bounds':
        body['transitions'][0].update(min_seconds=4, max_seconds=4)
    elif error == 'missing_entrance':
        body['entrance_camera'] = 'unknown_cam'
    elif error == 'colliding_zone':
        body['entrance_zone'] = 'cash_cam'
    else:
        body['transitions'][0]['min_seconds'] = False
    assert logged.put(path, json=body).status_code == 422
    assert logged.get(path).json() == first


def test_reject_foreign_camera_restricted_zone_and_moved_member(graph, logged, app):
    site, path, body = graph
    assert logged.put(path, json=body).is_success
    store = app.state.store
    store.execute("UPDATE cameras SET site_id='default' WHERE id='cash_cam'")
    assert logged.get(path).json()['config']['reason'] == 'camera_changed'
    assert logged.put(path, json=body).status_code == 404
    store.execute("UPDATE cameras SET site_id=? WHERE id='cash_cam'", (site,))
    assert logged.put(path, json=body).json()['config']['state'] == 'active'
    zone = {'name':'entry_area','restricted':True,'points':[[.1,.1],[.9,.1],[.9,.9]]}
    assert logged.put('/api/cameras/entry_cam/zones', json={'zones':[zone]}).is_success
    assert logged.get(path).json()['config']['state'] == 'invalidated'
    assert logged.put(path, json=body).status_code == 422


def test_auth_demo_write_protection_and_delete_trigger(graph, logged, app, settings):
    from dataclasses import replace
    from fastapi.testclient import TestClient

    site, path, body = graph
    assert logged.put(path, json=body).is_success
    assert logged.delete('/api/cameras/aisle_cam').is_success
    assert logged.get(path).json()['config']['reason'] == 'camera_deleted'
    store = app.state.store
    store.execute("UPDATE sites SET profile='office' WHERE id=?", (site,))
    assert logged.get(path).json()['config']['reason'] == 'profile_changed'
    assert logged.put(path, json=body).status_code == 422
    del logged.headers['X-CSRF-Token']
    assert logged.delete(path).status_code == 403
    logged.cookies.clear()
    assert logged.get(path).status_code == 401
    with TestClient(create_app(replace(settings,demo=True,data=settings.data.parent/'demo'), background=False)) as client:
        session = client.post('/api/login', json={'username':settings.username,'password':settings.password})
        client.headers['X-CSRF-Token'] = session.json()['csrf']
        assert client.put(path, json=body).status_code == 409
        assert client.delete(path).status_code == 409


def test_inside_zone_direction_requires_disjoint_polygons_on_same_camera(graph, logged):
    _, path, body = graph
    entry = {'name':'entry_area','restricted':False,'points':[[.1,.1],[.4,.1],[.4,.4],[.1,.4]]}
    inside = {'name':'inside','restricted':False,'points':[[.5,.1],[.8,.1],[.8,.4],[.5,.4]]}
    assert logged.put('/api/cameras/entry_cam/zones', json={'zones':[entry, inside]}).is_success
    assert logged.put(path, json={**body,'inside_zone':'entry_area'}).status_code == 422
    assert logged.put(path, json={**body,'inside_zone':'missing'}).status_code == 422
    assert logged.put(path, json={**body,'inside_zone':'inside'}).json()['config']['inside_zone'] == 'inside'
    assert logged.put(path, json=body).json()['config']['inside_zone'] is None
    for polygon in [[[.4,.1],[.8,.1],[.8,.4],[.4,.4]], [[.2,.2],[.3,.2],[.3,.3]]]:
        assert logged.put('/api/cameras/entry_cam/zones', json={'zones':[entry, {**inside,'points':polygon}]}).is_success
        assert logged.put(path, json={**body,'inside_zone':'inside'}).status_code == 422


def test_migrate_legacy_visit_graph_preserves_generation_and_rows(tmp_path):
    from app.store import Store
    from app.visits_config import initialize

    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("""CREATE TABLE retail_visit_configs (site_id TEXT PRIMARY KEY, entrance_camera TEXT,
            entrance_zone TEXT,camera_ids TEXT,transitions TEXT,activated_at REAL,state TEXT,reason TEXT)""")
        db.execute("""INSERT INTO retail_visit_configs VALUES(
            's','ca','entry','["ca","cb"]','[]',321.5,'active',NULL)""")
    initialize(store)
    initialize(store)
    row, = store.rows('SELECT * FROM retail_visit_configs')
    assert row['inside_zone'] is None and row['activated_at'] == 321.5 and row['state'] == 'active'
