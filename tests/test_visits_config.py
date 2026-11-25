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


@pytest.fixture
def multiple_entrances(graph, logged, camera):
    site, path, legacy = graph
    door_zone = {'name':'entry_area', 'restricted':False, 'points':[[.1,.1],[.3,.1],[.3,.4],[.1,.4]]}
    inside = {'name':'inner', 'restricted':False, 'points':[[.6,.2],[.8,.2],[.8,.8],[.6,.8]]}
    for cam, zones in [('side_cam',[door_zone, inside]), ('side_hall',[])]:
        assert logged.post('/api/cameras', json={**camera,'id':cam,'site_id':site,'zones':zones}).is_success
    secondary = dict(camera='side_cam', entrance_zone='entry_area', inside_zone='inner')
    primary = dict(camera='entry_cam', entrance_zone='entry_area', inside_zone=None)
    cameras = legacy['camera_ids'] + ['side_cam','side_hall']
    edges = [*legacy['transitions'], {'from_camera':'side_cam','to_camera':'side_hall'}]
    return path, dict(entrances=[primary, secondary], camera_ids=cameras, transitions=edges)


def test_multiple_entrance_components_retries_restart_and_legacy_writes(multiple_entrances, logged, app, settings):
    path, full = multiple_entrances
    written = logged.put(path, json=full)
    assert written.is_success, written.text
    saved = written.json()['config']
    assert saved['entrances'] == full['entrances']
    assert (saved['entrance_camera'], saved['entrance_zone'], saved['inside_zone']) == ('entry_cam','entry_area',None)
    assert logged.put(path, json=full).json()['config'] == saved
    restart = create_app(settings, background=False)
    assert decode(restart.state.store.rows('SELECT * FROM retail_visit_configs')[0]) == saved
    legacy = {k:saved[k] for k in ('entrance_camera','entrance_zone','inside_zone','camera_ids','transitions')}
    assert logged.put(path, json=legacy).json()['config'] == saved
    changed = {**legacy,'transitions':legacy['transitions'] + [{'from_camera':'side_hall','to_camera':'side_cam'}]}
    assert logged.put(path, json=changed).json()['config']['entrances'] == full['entrances']
    dropping_camera = {**legacy,'camera_ids':legacy['camera_ids'][:3],
                       'transitions':[e for e in legacy['transitions'] if 'side_' not in e['from_camera']]}
    assert logged.put(path, json=dropping_camera).status_code == 422
    singleton = {**dropping_camera,'entrances':full['entrances'][:1]}
    last = logged.put(path, json=singleton)
    assert last.is_success, last.text
    assert last.json()['config']['entrances'] == full['entrances'][:1]
    assert last.json()['config']['activated_at'] > saved['activated_at']


@pytest.mark.parametrize('issue', ['island','repeat_camera','outside_graph','missing_zone','conflict','empty','missing_primary'])
def test_invalid_entrances_do_not_disturb_active_generation(multiple_entrances, logged, issue):
    import copy
    path, graph = multiple_entrances
    saved = logged.put(path, json=graph).json()['config']
    body = copy.deepcopy(graph)
    if issue == 'island':
        body['transitions'].pop()
    elif issue == 'repeat_camera':
        body['entrances'][1]['camera'] = body['entrances'][0]['camera']
    elif issue == 'outside_graph':
        body['entrances'][1]['camera'] = 'absent'
    elif issue == 'missing_zone':
        body['entrances'][1]['inside_zone'] = 'absent'
    elif issue == 'conflict':
        body['entrance_camera'] = 'side_cam'
    elif issue == 'empty':
        body['entrances'] = []
    else:
        del body['entrances']
    assert logged.put(path, json=body).status_code == 422
    assert logged.get(path).json()['config'] == saved


def test_backfill_legacy_rows_remains_idempotent_for_both_zone_versions(tmp_path):
    import json
    from app.store import Store
    from app.visits_config import configured_entrances, initialize

    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("""CREATE TABLE retail_visit_configs (site_id TEXT PRIMARY KEY, entrance_camera TEXT NOT NULL,
            entrance_zone TEXT NOT NULL,inside_zone TEXT,camera_ids TEXT,transitions TEXT,activated_at REAL,
            state TEXT,reason TEXT)""")
        db.executemany('INSERT INTO retail_visit_configs VALUES(?,?,?,?,?,?,?,?,?)', [
            ('a','cam_a','outer','inner','["cam_a","cam_b"]','[]',12.5,'active',None),
            ('b','cam_b','door',None,'["cam_b","cam_c"]','[]',16.5,'invalidated','camera_changed')])
    initialize(store)
    initialize(store)
    first, second = store.rows('SELECT * FROM retail_visit_configs ORDER BY site_id')
    assert (first['activated_at'],first['state'],first['reason']) == (12.5,'active',None)
    assert json.loads(first['entrances']) == [{'camera':'cam_a','entrance_zone':'outer','inside_zone':'inner'}]
    assert (second['activated_at'],second['state'],second['reason']) == (16.5,'invalidated','camera_changed')
    assert configured_entrances(second) == configured_entrances(decode(second)) == [
        {'camera':'cam_b','entrance_zone':'door','inside_zone':None}]


def test_reordering_entrances_updates_primary_alias_without_changing_tracking_generation(multiple_entrances, logged):
    path, body = multiple_entrances
    first = logged.put(path, json=body).json()['config']
    reverse = {**body, 'entrances': list(reversed(body['entrances']))}
    reordered = logged.put(path, json=reverse).json()['config']
    assert reordered['activated_at'] == first['activated_at']
    assert reordered['entrances'] == reverse['entrances']
    assert reordered['entrance_camera'] == 'side_cam' and reordered['inside_zone'] == 'inner'
    assert logged.get(path).json()['config'] == reordered
    legacy = {k: reordered[k] for k in ('entrance_camera','entrance_zone','inside_zone','camera_ids','transitions')}
    assert logged.put(path, json=legacy).json()['config'] == reordered


def test_only_entrances_can_operate_as_independent_cameras_without_transitions(multiple_entrances, logged):
    path, body = multiple_entrances
    doors_only = {**body, 'camera_ids':['entry_cam','side_cam'], 'transitions':[]}
    saved = logged.put(path, json=doors_only)
    assert saved.is_success, saved.text
    assert saved.json()['config']['transitions'] == []
    assert logged.put(path, json={**doors_only, 'camera_ids':[*doors_only['camera_ids'], 'side_hall']}).status_code == 422
    assert logged.get(path).json() == saved.json()
