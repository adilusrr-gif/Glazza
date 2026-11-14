"""Browser check of the restaurant workflow against the real API and an isolated DB.

Camera frames and occupancy observations are synthetic fixtures. This does not
measure camera/model accuracy or send any external notification.
"""
from io import BytesIO
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import httpx
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from app.config import Settings
from app.main import create_app
from app import restaurant as restaurant_module


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-only',action='store_true',help='Run the local synthetic fixture for manual browser QA')
    parser.add_argument('--port',type=int,default=18197)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='glazza-restaurant-browser-') as directory:
        root = Path(directory)
        settings = Settings(data=root/'data', frigate_config=root/'config.yml',
                            frigate_url='http://nvr.test', analyzer_url='http://analyzer.test',
                            username='admin', password='restaurant-browser-password',
                            worker_key='restaurant-browser-worker-'*3, demo=False, secure_cookie=False)
        app = create_app(settings, background=False)
        service = app.state.engine.restaurant
        clock = [time.time()]
        real_at = restaurant_module._at
        restaurant_module._at = lambda now=None: float(clock[0] if now is None else now)
        frame = Image.new('RGB',(960,540),'#dee6ed')
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((80,100,400,400),radius=18,fill='#aa8a66')
        draw.rounded_rectangle((590,110,880,380),radius=18,fill='#aa8a66')
        draw.text((25,24),'SYNTHETIC CAMERA FIXTURE',fill='#21344c')
        buffer=BytesIO();frame.save(buffer,format='JPEG')
        def upstream(request):
            if request.url.path.endswith('/latest.jpg'):
                return httpx.Response(200,content=buffer.getvalue(),headers={'Content-Type':'image/jpeg'})
            if request.url.path == '/api/restart':
                return httpx.Response(200,json={'ok':True})
            return httpx.Response(404)
        app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        listener = socket.socket();listener.bind(('127.0.0.1',args.port if args.serve_only else 0))
        port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='warning'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        url=f'http://127.0.0.1:{port}'
        try:
            with httpx.Client(base_url=url,trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get('/healthz').status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise AssertionError('Fixture server did not start')
                login=client.post('/api/login',json={'username':'admin','password':settings.password});login.raise_for_status()
                client.headers['X-CSRF-Token']=login.json()['csrf']
                site=client.post('/api/platform/sites',json={'organization_id':'default','name':'Ресторан тест','profile':'restaurant'});site.raise_for_status();site_id=site.json()['id']
                response=client.post('/api/cameras',json={'site_id':site_id,'id':'dining','name':'Зал ресторана','stream':'rtsp://127.0.0.1:9/test','zones':[]});response.raise_for_status()
                manager=client.post('/api/restaurant/staff',json={'site_id':site_id,'name':'Менеджер смены','role':'manager'});manager.raise_for_status();manager=manager.json()
                other=client.post('/api/restaurant/staff',json={'site_id':site_id,'name':'Другой официант','role':'waiter'});other.raise_for_status();other=other.json()
                second=client.post('/api/restaurant/tables',json={'site_id':site_id,'camera':'dining','name':'Стол 99','points':[[.62,.15],[.95,.15],[.95,.8],[.62,.8]],'waiter_id':other['id'],'arrival_seconds':1,'vacancy_seconds':10});second.raise_for_status()
                def observe(count,advance=0,available=True):
                    clock[0]+=advance
                    tables=service.configuration_tables()
                    counts={table['zone']:count if table['name']=='Стол 7' else 0 for table in tables}
                    service.observe('dining',counts,available=available,now=clock[0]);service.tick(now=clock[0])
                    service.set_health({'state':'online' if available else 'offline','detail':'Тестовые наблюдения'})
                def table_state():
                    return next(table for table in service.snapshot()['tables'] if table['name']=='Стол 7')
                if args.serve_only:
                    waiter=client.post('/api/restaurant/staff',json={'site_id':site_id,'name':'Алия','role':'waiter'});waiter.raise_for_status();waiter=waiter.json()
                    for name,points in [('Стол 7',[[.08,.18],[.43,.18],[.43,.84],[.08,.84]]),('Стол 8',[[.48,.18],[.59,.18],[.59,.82],[.48,.82]]),('Стол 9',[[.08,.02],[.45,.02],[.45,.12],[.08,.12]])]:
                        created=client.post('/api/restaurant/tables',json={'site_id':site_id,'camera':'dining','name':name,'points':points,'waiter_id':waiter['id'],'arrival_seconds':1,'vacancy_seconds':10});created.raise_for_status()
                    observe(0)
                    initial=next(table for table in service.snapshot()['tables'] if table['name']=='Стол 9')
                    cleaned=client.post('/api/restaurant/tables/'+initial['id']+'/actions',json={'action':'cleaned','visit_id':None,'revision':initial['revision']});cleaned.raise_for_status()
                    observe(1);observe(1,2);observe(1,121)
                    response=client.post('/api/cameras',json={'site_id':site_id,'id':'unavailable','name':'Недоступная камера · тест','stream':'rtsp://127.0.0.1:9/missing','zones':[]});response.raise_for_status()
                    response=client.post('/api/restaurant/tables',json={'site_id':site_id,'camera':'unavailable','name':'Стол 10','points':[[.1,.1],[.8,.1],[.8,.8],[.1,.8]],'waiter_id':waiter['id']});response.raise_for_status()
                    @app.post('/__fixture/observation')
                    def fixture_observation(body: dict):
                        observe(int(body.get('people',1)),float(body.get('advance',0)),bool(body.get('available',True)))
                        return service.snapshot(site_id=site_id)
                    print(json.dumps({'fixture':url,'username':'admin','password':settings.password,'site_id':site_id,'waiter_url':url+waiter['portal_url'],'manager_url':url+manager['portal_url'],'control':'POST /__fixture/observation {people,advance,available}; synthetic observations only'},ensure_ascii=False),flush=True)
                    try:
                        while thread.is_alive():time.sleep(.5)
                    except KeyboardInterrupt:
                        pass
                    return
                with sync_playwright() as pw:
                    launch={'args':['--no-sandbox']}
                    executable=os.getenv('GLAZZA_CHROMIUM_EXECUTABLE')
                    if executable:launch['executable_path']=executable
                    browser=pw.chromium.launch(**launch)
                    admin=browser.new_page(viewport={'width':1440,'height':1000})
                    errors=[];admin.on('pageerror',lambda error:errors.append(str(error)))
                    admin.goto(url)
                    admin.locator('input[name=password]').fill(settings.password)
                    admin.get_by_role('button',name='Войти в Glazza').click()
                    admin.locator('#site-selector').select_option(site_id)
                    admin.locator('[data-view=restaurant]').click()
                    admin.locator('#restaurant-staff').click()
                    admin.locator('#restaurant-add-staff').click()
                    admin.locator('#restaurant-staff-form input[name=name]').fill('Алия')
                    admin.get_by_role('button',name='Сохранить сотрудника',exact=True).click()
                    link=admin.locator('#restaurant-staff-url');expect(link).to_be_visible()
                    waiter_url=link.input_value()
                    assert waiter_url.startswith(url+'/staff#token=')
                    admin.locator('#modal-close').click()
                    admin.locator('#restaurant-add-table').click()
                    admin.locator('#restaurant-table-form input[name=name]').fill('Стол 7')
                    admin.locator('#restaurant-table-form select[name=waiter_id]').select_option(label='Алия')
                    admin.locator('#restaurant-table-form input[name=arrival_seconds]').fill('1')
                    admin.locator('#restaurant-table-form input[name=vacancy_seconds]').fill('10')
                    canvas=admin.locator('#restaurant-zone')
                    box=canvas.bounding_box();assert box
                    for x,y in ((.08,.15),(.48,.15),(.48,.88),(.08,.88)):
                        canvas.click(position={'x':box['width']*x,'y':box['height']*y})
                    # A scheduled refresh must not erase an in-progress polygon/form.
                    admin.wait_for_timeout(2200)
                    expect(admin.locator('#restaurant-point-count')).to_have_text('4 / 20 точек')
                    expect(admin.locator('#restaurant-table-form input[name=name]')).to_have_value('Стол 7')
                    admin.get_by_role('button',name='Сохранить стол',exact=True).click()
                    expect(admin.locator('#modal')).not_to_be_visible()
                    table_id=table_state()['id']
                    card=admin.locator(f'[data-table-card="{table_id}"]')
                    expect(card).to_have_class('restaurant-table restaurant-gray')
                    observe(0)
                    expect(card.locator('[data-restaurant-action=cleaned]')).to_be_visible()
                    card.locator('[data-restaurant-action=cleaned]').click()
                    expect(card).to_have_class('restaurant-table restaurant-green')
                    # Staff token is in a fragment, then removed; no admin cookie is shared.
                    waiter_context=browser.new_context(viewport={'width':390,'height':844})
                    waiter=waiter_context.new_page();waiter.on('pageerror',lambda error:errors.append(str(error)))
                    waiter.goto(waiter_url)
                    expect(waiter.locator('#staff-name')).to_have_text('Алия')
                    assert waiter.url==url+'/staff'
                    expect(waiter.get_by_role('heading',name='Стол 99',exact=True)).to_have_count(0)
                    expect(waiter.locator('[data-table-card]')).to_have_count(1)
                    observe(1);observe(1,2)
                    waiter_card=waiter.locator(f'[data-table-card="{table_id}"]')
                    expect(waiter_card).to_have_class('restaurant-table restaurant-red')
                    waiter.locator('[data-restaurant-ack]').first.click()
                    assert table_state()['state']=='waiting', 'Acknowledgement must not confirm greeting'
                    observe(1,121)
                    manager_context=browser.new_context(viewport={'width':1440,'height':1000})
                    manager_page=manager_context.new_page();manager_page.on('pageerror',lambda error:errors.append(str(error)))
                    manager_page.goto(url+manager['portal_url'])
                    expect(manager_page.locator('.restaurant-alert-urgent')).to_be_visible()
                    expect(waiter_card.locator('[data-restaurant-action=greeted]')).to_be_enabled()
                    waiter_card.locator('[data-restaurant-action=greeted]').click()
                    expect(waiter_card.locator('.restaurant-state')).to_contain_text('Обслуживаются')
                    assert table_state()['state']=='served'
                    waiter_card.locator('[data-restaurant-action=hold]').click()
                    expect(waiter_card.locator('[data-restaurant-action=resume]')).to_be_visible()
                    observe(0);observe(0,61)
                    assert table_state()['state']=='served', 'A held visit must survive temporary absence'
                    waiter_card.locator('[data-restaurant-action=resume]').click()
                    expect(waiter_card.locator('[data-restaurant-action=hold]')).to_be_visible()
                    observe(0);observe(0,11)
                    expect(waiter_card).to_have_class('restaurant-table restaurant-yellow')
                    screenshots=ROOT/'test-results';screenshots.mkdir(exist_ok=True)
                    waiter.screenshot(path=str(screenshots/'restaurant-staff-mobile.png'),full_page=True)
                    admin.locator('#restaurant-camera').focus();admin.locator('#restaurant-camera').blur()
                    expect(card).to_have_class('restaurant-table restaurant-yellow')
                    admin.screenshot(path=str(screenshots/'restaurant-admin-desktop.png'),full_page=True)
                    waiter_card.locator('[data-restaurant-action=cleaned]').click()
                    expect(waiter_card).to_have_class('restaurant-table restaurant-green')
                    observe(0,available=False)
                    expect(waiter_card).to_have_class('restaurant-table restaurant-gray')
                    for page in (waiter,admin):
                        page.set_viewport_size({'width':390,'height':844})
                        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), 'Mobile horizontal overflow'
                    admin.screenshot(path=str(screenshots/'restaurant-admin-mobile.png'),full_page=True)
                    assert not errors, errors
                    browser.close()
                    print('PASS: staff setup/link, polygon survives refresh, scoped mobile portal, green/red/yellow/gray, ack distinct from greeting, escalation, hold/resume, cleanup, desktop/mobile layout')
        finally:
            restaurant_module._at=real_at
            server.should_exit=True;thread.join(timeout=8);listener.close()


if __name__=='__main__':main()
