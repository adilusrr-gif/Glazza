"""Run against serve_ui_fixture.py --port 18083 --populated (synthetic NVR)."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18083'

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    context = browser.new_context(ignore_https_errors=True, viewport={'width':1440, 'height':1100})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE, wait_until='domcontentloaded')
    page.locator('input[name=password]').fill('map-check-password-123')
    page.get_by_role('button', name='Войти в Glazza').click()
    page.locator('#content h1').wait_for()
    csrf = context.request.get(BASE + '/api/session').json()['csrf']
    headers = {'X-CSRF-Token':csrf, 'Origin':BASE}
    status = context.request.get(BASE + '/api/cameras/status').json()
    event_id = status['alert_camera']['latest_event']['id']
    original_event = context.request.get(BASE + '/api/events/' + event_id).json()
    try:
        assert status['online_0']['signal'] == 'online'
        assert status['offline_camera']['signal'] == 'offline'
        assert status['unknown_camera']['signal'] == 'unknown'
        assert status['missing_camera']['signal'] == 'unconfigured'
        assert page.locator('body').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(255, 255, 255)'
        assert page.locator('.sidebar').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(22, 75, 154)'
        page.locator('[data-view=map]').click()
        page.locator('.leaflet-tile-loaded').first.wait_for(timeout=30000)
        page.locator('.map-camera-cluster.signal-alert').wait_for()
        assert page.locator('.map-camera-cluster.signal-alert').inner_text() == '7'
        assert page.locator('.camera-map-marker').count() + page.locator('.map-camera-cluster').count() < 10
        assert page.locator('.camera-map-marker.signal-offline').count() == 1
        page.mouse.move(0,0); page.wait_for_timeout(300)
        page.screenshot(path=str(ROOT/'data/map-status-white-desktop.png'), full_page=True)

        page.locator('[data-map-filter=alerts]').click()
        assert page.locator('[data-map-camera]').count() == 1
        page.locator('.camera-map-marker.signal-alert').click()
        page.locator('[data-latest-event]').wait_for()
        assert page.locator('[data-latest-event]').get_attribute('data-latest-event') == event_id
        assert page.locator('.map-camera-health').inner_text() == 'Работает\nТревога'
        assert event_id not in {event['id'] for event in context.request.get(BASE+'/api/events?limit=200').json()}
        with page.expect_response(lambda response: response.url == BASE+'/api/events/'+event_id):
            page.locator('[data-latest-event]').click()
        page.get_by_role('heading', name='Возможная драка', exact=True).wait_for()
        for _ in range(40):
            if page.locator('#modal video').evaluate('(el)=>el.readyState') >= 2: break
            page.wait_for_timeout(100)
        assert page.locator('#modal video').evaluate('(el)=>el.readyState') >= 2
        page.get_by_role('button', name='Подтвердить', exact=True).click()
        page.locator('#modal').wait_for(state='hidden')
        page.locator('.map-list-empty').wait_for()
        assert page.locator('[data-map-camera]').count() == 0
        page.locator('[data-map-filter=all]').click()
        assert page.locator('.map-camera-cluster.signal-alert').count() == 0
        assert page.locator('.map-camera-cluster.signal-online').count() >= 1

        page.locator('[data-map-filter=offline]').click()
        assert page.locator('[data-map-camera]').count() == 2
        assert page.locator('.camera-map-marker').count() == 1
        page.locator('#map-search').fill('координат')
        assert page.locator('[data-map-camera]').count() == 1
        assert page.locator('.camera-map-marker').count() == 0
        assert page.locator('#map-fit').is_disabled()
        page.locator('#map-search').fill('')
        page.locator('[data-map-filter=all]').click()

        # Clusters expand by keyboard; coincident markers can still be opened/dragged.
        zoom = page.evaluate('cameraMap.map.getZoom()')
        page.locator('.map-camera-cluster.signal-online').first.focus()
        page.keyboard.press('Enter')
        page.wait_for_timeout(500)
        assert page.evaluate('cameraMap.map.getZoom()') > zoom
        page.evaluate("openCameraMarker('online_0')")
        page.locator('.map-popup').wait_for()
        page.locator('.leaflet-popup-close-button').click()
        marker = page.locator('.camera-map-marker[title^="Тест · Астана 1"]')
        marker.hover()
        rect = marker.bounding_box()
        x,y = rect['x']+rect['width']/2,rect['y']+rect['height']/2
        page.mouse.move(x,y); page.mouse.down(); page.mouse.move(x+70,y+40,steps=15); page.mouse.up()
        page.locator('#map-draft-status').wait_for(state='visible')
        assert page.locator('.map-draft-marker').count() == 1
        page.locator('#map-cancel').click()
        assert page.evaluate("cameraMap.markers.get('online_0').getLatLng().lat") == 51.1694
        page.locator('#map-country').click()

        for width in [390,320]:
            page.set_viewport_size({'width':width,'height':844})
            for view in ['overview','cameras','events','analysis','system','map']:
                page.locator('[data-view='+view+']').click()
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), (width,view)
            assert page.locator('[data-map-filter=offline]').is_visible()
            page.locator('[data-map-filter=alerts]').click()
            assert page.locator('[data-map-camera]').count() == 0
            page.locator('[data-map-filter=all]').click()
            page.evaluate('scrollTo(0,0)')
            page.wait_for_timeout(250)
            page.screenshot(path=str(ROOT/f'data/map-status-white-{width}.png'),full_page=True)
        assert not errors, errors
        print(json.dumps({'result':'PASS','fixture':'synthetic NVR telemetry, JPEG and MP4','checks':['measured signal states','cluster alert priority and expansion','one status filter + search','latest event outside global page and video playback','review clears red marker','coincident marker selection/drag/cancel','white workspace and blue nav','all views at 390/320']},ensure_ascii=False))
    finally:
        context.request.patch(BASE+'/api/events/'+event_id, headers=headers, data={'status':original_event['status'], 'note':original_event['note']})
        browser.close()
