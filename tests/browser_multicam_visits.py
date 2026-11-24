"""Chromium operator workflows against Glazza, using synthetic events and local NVR video."""
import json
import re
from pathlib import Path
import socket
import tempfile
import threading
import time

import httpx
from playwright.sync_api import expect, sync_playwright
import uvicorn

from app import visits
from app.config import Settings
from app.main import create_app


def check_multicam(page, store, site, events):
    page.locator('#site-selector').select_option(site)
    page.locator('[data-action=multicam-setup]').click()
    form = page.locator('#multicam-form')
    for role, cam in [('shelf', 'shelf_cam'), ('checkout', 'shelf_cam'), ('exit', 'exit_cam')]:
        form.locator(f'[name={role}_camera]').select_option(cam)
        form.locator(f'[name={role}_zone]').select_option('area')
    form.get_by_role('button', name='Включить связь камер').click()
    expect(page.locator('[data-multicam-error]')).to_contain_text('три разные камеры')
    form.locator('[name=checkout_camera]').select_option('checkout_cam')
    form.locator('[name=checkout_zone]').select_option('area')
    form.locator('[name=max_seconds]').fill('25')
    form.get_by_role('button', name='Включить связь камер').click()
    expect(page.get_by_text('Связь камер включена', exact=True)).to_be_visible()
    page.locator('#modal-close').click()
    page.locator('[data-action=multicam-setup]').click()
    expect(page.locator('#multicam-form [name=max_seconds]')).to_have_value('25')
    expect(page.locator('#multicam-form [name=checkout_camera]')).to_have_value('checkout_cam')
    page.locator('[data-multicam-disable]').click()
    expect(page.get_by_text('Связь камер выключена',exact=True)).to_be_visible()
    page.locator('#modal-close').click()
    page.locator('[data-action=multicam-history]').click()
    expect(page.locator('#clear-multicam-filter')).to_be_visible()
    expect(page.locator('tbody tr[data-event]')).to_have_count(3)
    neutral, alert, withdrawn, origins = events
    for event, roles in [(neutral,['shelf','checkout','exit']), (alert,['shelf','exit'])]:
        page.locator(f'[data-event="{event}"]').click()
        expect(page.locator('.multicam-clips button')).to_have_count(len(roles))
        for idx, role in enumerate(roles):
            page.locator(f'[data-clip-index="{idx}"]').click()
            video = page.locator('[data-clip-player] video')
            expect(video).to_have_attribute('src',f'/api/events/{origins[role]}/media/clip')
            expect(page.locator('[data-clip-seek]')).to_be_enabled()
            expect(video).to_have_js_property('currentTime',{'shelf':1,'checkout':2,'exit':3}[role])
            page.locator('[data-clip-seek]').click()
            timing = video.evaluate('(v) => ({t:v.currentTime, duration:v.duration, width:v.videoWidth})')
            assert timing['duration'] == 6 and timing['width'] == 160 and abs(timing['t'] - {'shelf':1,'checkout':2,'exit':3}[role]) < .1, timing
        if event == neutral:
            expect(page.locator('[data-review]')).to_have_count(0)
            expect(page.locator('#modal-title')).to_have_text('Маршрут через кассу')
            page.locator('#modal-close').click()
        else:
            page.locator('#review-note').fill('Записи просмотрены')
            page.locator('[data-review=dismissed]').click()
            expect(page.locator('#modal')).not_to_be_visible()
            assert store.rows('SELECT status,note FROM events WHERE id=?',(event,))[0] == {'status':'dismissed','note':'Записи просмотрены'}
    page.locator(f'[data-event="{withdrawn}"]').click()
    expect(page.locator('[data-review]')).to_have_count(0)
    expect(page.locator('#modal .warning-box')).to_contain_text('Связь отозвана')
    expect(page.locator('#modal textarea[readonly]')).to_have_value('Комментарий первой смены')
    page.locator('#modal-close').click()


def check_entrance_monitor(page, app, site):
    # The graph editor has its own browser suite. Exercise the actual ingest,
    # issued opaque ID and read APIs without manufacturing database visits.
    base = page.url.rstrip('/')
    session = page.request.get(base + '/api/session').json()
    config = {'entrance_camera':'shelf_cam','entrance_zone':'area','camera_ids':['shelf_cam','exit_cam'],
              'transitions':[{'from_camera':'shelf_cam','to_camera':'exit_cam','min_seconds':1,'max_seconds':40}]}
    response = page.request.put(base + '/api/retail/visits/' + site + '/config', data=config, headers={'X-CSRF-Token':session['csrf']})
    assert response.ok, response.text()
    now = time.time()
    event = {'id':'browser-entrance-person','camera':'shelf_cam','label':'person','start_time':now,
             'zones':['area'],'end_time':None,'data':{'frame_time':now,'path_data':[[[.15,.22],now]]}}
    app.state.engine.ingest([event])
    visits.record_health(app.state.store, 'shelf_cam', time.time(), True)
    data = page.request.get(base + '/api/retail/visits/'+site+'?active_only=true').json()
    assert len(data['visits']) == 1, data
    visit = data['visits'][0]
    assert visit['status'] == 'observed' and visit['active_camera'] == 'shelf_cam'
    page.locator('[data-view=overview]').click()
    page.locator('[data-nav=visits]').click()
    expect(page.locator('.visit-id')).to_have_text('ID ' + visit['id'])
    expect(page.locator('[data-visit-camera=shelf_cam]')).to_be_visible()
    expect(page.locator('.visit-path circle')).to_have_attribute('cx','96')
    expect(page.locator('.visit-path circle')).to_have_attribute('cy','79.2')
    # Without a confirmed new observation, no one should be displayed on
    # the next camera just because it appears in the configured graph.
    app.state.engine.ingest([{**event,'end_time':time.time(),'has_clip':True}])
    expect(page.locator('#visit-detail .pill')).to_have_text('Между камерами',timeout=8000)
    expect(page.locator('[data-visit-camera]')).to_have_count(0)
    expect(page.locator('.visit-timeline [data-visit-clip]')).to_have_count(1)
    page.locator('[data-view=overview]').click()


def check_visits(page, site):
    # Model the time-varying read payloads; API scoping and tracking are
    # covered separately by the store/engine tests. Writes above use the real API.
    now = time.time()
    phases = {'status':'observed','failed':False,'polls':0}
    def serve(route):
        phases['polls'] += 1
        if phases['failed']:
            return route.fulfill(status=503,json={'detail':'Связь с сервером потеряна'})
        status = phases['status']
        visit = {'id':'V-TEST-101','site_id':site,'status':status,'started_at':now-18,'last_seen_at':now,
                 'last_camera':'exit_cam','active_camera':'exit_cam' if status == 'observed' else None,'observations':2}
        if route.request.url.split('?')[0].endswith('/V-TEST-101'):
            return route.fulfill(json={**visit,'timeline':[
                {'origin_event':'event-in','role':'entrance','camera':'shelf_cam','zone':'area','started_at':now-18,'last_seen_at':now-16,'ended_at':now-16,'has_clip':True,'path':[[[.1,.2],now-18],[[.3,.4],now-16]]},
                {'origin_event':'event-middle','role':'observation','camera':'exit_cam','zone':'','started_at':now-6,'last_seen_at':now,'ended_at':None,'has_clip':False,'path':[[[.3,.4],now-6],[[.7,.8],now]]}
            ]})
        visible = status not in ('exited','expired') or 'active_only=false' in route.request.url
        return route.fulfill(json={'visits':[visit] if visible else [],'configured':True,'state':'active','checked_at':now,'poll_seconds':1})
    page.route('**/api/retail/visits/'+site+'**', serve)
    page.locator('[data-view=overview]').click()
    page.locator('[data-nav=visits]').click()
    expect(page.locator('.visit-id')).to_have_text('ID V-TEST-101')
    expect(page.locator('#visit-detail')).to_contain_text('Текущая камера')
    expect(page.locator('.visit-path figcaption')).to_contain_text('Выход')
    page.locator('[data-visit-path=event-in]').click()
    expect(page.locator('.visit-path figcaption')).to_contain_text('Полка')
    expect(page.locator('.visit-path polyline')).to_have_attribute('points','64.0,72.0 192.0,144.0')
    page.locator('[data-visit-clip=event-in]').click()
    expect(page.locator('#modal video')).to_have_attribute('src','/api/events/event-in/media/clip')
    page.locator('#modal-close').click()
    phases['status'] = 'uncertain'
    expect(page.locator('#visit-detail .pill')).to_have_text('Связь неясна')
    expect(page.locator('[data-visit-camera]')).to_have_count(0)
    phases['status'] = 'exited'
    expect(page.locator('#visit-list')).to_contain_text('Активных визитов пока нет')
    page.locator('[data-visit-filter=all]').click()
    expect(page.locator('#visit-detail .pill')).to_have_text('Вышел')
    phases['failed'] = True
    expect(page.locator('#visit-updated')).to_have_text('Нет актуальных данных')
    expect(page.locator('[data-visit-id]')).to_have_count(0)
    phases['failed'] = False
    expect(page.locator('#visit-detail .pill')).to_have_text('Вышел')
    page.set_viewport_size({'width':390,'height':844})
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.locator('#site-selector').select_option('default')
    expect(page.locator('#visit-monitor')).to_have_count(0)
    expect(page.locator('#content')).not_to_contain_text('V-TEST-101')
    polls = phases['polls']
    page.wait_for_timeout(1250)
    assert phases['polls'] == polls


def main():
    with tempfile.TemporaryDirectory(prefix='glazza-observation-ui-') as directory:
        root = Path(directory)
        cfg = Settings(data=root/'data',frigate_config=root/'config',frigate_url='http://nvr.test',analyzer_url='http://analyzer.test',
                       worker_key='chromium-test-worker-key-'*3,username='admin',password='browser-test-password-123',demo=False,secure_cookie=False)
        app = create_app(cfg,background=False)
        store = app.state.store
        site = 'test-shop'
        store.execute('INSERT INTO sites VALUES(?,?,?,?,?)',(site,'default','Тестовый магазин','retail','["person","theft"]'))
        zone = [{'name':'area','restricted':False,'points':[[.1,.1],[.8,.1],[.8,.8],[.1,.8]]}]
        roles = {'shelf':'Полка','checkout':'Касса','exit':'Выход'}
        now = time.time()
        origins = {}
        for role,name in roles.items():
            cam = role + '_cam'
            store.execute('INSERT INTO cameras(id,name,stream,site_id,zones) VALUES(?,?,?,?,?)',(cam,name,'rtsp://127.0.0.1/test',site,json.dumps(zone)))
            origins[role] = store.event(source='frigate',source_id=role,camera=cam,kind='person',score=.9,started=now-45,ended=now-39,site_id=site)
        evidence = [{'role':role,'camera':role+'_cam','zone':'area','origin_event':origins[role],
                     'start_seconds':i+1,'end_seconds':i+2} for i,role in enumerate(roles)]
        def seed_event(kind,suffix,items,**extra):
            details={'signal':'multicam_via_checkout' if kind=='retail_journey' else 'multicam_shelf_to_exit',
                     'checkout_camera':'checkout_cam','checkout_zone':'area','transition_seconds':12,'evidence':items,**extra}
            return store.event(source='retail_multicam',source_id=suffix,camera='exit_cam',kind=kind,score=None,
                               started=now-35,ended=now-23,site_id=site,details=details)
        neutral = seed_event('retail_journey','via',evidence)
        alert = seed_event('theft','possible',[evidence[0],evidence[2]])
        withdrawn = seed_event('theft','withdrawn',evidence,association_state='withdrawn',withdrawal_reason='Новые наблюдения')
        store.execute('UPDATE events SET status=?,note=? WHERE id=?',('dismissed','Комментарий первой смены',withdrawn))
        store.execute('UPDATE events SET status=? WHERE id=?',('confirmed',neutral))
        # Finite six-second synthetic blue frame, VP9/MP4, with duration and cue metadata.
        clip = (Path(__file__).parent/'fixtures'/'retail-evidence.mp4').read_bytes()
        def nvr(request):
            if request.url.path in {f'/api/events/{role}/clip.mp4' for role in roles}:
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', request.headers.get('range',''))
                headers = {'Content-Type':'video/mp4','Accept-Ranges':'bytes'}
                first = int(match[1]) if match else 0
                last = min(int(match[2]),len(clip)-1) if match and match[2] else len(clip)-1
                if match: headers['Content-Range'] = f'bytes {first}-{last}/{len(clip)}'
                data = clip[first:last+1]
                headers['Content-Length'] = str(len(data))
                return httpx.Response(206 if match else 200,headers=headers,stream=httpx.ByteStream(data))
            return httpx.Response(404)
        app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(nvr))
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread = threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get('/healthz').status_code == 200: break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                else:
                    raise RuntimeError('Test server did not start')
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=['--no-sandbox'])
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors = []
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{port}')
                page.locator('input[name=password]').fill(cfg.password)
                page.get_by_role('button',name='Войти в Glazza').click()
                expect(page.get_by_role('heading',name='Всё под наблюдением.')).to_be_visible()
                check_multicam(page,store,site,(neutral,alert,withdrawn,origins))
                check_entrance_monitor(page,app,site)
                check_visits(page,site)
                assert not errors, errors
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            listener.close()
    print('PASS: multicamera setup, scoped history, individual playable clips, withdrawal and human review; real entrance ID and current camera, visit timeline, trajectory, status changes, polling and site isolation.')


if __name__ == '__main__':
    main()
