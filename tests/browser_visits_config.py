"""Commission two entrances and a ten-camera directed shop graph through the real browser and API."""
from pathlib import Path
import socket
import tempfile
import threading
import time

from playwright.sync_api import expect, sync_playwright
import uvicorn

from app.config import Settings
from app.main import create_app


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        settings = Settings(data=root/'data', frigate_config=root/'frigate.yml', frigate_url='http://127.0.0.1:1',
                            analyzer_url='http://127.0.0.1:1', worker_key='test-browser-worker-key-'*2,
                            username='admin', password='temp-browser-visit-123', demo=False, secure_cookie=False)
        app = create_app(settings, background=False)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            url = f'http://127.0.0.1:{listener.getsockname()[1]}'
            thread = threading.Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        raise RuntimeError('Fixture server did not start')
                    time.sleep(.05)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(args=['--no-sandbox'])
                    context = browser.new_context(base_url=url, viewport={'width':1280,'height':900})
                    session = context.request.post('/api/login', data={'username':'admin','password':settings.password})
                    assert session.ok, session.text()
                    headers = {'X-CSRF-Token':session.json()['csrf']}
                    shop = context.request.post('/api/platform/sites', data={
                        'organization_id':'default','name':'Тестовый магазин','profile':'retail'}, headers=headers)
                    assert shop.ok, shop.text()
                    site = shop.json()['id']
                    endpoint = '/api/retail/visits/' + site + '/config'
                    cameras = [f'cam_{i:02d}' for i in range(10)]
                    for i, camera in enumerate(cameras):
                        zones = [
                            {'name':'outside','restricted':False,'points':[[.05,.1],[.3,.1],[.3,.7]]},
                            {'name':'interior','restricted':False,'points':[[.5,.1],[.8,.1],[.8,.7]]},
                        ] if i == 0 else [
                            {'name':'south_entry','restricted':False,'points':[[.2,.1],[.6,.1],[.6,.6]]},
                            {'name':'staff_only','restricted':True,'points':[[.2,.7],[.5,.7],[.5,.9]]}
                        ] if i == 9 else []
                        response = context.request.post('/api/cameras', data={
                            'id':camera,'name':f'Камера {i}', 'site_id':site,'zones':zones,
                            'stream':f'rtsp://127.0.0.1/{camera}'}, headers=headers)
                        assert response.ok, response.text()
                    legacy = context.request.put(endpoint, headers=headers, data={
                        'entrance_camera':cameras[0], 'entrance_zone':'outside', 'inside_zone':'interior',
                        'camera_ids':cameras[:2], 'transitions':[{'from_camera':cameras[0],'to_camera':cameras[1]}]})
                    assert legacy.ok, legacy.text()
                    legacy_view = legacy.json()['config']
                    legacy_view.pop('entrances', None)
                    page = context.new_page()
                    # A previous deployment returned only the three scalar entrance fields.
                    page.route(url + endpoint, lambda route: route.fulfill(json={'config':legacy_view}), times=1)
                    errors = []
                    page.on('pageerror', lambda err: errors.append(str(err)))
                    page.goto('/')
                    page.locator('#site-selector').select_option(site)
                    page.locator('[data-nav=visits]').click()
                    page.locator('#visit-setup').click()
                    form = page.locator('#visit-config-form')
                    doors = form.locator('[data-visit-entrances] > [role=group]')
                    expect(doors).to_have_count(1)
                    expect(doors.first.locator('[name=entrance_camera]')).to_have_value(cameras[0])
                    expect(doors.first.locator('[name=inside_zone]')).to_have_value('interior')
                    form.locator('[data-add-entrance]').click()
                    second = doors.nth(1)
                    second.locator('[name=entrance_camera]').select_option(cameras[0])
                    second.locator('[name=entrance_zone]').select_option('outside')
                    form.locator('[type=submit]').click()
                    expect(form.locator('[data-visit-config-error]')).to_contain_text('отдельную камеру для каждого входа')
                    second.locator('[name=entrance_camera]').select_option(cameras[9])
                    expect(second.locator('[name=entrance_zone]')).to_have_value('')
                    expect(second.locator('option[value=outside]')).to_have_count(0)
                    expect(second.locator('option[value=staff_only]')).to_have_count(0)
                    second.locator('[name=entrance_zone]').select_option('south_entry')
                    expect(second.locator('[name=inside_zone] option[value=south_entry]')).to_have_count(0)
                    # The second entrance reaches the final interior camera in reverse order.
                    direction = [(cameras[i],cameras[i+1]) for i in range(8)] + [(cameras[9],cameras[8])]
                    edges = form.locator('[data-visit-edges] fieldset')
                    for index,(source,target) in enumerate(direction):
                        if index:
                            form.locator('[data-add-edge]').click()
                        edges.nth(index).locator('[data-from]').select_option(source)
                        edges.nth(index).locator('[data-to]').select_option(target)
                    form.locator('[type=submit]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    found = context.request.get(endpoint).json()['config']
                    expected_doors = [
                        {'camera':cameras[0],'entrance_zone':'outside','inside_zone':'interior'},
                        {'camera':cameras[9],'entrance_zone':'south_entry','inside_zone':None}
                    ]
                    assert found['entrances'] == expected_doors and found['state'] == 'active'
                    assert found['camera_ids'] == cameras and len(found['transitions']) == 9
                    assert found['entrance_camera'] == cameras[0] and found['inside_zone'] == 'interior'
                    page.set_viewport_size({'width':390,'height':844})
                    page.locator('#visit-setup').click()
                    expect(edges).to_have_count(9)
                    expect(doors).to_have_count(2)
                    expect(doors.first.locator('[name=inside_zone]')).to_have_value('interior')
                    expect(second.locator('[name=entrance_camera]')).to_have_value(cameras[9])
                    expect(second.locator('[name=entrance_zone]')).to_have_value('south_entry')
                    expect(second.locator('[name=inside_zone]')).to_have_value('')
                    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    for _ in direction:
                        edges.last.get_by_role('button',name='Удалить переход').click()
                    form.locator('[type=submit]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    isolated = context.request.get(endpoint).json()['config']
                    assert isolated['entrances'] == expected_doors and isolated['transitions'] == []
                    assert isolated['camera_ids'] == [cameras[0],cameras[9]]
                    page.locator('#visit-setup').click()
                    expect(edges).to_have_count(0)
                    expect(doors).to_have_count(2)
                    second.locator('[data-remove-entrance]').click()
                    expect(doors).to_have_count(1)
                    expect(doors.first.locator('[data-remove-entrance]')).not_to_be_visible()
                    form.locator('[type=submit]').click()
                    expect(form.locator('[data-visit-config-error]')).to_contain_text('добавьте переход')
                    form.locator('[data-add-edge]').click()
                    edges.first.locator('[data-from]').select_option(cameras[0])
                    edges.first.locator('[data-to]').select_option(cameras[9])
                    form.locator('[type=submit]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    assert context.request.get(endpoint).json()['config']['entrances'] == expected_doors[:1]
                    page.locator('#visit-setup').click()
                    form.locator('[data-disable-visits]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    assert context.request.get(endpoint).json() == {'config':None}
                    assert not errors, errors
                    print('PASS: legacy door preserved; 2 entrances, 10 cameras, 9 directed transitions; per-camera zones, duplicate prevention, mobile reopen, two independent entrances with no transitions, door removal and disable')
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)


if __name__ == '__main__':
    main()
