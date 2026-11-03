"""Interactive map check; isolated live app on localhost:18082, no NVR apply."""
import json
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18082'


def drag(page, locator, dx, dy):
    locator.hover()  # Wait for popup auto-pan to settle before reading its position.
    box = locator.bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=15)
    page.mouse.up()
    page.wait_for_timeout(250)


with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, ignore_https_errors=True)
    page = context.new_page()
    errors, created = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE, wait_until='domcontentloaded')
    page.locator('input[name=password]').fill('map-check-password-123')
    page.get_by_role('button', name='Войти в Glazza').click()
    page.locator('#content h1').wait_for()
    csrf = context.request.get(BASE + '/api/session').json()['csrf']
    headers = {'X-CSRF-Token': csrf, 'Origin': BASE}
    try:
        page.locator('[data-view=map]').click()
        page.locator('.leaflet-tile-loaded').first.wait_for(timeout=30000)
        point = page.evaluate('cameraMap.map.containerPointToLatLng([250,200]).wrap()')
        page.locator('#camera-map').click(position={'x': 250, 'y': 200})
        page.locator('[data-new-camera]').click()
        page.get_by_role('heading', name='Камера в выбранной точке').wait_for()
        identifier = 'map_action_' + uuid.uuid4().hex[:12]
        page.locator('#camera-form input[name=name]').fill('Интерактивная камера')
        page.locator('#camera-form input[name=id]').fill(identifier)
        page.locator('#camera-form input[name=stream]').fill('rtsp://127.0.0.1:9/test')
        with page.expect_response(lambda response: response.url.endswith('/api/cameras') and response.request.method == 'POST') as response:
            page.get_by_role('button', name='Сохранить камеру', exact=True).click()
        assert response.value.status == 201
        created.append(identifier)
        page.locator('#modal').wait_for(state='hidden')
        page.locator('.map-camera-preview').wait_for()
        assert page.locator('.map-camera-preview img').get_attribute('src').startswith('/api/cameras/' + identifier + '/snapshot')
        saved = next(c for c in context.request.get(BASE + '/api/cameras').json() if c['id'] == identifier)
        assert abs(saved['latitude'] - point['lat']) < 0.00001
        assert abs(saved['longitude'] - point['lng']) < 0.00001
        page.locator('.leaflet-popup-close-button').click()

        # Dragging edits a draft, not the saved location; cancellation restores it.
        drag(page, page.locator('.camera-map-marker').first, 80, 50)
        page.locator('#map-draft-status').wait_for(state='visible')
        assert float(page.locator('#map-latitude').input_value()) != saved['latitude']
        assert next(c for c in context.request.get(BASE + '/api/cameras').json() if c['id'] == identifier)['latitude'] == saved['latitude']
        first_draft = page.locator('#map-latitude').input_value()
        drag(page, page.locator('.map-draft-marker'), 40, -50)
        assert page.locator('#map-latitude').input_value() != first_draft
        page.locator('#map-cancel').click()
        assert page.locator('.map-draft-marker').count() == 0
        assert page.evaluate('(id)=>cameraMap.markers.get(id).getLatLng().lat', identifier) == saved['latitude']

        drag(page, page.locator('.camera-map-marker').first, 90, 45)
        new_latitude = float(page.locator('#map-latitude').input_value())
        with page.expect_response(lambda response: response.url.endswith('/location') and response.request.method == 'PUT') as response:
            page.get_by_role('button', name='Сохранить отметку', exact=True).click()
        assert response.value.status == 200
        page.locator('#map-location').wait_for(state='hidden')
        assert next(c for c in context.request.get(BASE + '/api/cameras').json() if c['id'] == identifier)['latitude'] == new_latitude

        # Real panning, wheel and keyboard interaction.
        page.locator('#map-country').click()
        page.wait_for_timeout(350)
        center = page.evaluate('cameraMap.map.getCenter()')
        box = page.locator('#camera-map').bounding_box()
        page.mouse.move(box['x'] + 90, box['y'] + 90)
        page.mouse.down(); page.mouse.move(box['x'] + 170, box['y'] + 140, steps=15); page.mouse.up()
        page.wait_for_timeout(350)
        assert page.evaluate('cameraMap.map.getCenter()') != center
        zoom = page.evaluate('cameraMap.map.getZoom()')
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(500)
        assert page.evaluate('cameraMap.map.getZoom()') > zoom
        page.locator('#camera-map').focus()
        center = page.evaluate('cameraMap.map.getCenter()')
        page.keyboard.press('ArrowRight'); page.wait_for_timeout(350)
        assert page.evaluate('cameraMap.map.getCenter()') != center

        # Expanded map preserves its instance and has working Escape.
        width = page.locator('#camera-map').bounding_box()['width']
        page.evaluate('window.mapInstance = cameraMap.map')
        page.evaluate('window.mapCenter = cameraMap.map.getCenter()')
        page.locator('#map-expand').click()
        assert page.locator('.map-layout.is-expanded').is_visible()
        assert page.locator('#camera-map').bounding_box()['width'] > width
        assert page.evaluate('cameraMap.map === window.mapInstance')
        page.wait_for_timeout(100)
        assert page.evaluate('cameraMap.map.getCenter().equals(window.mapCenter)')
        page.keyboard.press('Escape')
        assert page.locator('.map-layout.is-expanded').count() == 0

        other_id = 'map_action_' + uuid.uuid4().hex[:12]
        response = context.request.post(BASE + '/api/cameras', headers=headers, data={'id': other_id, 'name': 'Вторая камера', 'stream': 'rtsp://127.0.0.1:9/test', 'latitude': 51.1694, 'longitude': 71.4491})
        assert response.status == 201
        created.append(other_id)
        page.locator('#content h1').click()
        page.evaluate('refresh()')
        page.locator('[data-map-camera="' + other_id + '"]').wait_for()
        page.locator('#map-search').fill('Интерактивная')
        assert page.locator('[data-map-camera]').count() == 1
        assert page.evaluate('cameraMap.cluster.getLayers().length') == 1
        assert page.evaluate('(id)=>cameraMap.cluster.hasLayer(cameraMap.markers.get(id))', identifier)
        page.locator('#map-search').fill('')
        assert page.evaluate('cameraMap.cluster.getLayers().length') == 2
        page.locator('#camera-map').click(position={'x': 250, 'y': 200})
        page.locator('[data-existing-camera]').select_option(other_id)
        page.locator('[data-place-camera]').click()
        assert page.locator('#map-location h2').inner_text() == 'Вторая камера'
        assert page.locator('.map-draft-marker').is_visible()
        page.locator('#map-cancel').click()
        page.locator('#map-country').click()
        page.wait_for_timeout(400)
        page.screenshot(path=str(ROOT / 'data/map-interactions-desktop.png'), full_page=True)

        touch = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, ignore_https_errors=True, storage_state=context.storage_state())
        mobile = touch.new_page()
        mobile.on('pageerror', lambda error: errors.append(str(error)))
        mobile.goto(BASE, wait_until='domcontentloaded')
        mobile.locator('#content h1').wait_for()
        mobile.locator('[data-view=map]').tap()
        mobile.evaluate('window.mapCenter = cameraMap.map.getCenter()')
        mobile.locator('#map-expand').tap()
        mobile.wait_for_timeout(100)
        assert mobile.evaluate('cameraMap.map.getCenter().equals(window.mapCenter)')
        assert mobile.locator('.map-layout.is-expanded').is_visible()
        assert not mobile.evaluate('document.documentElement.scrollWidth>innerWidth')
        # Two physical touch points exercise Leaflet's pinch handler.
        box = mobile.locator('#camera-map').bounding_box()
        x, y = box['x'] + box['width']/2, box['y'] + 170
        client = touch.new_cdp_session(mobile)
        initial_zoom = mobile.evaluate('cameraMap.map.getZoom()')
        client.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[{'x':x-25,'y':y,'id':0},{'x':x+25,'y':y,'id':1}]})
        for spread in [35,45,55,65,75]:
            client.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[{'x':x-spread,'y':y,'id':0},{'x':x+spread,'y':y,'id':1}]})
        client.send('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
        mobile.wait_for_timeout(500)
        assert mobile.evaluate('cameraMap.map.getZoom()') > initial_zoom
        mobile.locator('#camera-map').tap(position={'x':100,'y':200})
        mobile.locator('[data-new-camera]').wait_for()
        mobile.wait_for_timeout(300)
        mobile.screenshot(path=str(ROOT / 'data/map-interactions-mobile.png'), full_page=True)
        mobile.locator('.leaflet-popup-close-button').tap()
        mobile.locator('#map-expand').tap()
        assert mobile.locator('.map-layout.is-expanded').count() == 0
        touch.close()
        assert not errors, errors
        print(json.dumps({'result':'PASS','checks':['create camera at clicked point','camera snapshot request','drag draft / cancel / save','pan / wheel / keyboard','filter both markers and list','place existing camera from map','expanded map / Escape','touch pinch and point popup']}, ensure_ascii=False))
    finally:
        for identifier in created:
            response = context.request.delete(BASE + '/api/cameras/' + identifier, headers=headers)
            assert response.status == 200, response.text()
        browser.close()
