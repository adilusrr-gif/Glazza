"""Opt-in integration check against an isolated live-mode app on localhost:18082.

Creates temporary camera records and removes them; never applies NVR configuration.
"""
import json
import os
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv('GLAZZA_MAP_TEST_URL', 'http://127.0.0.1:18082')
PASSWORD = os.getenv('GLAZZA_MAP_TEST_PASSWORD', 'map-check-password-123')

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1100})
    page = context.new_page()
    errors, csp_errors, tile_requests, created = [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: csp_errors.append(message.text) if 'Content Security Policy' in message.text else None)
    page.on('request', lambda request: tile_requests.append(request) if request.url.startswith('https://tile.openstreetmap.org/') else None)
    page.goto(BASE, wait_until='domcontentloaded')
    page.locator('input[name=password]').fill(PASSWORD)
    page.get_by_role('button', name='Войти в Glazza').click()
    page.get_by_role('heading', name='Всё под наблюдением.').wait_for()
    csrf = context.request.get(BASE + '/api/session').json()['csrf']
    headers = {'X-CSRF-Token': csrf, 'Origin': BASE}
    try:
        page.locator('[data-view=map]').click()
        page.locator('#camera-map .leaflet-tile-loaded').first.wait_for(timeout=30000)
        assert page.locator('#map-camera-list').inner_text()
        for name, latitude, longitude in [('Астана', 51.1694, 71.4491), ('Алматы', 43.2389, 76.8897), ('Без координат', None, None)]:
            identifier = 'map_test_' + uuid.uuid4().hex[:12]
            response = context.request.post(BASE + '/api/cameras', headers=headers, data={
                'id': identifier, 'name': 'Проверка карты · ' + name,
                'stream': 'rtsp://127.0.0.1:9/test', 'zones': [], 'analyze': False,
                'latitude': latitude, 'longitude': longitude,
            })
            assert response.status == 201, response.text()
            created.append(identifier)
        page.evaluate('refresh()')
        page.locator('[data-map-camera="' + created[2] + '"]').wait_for()
        assert page.locator('.camera-map-marker').count() >= 2
        page.screenshot(path=str(ROOT / 'data/map-kazakhstan-check.png'), full_page=True)
        page.locator('[data-map-camera="' + created[2] + '"]').click()
        page.locator('#camera-map').click(position={'x': 260, 'y': 220})
        assert page.locator('#map-latitude').input_value()
        assert page.evaluate('cameraMap.preview !== null')
        # Polling must preserve the map, viewport, selected camera and unsaved draft.
        page.evaluate('window.mapBeforeRefresh = cameraMap.map; window.mapCenterBefore = cameraMap.map.getCenter();')
        draft = page.locator('#map-latitude').input_value()
        page.evaluate('refresh()')
        assert page.evaluate('cameraMap.map === window.mapBeforeRefresh && cameraMap.map.getCenter().equals(window.mapCenterBefore)')
        assert page.locator('#map-latitude').input_value() == draft
        page.locator('#map-latitude').fill('47.0945')
        page.locator('#map-longitude').fill('51.9238')
        with page.expect_response(lambda response: response.url.endswith('/location') and response.request.method == 'PUT') as saved:
            page.get_by_role('button', name='Сохранить отметку', exact=True).click()
        assert saved.value.status == 200
        page.locator('#map-location').wait_for(state='hidden')
        page.reload(wait_until='domcontentloaded')
        page.get_by_role('heading', name='Всё под наблюдением.').wait_for()
        page.locator('[data-view=map]').click()
        page.locator('[data-map-camera="' + created[2] + '"]').click()
        assert page.locator('#map-latitude').input_value() == '47.0945'
        page.locator('#map-cancel').click()
        page.locator('#map-fit').click()
        page.wait_for_timeout(600)
        marker = page.locator('.camera-map-marker').filter(has=page.locator('span')).first
        marker.click()
        page.locator('.map-popup [data-open]').click()
        page.locator('#zone-canvas').wait_for()
        page.locator('#camera-location').click()
        assert not page.locator('#modal').evaluate('(node) => node.open')
        assert page.locator('#map-location').is_visible()
        page.locator('[data-map-camera="' + created[2] + '"]').click()
        with page.expect_response(lambda response: response.url.endswith('/location') and response.request.method == 'PUT') as removed:
            page.locator('#map-remove-location').click()
        assert removed.value.status == 200
        page.locator('#map-location').wait_for(state='hidden')
        camera = next(c for c in context.request.get(BASE + '/api/cameras').json() if c['id'] == created[2])
        assert camera['latitude'] is None and camera['longitude'] is None
        page.locator('#map-search').fill('Без координат')
        assert page.locator('[data-map-camera]').count() == 1
        page.locator('#map-search').fill('')
        for width in [390, 320]:
            page.set_viewport_size({'width': width, 'height': 844})
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
            page.locator('[data-map-camera="' + created[0] + '"]').click()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
            page.screenshot(path=str(ROOT / f'data/map-mobile-{width}.png'), full_page=True)
            page.locator('#map-cancel').click()
        # Returning to another view must release Leaflet and its event handlers.
        page.locator('[data-view=cameras]').click()
        assert page.evaluate('cameraMap.map === null')
        page.locator('[data-nav=map]').click()
        assert page.locator('#camera-map').is_visible()
        assert tile_requests
        assert tile_requests[0].all_headers().get('referer') == BASE + '/'
        # A failed external map connection must not hide local camera data.
        page.locator('[data-view=cameras]').click()
        page.route('https://tile.openstreetmap.org/**', lambda route: route.abort())
        page.locator('[data-nav=map]').click()
        page.locator('#map-tile-error').wait_for(state='visible')
        page.locator('[data-map-camera="' + created[0] + '"]').click()
        assert page.locator('#map-latitude').input_value() == '51.1694'
        page.unroute('https://tile.openstreetmap.org/**')
        page.locator('#mobile-logout').click()
        page.locator('input[name=password]').fill(PASSWORD)
        page.get_by_role('button', name='Войти в Glazza').click()
        page.locator('#camera-map .leaflet-control-zoom').wait_for()
        csrf = context.request.get(BASE + '/api/session').json()['csrf']
        headers['X-CSRF-Token'] = csrf
        assert page.evaluate('cameraMap.map !== null')
        assert not errors, errors
        assert not csp_errors, csp_errors
        print(json.dumps({'result': 'PASS', 'checks': ['real map tiles', 'camera placement', 'save and reload', 'remove location', 'camera settings', 'polling preserves draft and viewport', 'search', 'mobile 390/320', 'CSP and tile referrer', 'navigation cleanup']}, ensure_ascii=False))
    finally:
        for identifier in created:
            response = context.request.delete(BASE + '/api/cameras/' + identifier, headers=headers)
            assert response.status == 200, response.text()
        browser.close()
