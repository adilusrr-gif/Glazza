"""Isolated Chromium regressions: alarms survive forms, refreshes and map state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18327'


def refresh_page(page):
    # Chromium pinned in CI applies CSP to wait_for_function's injected eval.
    # Direct evaluate is supported without changing the product policy.
    deadline = time.monotonic() + 10
    while page.evaluate('refreshing'):
        if time.monotonic() >= deadline:
            raise AssertionError('Previous refresh did not finish')
        page.wait_for_timeout(50)
    page.evaluate('refresh()')


def check_retail_review(page, store):
    # The operator must find older, unresolved alerts even when the unfiltered
    # overview only loaded the latest 200 person events.
    now = time.time()
    store.execute("INSERT INTO sites VALUES(?,?,?,?,?)", ('retail-fixture','default','Тест магазина','retail','["person","theft"]'))
    zones = [
        {'name':name,'restricted':False,'points':[[left,.1],[left+.15,.1],[left+.15,.3],[left,.3]]}
        for name,left in [('shelf_a',.1),('tills',.4),('door',.7)]
    ]
    store.execute("INSERT INTO cameras(id,name,stream,site_id,zones) VALUES(?,?,?,?,?)",
                  ('retail_cam','Торговый зал','rtsp://127.0.0.1/test','retail-fixture',json.dumps(zones)))
    route = store.event(source='retail_route', source_id='retail-track', camera='retail_cam', site_id='retail-fixture',
                        kind='theft', score=None, started=now-800, ended=now-790,
                        details={'signal':'shelf_to_exit','shelf_zone':'shelf_a','checkout_zone':'tills',
                                 'exit_zone':'door','start_seconds':2,'end_seconds':8})
    for index in range(201):
        store.event(source='frigate', source_id=f'retail-person-{index}', camera='retail_cam', site_id='retail-fixture',
                    kind='person', score=.8, started=now-300+index)
    page.evaluate("navigate('overview')")
    refresh_page(page)
    page.locator('#site-selector').select_option('default')
    page.locator('#open-thefts').wait_for(state='hidden')
    page.locator('#site-selector').select_option('retail-fixture')
    page.locator('#retail-monitor').wait_for()
    assert page.locator('.theft-alert-count').inner_text() == '1'
    page.locator('[data-action=retail-setup]').click()
    page.locator('#route-form select[name=shelf_zone]').select_option('shelf_a')
    page.locator('#route-form select[name=checkout_zone]').select_option('shelf_a')
    page.locator('#route-form select[name=exit_zone]').select_option('door')
    page.get_by_role('button',name='Включить маршрут').click()
    assert 'разные зоны' in page.locator('[data-route-error]').inner_text()
    page.locator('#route-form select[name=checkout_zone]').select_option('tills')
    page.get_by_role('button',name='Включить маршрут').click()
    page.get_by_text('Маршрут включён',exact=True).wait_for()
    page.locator('#modal-close').click()
    refresh_page(page)
    assert 'Камер с маршрутом: 1' in page.locator('#retail-monitor').inner_text()
    page.locator('[data-action=retail-setup]').click()
    assert page.locator('#route-form select[name=checkout_zone]').input_value() == 'tills'
    page.get_by_role('button',name='Отключить маршрут').click()
    page.get_by_text('Маршрут выключен',exact=True).wait_for()
    page.locator('#modal-close').click()
    refresh_page(page)
    assert 'Маршрут не настроен' in page.locator('#retail-monitor').inner_text()
    page.locator('#open-thefts').click()
    page.locator(f'[data-event="{route}"]').click()
    assert page.locator('#modal video').get_attribute('src') == f'/api/events/{route}/media/clip'
    assert 'shelf_a' in page.locator('.theft-facts').inner_text()
    assert '2–8 с' in page.locator('.theft-interval').inner_text()
    assert 'Оценка модели' not in page.locator('#modal').inner_text()
    # Keep the fixture independent of NVR connectivity. An operator can
    # retain an inconclusive item and add a note even if its clip expired.
    page.locator('#review-note').fill('Нужен более длинный фрагмент')
    store.execute('UPDATE events SET note=? WHERE id=?', ('Уточнить у смены',route))
    page.locator('[data-review=new]').click()
    page.locator('#theft-reload').wait_for()
    assert page.locator('#review-note').input_value() == 'Нужен более длинный фрагмент'
    page.locator('#theft-reload').click()
    page.locator('#theft-reload').wait_for(state='hidden')
    assert page.locator('#review-note').input_value() == 'Уточнить у смены'
    page.locator('#review-note').fill('Покупатель вышел без товара')
    page.locator('[data-review=dismissed]').click()
    page.locator('#modal').wait_for(state='hidden')
    page.locator(f'[data-event="{route}"]').wait_for(state='hidden')
    page.locator('[data-filter=status]').select_option('dismissed')
    page.locator(f'[data-event="{route}"]').click()
    assert page.locator('#review-note').input_value() == 'Покупатель вышел без товара'
    assert 'Сигнал отклонён' in page.locator('.detail-meta').inner_text()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.querySelector("#modal-body").scrollWidth <= document.querySelector("#modal-body").clientWidth')
    page.locator('#modal-close').click()
    page.locator('[data-view=overview]').click()
    page.locator('#retail-monitor').wait_for()
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')


def main():
    with tempfile.TemporaryDirectory(prefix='glazza-alert-regressions-') as directory:
        root = Path(directory)
        env = {**os.environ,'PYTHONPATH':str(ROOT),'GLAZZA_DATA':str(root/'data'),
               'GLAZZA_PASSWORD':'regression-password-123','GLAZZA_WORKER_KEY':'regression-worker-key-'*3,
               'GLAZZA_DEMO':'false','FRIGATE_CONFIG':str(root/'config.yml')}
        with (root/'server.log').open('w') as logs:
            code = "from app.main import create_app; import uvicorn; uvicorn.run(create_app(background=False),host='127.0.0.1',port=18327,access_log=False)"
            server = subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,env=env,stdout=logs,stderr=logs)
            try:
                with httpx.Client(trust_env=False,base_url=BASE) as client:
                    for _ in range(100):
                        try:
                            if client.get('/healthz').status_code == 200: break
                        except httpx.HTTPError: pass
                        time.sleep(.1)
                    else: raise RuntimeError('Server failed to start')
                    session = client.post('/api/login',json={'username':'admin','password':env['GLAZZA_PASSWORD']}).json()
                    client.headers['X-CSRF-Token'] = session['csrf']
                    assert client.post('/api/cameras',json={'id':'door_camera','name':'Тестовый вход','stream':'rtsp://127.0.0.1/test'}).status_code == 201
                store = Store(root/'data')
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(args=['--no-sandbox'])
                    page = browser.new_page(viewport={'width':1440,'height':1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('https://**/*', lambda route: route.abort())
                    page.goto(BASE)
                    page.locator('input[name=password]').fill(env['GLAZZA_PASSWORD'])
                    page.get_by_role('button',name='Войти в Glazza').click()
                    page.get_by_role('heading',name='Всё под наблюдением.').wait_for()
                    page.evaluate('clearInterval(timer); clearInterval(imageTimer)')
                    page.locator('[data-view=analysis]').click()
                    page.locator('#video-file').wait_for()
                    refresh_page(page)
                    page.locator('#video-file').set_input_files({'name':'synthetic.mp4','mimeType':'video/mp4','buffer':b'0000ftyp'+b'x'*24})
                    page.locator('#video-file').focus()
                    page.evaluate('window.savedFileInput = document.querySelector("#video-file")')
                    event = store.event(source='test',source_id='fight-1',camera='door_camera',kind='fight',score=.9,started=time.time())
                    refresh_page(page)
                    assert page.locator('.fight-alert').count() == 1
                    assert page.evaluate('document.querySelector("#video-file") === savedFileInput && savedFileInput.files.length === 1')
                    assert page.evaluate('document.activeElement === savedFileInput')
                    refresh_page(page)
                    assert page.locator('.fight-alert').count() == 1
                    store.execute("UPDATE events SET status='dismissed' WHERE id=?",(event,))
                    refresh_page(page)
                    assert page.locator('.fight-alert').count() == 0
                    assert page.locator('#video-file').evaluate('(el)=>el.files.length') == 1
                    page.locator('[data-view=map]').click()
                    page.locator('#camera-map').wait_for()
                    page.evaluate('window.savedMap = cameraMap.map')
                    entry = str(uuid.uuid4())
                    store.execute('INSERT INTO admission_entries(id,event_id,source_id,camera,direction,at,state,reason) VALUES(?,?,?,?,?,?,?,?)',
                                  (entry,event,'test-entry','door_camera','entry',time.time(),'review','unknown_or_ambiguous'))
                    for _ in range(3):
                        refresh_page(page)
                        assert page.locator('.admission-alert').count() == 1
                        assert page.evaluate('cameraMap.map === savedMap')
                    # A modal prevents page replacement, but alarms must still reconcile.
                    page.evaluate('document.querySelector("#modal").showModal()')
                    store.execute("UPDATE admission_entries SET decision='dismissed',state='reviewed' WHERE id=?",(entry,))
                    event2 = store.event(source='test',source_id='fight-2',camera='door_camera',kind='fight',score=.9,started=time.time())
                    refresh_page(page)
                    assert page.locator('.admission-alert').count() == 0
                    assert page.locator('.fight-alert').count() == 1
                    assert page.locator('#modal').evaluate('(el)=>el.open')
                    page.evaluate('document.querySelector("#modal").close()')
                    for view in ('map','overview'):
                        page.evaluate('(view)=>navigate(view)',view)
                        refresh_page(page)
                        page.locator('#open-fights').focus()
                        page.evaluate('window.savedAlarmButton = document.querySelector("#open-fights")')
                        refresh_page(page)
                        assert page.evaluate('document.activeElement === savedAlarmButton && savedAlarmButton.isConnected')
                    page.locator('#open-fights').press('Enter')
                    assert page.evaluate("filters.kind === 'fight' && filters.status === 'new'")
                    page.locator(f'[data-event="{event2}"]').wait_for()
                    # Persisted human choice must be distinct from model candidate.
                    with httpx.Client(trust_env=False,base_url=BASE) as api_client:
                        token = api_client.post('/api/login',json={'username':'admin','password':env['GLAZZA_PASSWORD']}).json()
                        api_client.headers['X-CSRF-Token'] = token['csrf']
                        def person(name):
                            response=api_client.post('/api/admission/people',json={'name':name,'category':'staff','valid_from':time.time()-60,'valid_until':time.time()+3600,'basis':'Test fixture'})
                            assert response.status_code == 201
                            return response.json()['id']
                        candidate,verified = person('Candidate Person A'),person('Verified Person B')
                        store.execute('UPDATE admission_entries SET candidate_id=? WHERE id=?',(candidate,entry))
                        assert api_client.post(f'/api/admission/entries/{entry}/decision',json={'decision':'denied','person_id':verified,'note':'Saved operator comment'}).status_code == 200
                    page.evaluate("navigate('admission')")
                    refresh_page(page)
                    assert 'Проверенный человек: Verified Person B' in page.locator('#content').inner_text()
                    page.locator(f'[data-admission-entry="{entry}"]').click()
                    assert page.locator('#entry-form select[name=decision]').input_value() == 'denied'
                    assert page.locator('#entry-form select[name=person_id]').input_value() == verified
                    assert page.locator('#entry-form textarea[name=note]').input_value() == 'Saved operator comment'
                    assert 'Кандидат: Candidate Person A' in page.locator('#modal').inner_text()
                    assert 'Проверенный человек: Verified Person B' in page.locator('#modal').inner_text()
                    page.evaluate('document.querySelector("#modal").close()')
                    # Removing the focused alarm provides a stable focus destination.
                    page.evaluate("navigate('overview')")
                    refresh_page(page)
                    page.locator('#open-fights').focus()
                    store.execute("UPDATE events SET status='dismissed' WHERE id=?",(event2,))
                    refresh_page(page)
                    assert page.locator('.fight-alert').count() == 0
                    assert page.evaluate('document.activeElement.id') == 'breadcrumb'
                    check_retail_review(page, store)
                    assert not errors, errors
                    browser.close()
                print('PASS: fresh/resolved fight alarms with selected file and focus; map alarm deduplication/removal; modal preservation; alarm keyboard focus/removal/navigation; saved admission person/decision/comment; retail site scoping, shelf/checkout/exit setup and disabling, backlog queue, evidence and conflicting review; no JS errors')
            finally:
                server.terminate()
                try: server.wait(timeout=5)
                except subprocess.TimeoutExpired: server.kill(); server.wait()


if __name__ == '__main__':
    main()
