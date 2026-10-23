"""Real Chromium user flows. Run explicitly: python tests/browser_smoke.py."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    processes = []
    with tempfile.TemporaryDirectory(prefix='glazza-browser-') as directory:
        logs = open(Path(directory) / 'servers.log', 'w')
        try:
            for port, mode in [(18080, 'true'), (18081, 'false')]:
                env = {**os.environ, 'GLAZZA_PASSWORD': 'test-password-12345', 'GLAZZA_WORKER_KEY': 'browser-smoke-worker-key-1234567890123456',
                       'GLAZZA_DATA': str(Path(directory) / str(port)), 'GLAZZA_DEMO': mode,
                       'FRIGATE_CONFIG': str(Path(directory) / 'config.yml'), 'FRIGATE_URL': 'http://127.0.0.1:59999', 'ANALYZER_URL': 'http://127.0.0.1:59998'}
                processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:create_app', '--factory', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=ROOT, env=env, stdout=logs, stderr=logs))
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(f'http://127.0.0.1:{port}/healthz').status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Server did not start')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=['--no-sandbox'])
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def login(port):
                    page.goto(f'http://127.0.0.1:{port}')
                    page.locator('input[name=password]').fill('test-password-12345')
                    page.get_by_role('button',name='Войти в Glazza').click()
                    page.get_by_role('heading',name='Всё под наблюдением.').wait_for()
                login(18080)
                page.screenshot(path=str(ROOT/'docs/dashboard.png'),full_page=True)
                page.locator('[data-view=events]').click()
                page.locator('[data-event]').first.click()
                page.locator('#review-note').fill('Проверено в браузере')
                page.get_by_role('button',name='Отклонить',exact=True).click()
                page.locator('#modal').wait_for(state='hidden')
                page.locator('[data-filter=status]').select_option('dismissed')
                page.locator('tbody tr').first.wait_for()
                page.locator('[data-view=analysis]').click()
                assert page.locator('#video-file').is_disabled()
                page.locator('[data-view=system]').click()
                page.get_by_role('heading',name='Состояние системы').wait_for()
                page.set_viewport_size({'width':390,'height':844})
                page.locator('[data-view=overview]').click()
                page.locator('#toast').wait_for(state='hidden')
                page.screenshot(path=str(ROOT/'docs/mobile.png'),full_page=True)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Mobile overflow'
                page.get_by_role('button',name='Выйти из аккаунта',exact=True).click()
                page.locator('#login-form').wait_for()
                page.set_viewport_size({'width':1440,'height':1000})
                # New context separates demo and live sessions.
                page.close(); page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                login(18081)
                page.locator('[data-action=add-camera]').first.click()
                page.locator('#camera-form input[name=name]').fill('Тестовая камера')
                page.locator('#camera-form input[name=id]').fill('test_camera')
                page.locator('#camera-form input[name=stream]').fill('rtsp://demo:private@192.168.50.2/stream')
                page.get_by_role('button',name='Сохранить камеру').click()
                page.locator('[data-camera=test_camera]').wait_for()
                page.locator('[data-camera=test_camera]').click()
                canvas=page.locator('#zone-canvas')
                canvas.click(position={'x':40,'y':40});canvas.click(position={'x':200,'y':40});canvas.click(position={'x':200,'y':150})
                page.get_by_role('button',name='Добавить зону').click()
                page.get_by_role('button',name='Сохранить зоны').click()
                page.locator('#modal').wait_for(state='hidden')
                page.locator('[data-view=cameras]').click()
                page.get_by_role('button',name='Применить настройки',exact=True).click()
                page.get_by_role('button',name='Применить и перезапустить',exact=True).click()
                page.locator('#modal').wait_for(state='hidden')
                page.get_by_role('status').filter(has_text='Конфигурация сохранена').wait_for()
                assert not errors, errors
                print('PASS: login, navigation, review, filters, mobile layout, camera setup, polygon zones, offline apply. No page errors.')
                browser.close()
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill()
            logs.close()


if __name__=='__main__':
    main()
