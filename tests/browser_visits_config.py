"""Commission a ten-camera directed shop graph through the real browser and API."""
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
                        ] if i == 0 else []
                        response = context.request.post('/api/cameras', data={
                            'id':camera,'name':f'Камера {i}', 'site_id':site,'zones':zones,
                            'stream':f'rtsp://127.0.0.1/{camera}'}, headers=headers)
                        assert response.ok, response.text()
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda err: errors.append(str(err)))
                    page.goto('/')
                    page.locator('#site-selector').select_option(site)
                    page.locator('[data-nav=visits]').click()
                    page.locator('#visit-setup').click()
                    form = page.locator('#visit-config-form')
                    form.locator('[name=entrance_camera]').select_option(cameras[0])
                    form.locator('[name=entrance_zone]').select_option('outside')
                    form.locator('[name=inside_zone]').select_option('interior')
                    for index, camera in enumerate(cameras[1:]):
                        if index:
                            form.locator('[data-add-edge]').click()
                        row = form.locator('fieldset').nth(index)
                        row.locator('[data-from]').select_option(cameras[index])
                        row.locator('[data-to]').select_option(camera)
                    form.locator('[type=submit]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    found = context.request.get(endpoint).json()['config']
                    assert found['camera_ids'] == cameras and len(found['transitions']) == 9
                    assert found['inside_zone'] == 'interior' and found['state'] == 'active'
                    page.set_viewport_size({'width':390,'height':844})
                    page.locator('#visit-setup').click()
                    expect(form.locator('fieldset')).to_have_count(9)
                    expect(form.locator('[name=inside_zone]')).to_have_value('interior')
                    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    form.locator('[data-disable-visits]').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    assert context.request.get(endpoint).json() == {'config':None}
                    assert not errors, errors
                    print('PASS: 10 cameras, 9 directed transitions, entrance direction, save, mobile reopen and disable')
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)


if __name__ == '__main__':
    main()
