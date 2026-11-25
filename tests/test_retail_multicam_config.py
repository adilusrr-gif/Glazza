import pytest

from app.main import create_app


@pytest.fixture
def topology(logged, camera):
    site = logged.post('/api/platform/sites', json={'organization_id':'default', 'name':'Магазин', 'profile':'retail'}).json()['id']
    body = {}
    for role, camera_id in [('shelf','aisle_cam'),('checkout','till_cam'),('exit','door_cam')]:
        zone = {'name':'passage', 'restricted':False, 'points':[[.1,.1],[.8,.1],[.8,.8],[.1,.8]]}
        response = logged.post('/api/cameras', json={**camera, 'site_id':site, 'id':camera_id,'zones':[zone]})
        assert response.status_code == 201, response.text
        body[role + '_camera'] = camera_id
        body[role + '_zone'] = zone['name']
    return site, '/api/retail/multicam/' + site, body


def test_activation_is_site_scoped_idempotent_and_durable(topology, logged, app, settings):
    site, path, body = topology
    assert logged.get(path).json() == {'rule':None}
    first = logged.put(path, json=body).json()['rule']
    assert first == {**body, 'site_id':site, 'state':'active', 'reason':None, 'min_seconds':2,
                     'max_seconds':60, 'activated_at':first['activated_at']}
    assert logged.put(path, json=body).json()['rule'] == first
    assert logged.get('/api/retail/multicam/default').json() == {'rule':None}
    assert logged.put('/api/retail/multicam/default', json=body).status_code == 422
    restart = create_app(settings, background=False)
    assert restart.state.store.rows('SELECT * FROM retail_multicam_routes WHERE site_id=?', (site,)) == [first]
    changed = logged.put(path, json={**body,'min_seconds':0, 'max_seconds':600}).json()['rule']
    assert changed['activated_at'] > first['activated_at']
    assert logged.delete(path).json() == {'rule':None}
    assert logged.delete(path).json() == {'rule':None}
    assert [r['action'] for r in app.state.store.rows('SELECT action FROM audit WHERE action LIKE ? ORDER BY id',
                                                     ('retail.multicam.%',))] == [
        'retail.multicam.configure', 'retail.multicam.configure', 'retail.multicam.disable']


@pytest.mark.parametrize('wrong', [
    {'exit_camera':'aisle_cam'}, {'shelf_zone':'absent'}, {'min_seconds':-1},
    {'min_seconds':60}, {'max_seconds':601}, {'min_seconds':True}, {'enabled':True},
])
def test_invalid_config_does_not_overwrite_active_rule(topology, logged, wrong):
    _, path, body = topology
    initial = logged.put(path, json=body).json()
    assert logged.put(path, json={**body, **wrong}).status_code == 422
    assert logged.get(path).json() == initial


def test_access_and_cross_site_camera_rejection(topology, logged, app, camera):
    _, path, body = topology
    assert logged.post('/api/cameras', json={**camera,'id':'foreign'}).status_code == 201
    assert logged.put(path, json={**body,'exit_camera':'foreign'}).status_code == 404
    assert logged.get('/api/retail/multicam/missing').status_code == 404
    assert logged.delete('/api/retail/multicam/missing').status_code == 404
    del logged.headers['X-CSRF-Token']
    assert logged.put(path, json=body).status_code == 403
    assert logged.delete(path).status_code == 403
    logged.cookies.clear()
    assert logged.get(path).status_code == 401


def test_camera_edit_revalidation_and_profile_change(topology, logged, app):
    site, path, body = topology
    original = logged.put(path, json=body).json()['rule']
    store = app.state.store
    store.execute("UPDATE cameras SET name='Новое имя' WHERE id=?", (body['shelf_camera'],))
    assert logged.get(path).json()['rule'] == original
    subject = '/api/cameras/' + body['checkout_camera'] + '/zones'
    zone = {'name':'passage', 'points':[[.1,.1],[.7,.1],[.7,.7],[.1,.7]],'restricted':True}
    assert logged.put(subject, json={'zones':[zone]}).is_success
    assert logged.get(path).json()['rule']['reason'] == 'camera_changed'
    assert logged.put(path, json=body).status_code == 422
    zone.update(points=[[0,0],[1,.8],[.1,1],[.9,0],[0,.8]], restricted=False)
    assert logged.put(subject, json={'zones':[zone]}).is_success
    assert logged.put(path, json=body).status_code == 422
    zone.update(points=[[0,0],[1,0],[1,1]])
    assert logged.put(subject, json={'zones':[zone]}).is_success
    updated = logged.put(path, json=body).json()['rule']
    assert updated['state'] == 'active' and updated['activated_at'] > original['activated_at']
    store.execute("UPDATE sites SET profile='office' WHERE id=?", (site,))
    assert logged.get(path).json()['rule']['reason'] == 'profile_changed'
    assert logged.put(path, json=body).status_code == 422
    assert logged.delete(path).is_success


@pytest.mark.parametrize('operation', ['move', 'delete', 'stream'])
def test_camera_lifecycle_invalidates_without_rearming_or_changing_other_site(topology, logged, app, operation):
    _, path, body = topology
    assert logged.put(path, json=body).is_success
    store = app.state.store
    camera_id = body['exit_camera']
    if operation == 'move':
        store.execute("UPDATE cameras SET site_id='default' WHERE id=?", (camera_id,))
    elif operation == 'delete':
        assert logged.delete('/api/cameras/' + camera_id).is_success
    else:
        store.execute("UPDATE cameras SET stream='rtsp://camera2.local/live' WHERE id=?", (camera_id,))
    saved = logged.get(path).json()['rule']
    assert saved['state'] == 'invalidated'
    assert saved['reason'] == ('camera_deleted' if operation == 'delete' else 'camera_changed')
    if operation != 'stream':
        assert logged.put(path, json=body).status_code == 404


def test_concurrent_retries_create_one_generation_and_one_audit(topology, app, logged):
    from concurrent.futures import ThreadPoolExecutor

    _, path, body = topology
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: logged.put(path, json=body), range(4)))
    assert all(r.is_success for r in responses)
    assert len({r.json()['rule']['activated_at'] for r in responses}) == 1
    assert app.state.store.rows("SELECT count(*) AS n FROM audit WHERE action='retail.multicam.configure'")[0]['n'] == 1
