"""Login presentation and unchanged session flow, on an isolated local server."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18422'


def main():
    with tempfile.TemporaryDirectory(prefix='glazza-login-') as directory:
        root=Path(directory)
        env={**os.environ,'PYTHONPATH':str(ROOT),'GLAZZA_DATA':str(root/'data'),'GLAZZA_PASSWORD':'login-preview-password-123',
             'GLAZZA_WORKER_KEY':'login-preview-worker-key-'*3,'GLAZZA_DEMO':'false','FRIGATE_CONFIG':str(root/'config.yml')}
        code="from app.main import create_app; import uvicorn; uvicorn.run(create_app(background=False),host='127.0.0.1',port=18422,access_log=False)"
        with (root/'server.log').open('w') as logs:
            server=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,env=env,stdout=logs,stderr=logs)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(BASE+'/healthz').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Server did not start')
                with sync_playwright() as p:
                    browser=p.chromium.launch(args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1672,'height':940})
                    errors=[];requests=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda request:requests.append(request.url) if request.url.endswith('/api/login') else None)
                    response=page.goto(BASE)
                    assert "script-src 'self'" in response.headers['content-security-policy']
                    page.locator('#login-title').wait_for()
                    page.wait_for_timeout(1200)
                    assert page.locator('#login').is_visible()
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    assert page.locator('#login-submit').is_enabled()
                    screenshot_dir=os.getenv('GLAZZA_LOGIN_SCREENSHOTS')
                    if screenshot_dir:
                        Path(screenshot_dir).mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(Path(screenshot_dir)/'login-desktop.png'))
                    page.locator('#login-password').fill('not-the-password')
                    page.locator('#login-password-toggle').click()
                    assert page.locator('#login-password').get_attribute('type')=='text'
                    assert page.locator('#login-password-toggle').get_attribute('aria-pressed')=='true'
                    page.locator('#login-password-toggle').click()
                    assert page.locator('#login-password').get_attribute('type')=='password'
                    page.locator('[data-login-language=kk]').click()
                    assert page.locator('#login-title').inner_text()=='Қайта оралуыңызбен'
                    assert page.locator('#login-password').input_value()=='not-the-password'
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    # Two synchronous submit events still produce only one API request.
                    page.evaluate("document.querySelector('#login-form').requestSubmit(); document.querySelector('#login-form').requestSubmit()")
                    page.locator('#login-error').filter(has_text='Логин немесе құпиясөз қате').wait_for()
                    assert len(requests)==1,requests
                    assert page.locator('#login-submit').is_enabled()
                    assert page.locator('#login-password').get_attribute('aria-invalid')=='true'
                    page.locator('[data-login-language=ru]').click()
                    assert page.locator('#login-error').inner_text()=='Неверный логин или пароль'
                    page.reload()
                    assert page.locator('[data-login-language=ru]').get_attribute('aria-pressed')=='true'
                    assert not page.locator('#login-password').input_value()
                    for width,height in [(1024,768),(768,1024),(390,844),(320,568)]:
                        page.set_viewport_size({'width':width,'height':height})
                        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                        assert page.locator('#login-password').bounding_box()['width']>=200
                    page.set_viewport_size({'width':390,'height':844})
                    if screenshot_dir:page.screenshot(path=str(Path(screenshot_dir)/'login-mobile.png'),full_page=True)
                    page.emulate_media(reduced_motion='reduce')
                    assert page.locator('.login-art').evaluate('(el)=>getComputedStyle(el).animationName')=='none'
                    # Keyboard Enter performs the existing authenticated flow.
                    page.locator('#login-password').fill(env['GLAZZA_PASSWORD'])
                    page.locator('#login-password').press('Enter')
                    page.locator('#workspace').wait_for(state='visible')
                    assert page.locator('#login').is_hidden()
                    assert not page.locator('#login-password').input_value()
                    assert page.locator('#login-password').get_attribute('type')=='password'
                    assert page.context.request.get(BASE+'/api/session').status==200
                    assert page.locator('#content').evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(255, 255, 255)'
                    page.locator('#mobile-logout').click()
                    page.locator('#login').wait_for(state='visible')
                    assert page.context.request.get(BASE+'/api/session').status==401
                    assert not errors,errors
                    browser.close()
                    print('PASS: responsive login 320–1672px; password visibility; RU/KZ; error translation; single pending request; keyboard login; session/logout; unchanged workspace; reduced motion; no JS errors')
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()


if __name__=='__main__':main()
