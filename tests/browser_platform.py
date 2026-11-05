"""Real browser multi-site setup and filtered camera flow on an isolated database."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='glazza-platform-') as directory:
        env={**os.environ,'GLAZZA_DATA':directory+'/data','GLAZZA_PASSWORD':'platform-test-password','GLAZZA_WORKER_KEY':'platform-worker-key-'*3,'GLAZZA_DEMO':'false','FRIGATE_CONFIG':directory+'/config.yml','FRIGATE_URL':'http://127.0.0.1:59999','ANALYZER_URL':'http://127.0.0.1:59998'}
        with open(directory+'/server.log','w') as logs:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:create_app','--factory','--port','18196','--no-access-log'],cwd=ROOT,env=env,stdout=logs,stderr=logs)
            try:
                with httpx.Client(trust_env=False) as c:
                    for _ in range(100):
                        try:
                            if c.get('http://127.0.0.1:18196/healthz').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Server failed')
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(args=['--no-sandbox']);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto('http://127.0.0.1:18196')
                    page.locator('input[name=password]').fill(env['GLAZZA_PASSWORD'])
                    page.get_by_role('button',name='Войти в Glazza').click()
                    page.locator('[data-view=platform]').click()
                    page.locator('#new-organization').click()
                    page.locator('#organization-form input[name=name]').fill('Сеть магазинов')
                    page.get_by_role('button',name='Создать организацию',exact=True).click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.locator('#new-site').click()
                    page.locator('#site-form input[name=name]').fill('Супермаркет Алматы')
                    page.locator('#site-form select[name=organization_id]').select_option(label='Сеть магазинов')
                    page.locator('#site-form select[name=profile]').select_option('retail')
                    assert page.locator('#site-modules input[value=queue]').is_disabled()
                    assert not page.locator('#site-modules input[value=admission]').is_checked()
                    page.get_by_role('button',name='Сохранить объект').click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.get_by_role('heading',name='Супермаркет Алматы',exact=True).wait_for()
                    page.locator('#site-selector').select_option(label='Супермаркет Алматы · Магазин / супермаркет')
                    page.locator('[data-view=cameras]').click()
                    page.locator('[data-action=add-camera]').first.click()
                    page.locator('#camera-form input[name=name]').fill('Вход магазина')
                    page.locator('#camera-form input[name=id]').fill('retail_door')
                    page.locator('#camera-form input[name=stream]').fill('rtsp://127.0.0.1/test')
                    page.get_by_role('button',name='Сохранить камеру').click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.locator('[data-camera=retail_door]').wait_for()
                    page.locator('#site-selector').select_option('default')
                    page.locator('[data-camera=retail_door]').wait_for(state='hidden')
                    page.locator('[data-view=platform]').click()
                    page.locator('#new-site').wait_for()
                    page.set_viewport_size({'width':390,'height':844})
                    if page.evaluate('document.documentElement.scrollWidth>innerWidth'):
                        print(page.evaluate("[...document.querySelectorAll('#content *, header *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>({tag:e.tagName,cls:e.className,text:e.textContent.slice(0,80),width:e.getBoundingClientRect().width})).slice(0,15)"))
                        page.screenshot(path='/tmp/glazza-platform-mobile.png',full_page=True)
                        raise AssertionError('Mobile overflow')
                    assert not errors,errors
                    print('PASS: organization, retail site, planned modules disabled, scoped camera creation/filter, mobile layout')
                    browser.close()
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill()


if __name__=='__main__':main()
