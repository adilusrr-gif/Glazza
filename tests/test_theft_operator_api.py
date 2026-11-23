import httpx
import pytest


def signal(store, identifier, site='default', source='analysis', details=None):
    return store.event(source=source, source_id=identifier, site_id=site, kind='theft', camera='aisle',
                       score=None, started=100, ended=112, details=details)


def test_pending_counts_cover_full_backlog_and_preserve_other_review(logged, app):
    store = app.state.store
    site = logged.post('/api/platform/sites', json={
        'name': 'Магазин', 'profile': 'retail', 'organization_id': 'default'}).json()['id']
    alert = signal(store, 'shelf', site)
    signal(store, 'another-site')
    for i in range(202):
        store.event(source='frigate', source_id=str(i), site_id=site, kind='person', camera='aisle',
                    score=None, started=200 + i, ended=210 + i)
    assert logged.get('/api/overview', params={'site_id': site}).json()['pending_thefts'] == 1
    assert logged.get('/api/overview').json()['pending_thefts'] == 2
    assert [e['id'] for e in logged.get('/api/events', params={
        'site_id': site, 'kind': 'theft', 'status': 'new'}).json()] == [alert]
    path = f'/api/events/{alert}'
    note = {'status': 'new', 'note': 'Нужен второй ракурс', 'expected_status': 'new', 'expected_note': ''}
    assert logged.patch(path, json=note).status_code == 200
    assert logged.patch(path, json={**note, 'status': 'confirmed'}).status_code == 409
    verdict = {'status': 'dismissed', 'note': 'Человек вышел без товара',
               'expected_status': 'new', 'expected_note': note['note']}
    assert logged.patch(path, json=verdict).status_code == 200
    assert logged.patch(path, json=verdict).status_code == 200
    assert logged.patch(path, json={**verdict, 'status': 'confirmed'}).status_code == 409
    assert len(store.rows("SELECT 1 FROM audit WHERE subject=? AND action='event.review'", (alert,))) == 2
    assert logged.get('/api/overview', params={'site_id': site}).json()['pending_thefts'] == 0
    assert logged.get('/api/overview').json()['pending_thefts'] == 1


@pytest.mark.parametrize('source', ['retail_route', 'analysis'])
def test_retained_nvr_origin_serves_evidence_after_job_file_expires(logged, app, source):
    store = app.state.store
    origin = store.event(source='frigate', source_id='abc', site_id='default', kind='person',
                         camera='aisle', score=.9, started=100, ended=112)
    alert = signal(store, 'shelf', source=source, details={'origin_event': origin, 'job_id': 'expired'})
    def frigate(request):
        assert request.url.path == '/api/events/abc/clip.mp4'
        assert request.headers['Range'] == 'bytes=0-3'
        return httpx.Response(206, headers={'content-range': 'bytes 0-3/512'}, content=b'clip')
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(frigate))
    response = logged.get(f'/api/events/{alert}/media/clip', headers={'Range': 'bytes=0-3'})
    assert response.status_code == 206
    assert response.headers['content-range'] == 'bytes 0-3/512'
    assert response.content == b'clip'


def test_rolling_clip_uses_original_window_when_local_file_expired(logged, app):
    store = app.state.store
    store.enqueue(id='window', camera='aisle', site_id='default', clip_start=100, clip_end=112)
    alert = signal(store, 'candidate', details={'job_id': 'window', 'rolling': True})
    def frigate(request):
        assert request.url.path == '/api/aisle/start/100.0/end/112.0/clip.mp4'
        return httpx.Response(200, content=b'original window')
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(frigate))
    assert logged.get(f'/api/events/{alert}/media/clip').content == b'original window'
