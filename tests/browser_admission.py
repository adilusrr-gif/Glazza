"""Isolated Chromium flow for admission registry and human review."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import httpx
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='glazza-admission-browser-') as directory:
        root=Path(directory)
        env={**os.environ,'GLAZZA_DATA':str(root/'data'),'GLAZZA_PASSWORD':'browser-admission-password','GLAZZA_WORKER_KEY':'browser-admission-worker-key-'*2,'GLAZZA_DEMO':'false','FRIGATE_URL':'http://127.0.0.1:59999','ANALYZER_URL':'http://127.0.0.1:59998','FRIGATE_CONFIG':str(root/'config.yml')}
        with (root/'server.log').open('w') as logs:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:create_app','--factory','--port','18195','--no-access-log'],cwd=ROOT,env=env,stdout=logs,stderr=logs)
            try:
                with httpx.Client(base_url='http://127.0.0.1:18195',trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get('/healthz').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Server failed')
                    login=client.post('/api/login',json={'username':'admin','password':env['GLAZZA_PASSWORD']}).json()
                    client.headers['X-CSRF-Token']=login['csrf']
                    assert client.post('/api/cameras',json={'id':'door_camera','name':'Вход','stream':'rtsp://127.0.0.1/test','zones':[{'name':'door_zone','points':[[0,0],[1,0],[1,1]]}]}).status_code==201
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto('http://127.0.0.1:18195')
                    page.locator('input[name=password]').fill(env['GLAZZA_PASSWORD'])
                    page.get_by_role('button',name='Войти в Glazza').click()
                    page.locator('[data-view=admission]').click()
                    page.get_by_role('heading',name='Допуск и посетители').wait_for()
                    page.locator('#add-person').click()
                    page.locator('#person-form input[name=name]').fill('Тестовый гость')
                    page.locator('#person-form select[name=category]').select_option('guest')
                    page.locator('#person-form input[name=host]').fill('Принимающий сотрудник')
                    page.locator('#person-form input[name=basis]').fill('Тестовое разрешение')
                    page.get_by_role('button',name='Сохранить допуск').click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.get_by_text('Тестовый гость',exact=True).wait_for()
                    page.get_by_role('button',name='Включить контроль зоны').click()
                    page.locator('[data-entrance-toggle]').wait_for()
                    from app.store import Store
                    store=Store(root/'data')
                    event=store.event(source='frigate',source_id='browser-entry',camera='door_camera',kind='person',score=.9,started=time.time())
                    store.execute('INSERT INTO admission_entries(id,event_id,source_id,camera,direction,at,state,reason) VALUES(?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),event,'browser-entry','door_camera','entry',time.time(),'review','unknown_or_ambiguous'))
                    page.reload();page.locator('[data-view=admission]').click()
                    page.locator('[data-admission-entry]').first.click()
                    page.locator('#entry-form select[name=decision]').select_option('denied')
                    page.locator('#entry-form textarea[name=note]').fill('Документ не подтверждён')
                    page.get_by_role('button',name='Сохранить решение').click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.get_by_text('Отказ в допуске',exact=True).wait_for()
                    page.set_viewport_size({'width':390,'height':844})
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),'Mobile overflow'
                    assert not errors,errors
                    browser.close()
                    print('PASS: registry guest creation, entrance configuration, incident review, mobile width; no JS errors')
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill()


if __name__=='__main__':main()
