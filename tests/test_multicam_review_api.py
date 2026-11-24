import httpx


def test_multicam_history_includes_normal_journeys_and_bypass_candidates(app, logged):
    store = app.state.store
    site = logged.post('/api/platform/sites', json={'name': 'Магазин', 'profile': 'retail',
                                                   'organization_id': 'default'}).json()['id']
    def add(source, key, kind, scope=site, details=None):
        return store.event(source=source, source_id=key, site_id=scope, kind=kind, camera='door',
                           started=100, ended=110, score=None, details=details)
    origin = add('frigate', 'exit-track', 'person')
    details = {'origin_event': origin}
    normal = add('retail_multicam', 'abc', 'retail_journey', details=details)
    candidate = add('retail_multicam', 'xyz', 'theft', details=details)
    add('retail_route', 'single', 'theft')
    other_origin = add('frigate', 'other-track', 'person', 'default')
    add('retail_multicam', 'other', 'theft', 'default')
    params = {'site_id': site, 'source': 'retail_multicam'}
    cards = logged.get('/api/events', params=params).json()
    assert {card['id'] for card in cards} == {normal, candidate}
    assert logged.get('/api/overview', params={'site_id': site}).json()['pending'] == 2
    assert logged.patch('/api/events/'+normal, json={'status': 'dismissed'}).status_code == 409
    assert logged.patch('/api/events/'+candidate, json={'status': 'dismissed'}).status_code == 200

    def frigate(request):
        assert request.url.path == '/api/events/exit-track/clip.mp4'
        return httpx.Response(200, content=b'original exit clip')
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(frigate))
    assert logged.get(f'/api/events/{normal}/media/clip').content == b'original exit clip'
    bad = add('retail_multicam', 'cross-site', 'theft', details={'origin_event': other_origin})
    assert logged.get(f'/api/events/{bad}/media/clip').status_code == 404
