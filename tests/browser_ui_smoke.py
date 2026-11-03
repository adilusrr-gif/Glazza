"""Opt-in UI check against an isolated live app at localhost:18082."""
import json
import os
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv('GLAZZA_UI_TEST_URL', 'http://127.0.0.1:18082')
PASSWORD = os.getenv('GLAZZA_UI_TEST_PASSWORD', 'map-check-password-123')
camera_id = 'ui_test_' + uuid.uuid4().hex[:12]

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'])
    context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1000}, permissions=['camera'])
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE, wait_until='domcontentloaded')
    login_help = page.get_by_role('button', name='Помощь со входом')
    assert 'credentials.txt' not in page.locator('body').inner_text()
    login_help.hover()
    page.get_by_role('tooltip').wait_for(state='visible')
    assert 'credentials.txt' in page.get_by_role('tooltip').inner_text()
    page.locator('input[name=password]').fill(PASSWORD)
    page.get_by_role('button', name='Войти в Glazza').click()
    page.get_by_role('heading', name='Всё под наблюдением.').wait_for()
    csrf = context.request.get(BASE + '/api/session').json()['csrf']
    headers = {'X-CSRF-Token': csrf, 'Origin': BASE}
    created = False
    try:
        # Hover, keyboard focus, Escape, hoverable tooltip and poll stability.
        hint = page.locator('#content .page-head .help-trigger')
        assert 'Важные события вашего объекта' not in page.locator('#content').inner_text()
        hint.hover()
        assert page.get_by_role('tooltip').is_visible()
        page.get_by_role('tooltip').hover()
        page.wait_for_timeout(180)
        assert page.get_by_role('tooltip').is_visible()
        hint.focus()
        page.evaluate('refresh()')
        assert page.get_by_role('tooltip').is_visible()
        page.keyboard.press('Escape')
        assert not page.get_by_role('tooltip').is_visible()
        assert not hint.get_attribute('aria-describedby')
        page.locator('#content h1').click()
        page.screenshot(path=str(ROOT / 'data/ui-overview.png'), full_page=True)

        page.locator('.head-actions [data-action=add-camera]').click()
        assert 'Поддерживаются RTSP' not in page.locator('#modal').inner_text()
        page.locator('#modal-help .help-trigger').click()
        assert 'Поддерживаются RTSP' in page.get_by_role('tooltip').inner_text()
        assert page.get_by_role('tooltip').evaluate('(el) => el.closest("dialog") !== null')
        # Tooltip stays inside the viewport, above the dialog, and does not submit.
        assert page.get_by_role('tooltip').evaluate('(el) => { const r=el.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight; }')
        assert not context.request.get(BASE + '/api/cameras').json()
        page.keyboard.press('Escape')
        assert page.locator('#modal').evaluate('(el) => el.open')
        page.locator('#camera-form input[name=name]').fill('Проверка интерфейса')
        page.locator('#camera-form input[name=id]').fill(camera_id)
        page.locator('#camera-form input[name=stream]').fill('rtsp://127.0.0.1:9/test')
        page.get_by_role('button', name='Сохранить камеру', exact=True).click()
        page.locator('#modal').wait_for(state='hidden')
        created = True

        for view in ['overview', 'cameras', 'map', 'events', 'analysis', 'system']:
            page.locator('[data-view=' + view + ']').click()
            page.locator('#content h1').wait_for()
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), view
            assert page.locator('#content .page-head > div > p').count() == 0
            assert page.locator('#content .model-note, #content .insight, #content .stat-bottom').count() == 0
            page.screenshot(path=str(ROOT / ('data/ui-' + view + '.png')), full_page=True)

        page.locator('[data-view=analysis]').click()
        assert 'MP4, MOV' not in page.locator('#content').inner_text()
        page.get_by_role('button', name='Форматы и ограничения видео').hover()
        assert 'MP4, MOV' in page.get_by_role('tooltip').inner_text()
        # Choosing a file survives polling and viewing help.
        page.locator('#video-file').set_input_files({'name': 'ui-check.mp4', 'mimeType': 'video/mp4', 'buffer': b'preview-only'})
        page.evaluate('refresh()')
        assert page.locator('#video-file').evaluate('(el)=>el.files.length') == 1
        page.locator('#video-file').set_input_files([])
        page.get_by_role('button', name='Открыть веб-камеру', exact=True).click()
        page.locator('#webcam-start:not([disabled])').wait_for()
        assert page.locator('#webcam-status').inner_text() == 'Камера готова'
        page.locator('#modal-help .help-trigger').hover()
        assert 'Микрофон не используется' in page.get_by_role('tooltip').inner_text()
        page.locator('#modal-close').click()
        for _ in range(20):
            if page.evaluate('webcam === null'): break
            page.wait_for_timeout(50)
        assert page.evaluate('webcam === null')

        # Synthetic browser-only result verifies nested templates without creating jobs.
        page.evaluate('''() => {
          state.jobs = [{id:'ui-result', camera:'upload', created:Date.now()/1000, state:'succeeded', has_file:false,
            result:{model:'Test', duration_seconds:5, detections:[], fight:{windows_analyzed:4,threshold:.5,peak_score:.1}, face:{frames_with_faces:3,peak_blendshapes:{jawOpen:.2}}}}];
          renderAnalysis(); bindActions();
        }''')
        page.locator('[data-job=ui-result]').click()
        assert '${help' not in page.locator('#modal').inner_text()
        assert 'Сигналы не обнаружены' in page.locator('.success-box').inner_text()
        assert 'Максимальные значения' not in page.locator('#modal').inner_text()
        page.get_by_role('button', name='Как читать показатели мимики').hover()
        assert 'Максимальные значения' in page.get_by_role('tooltip').inner_text()
        page.locator('#modal-close').click()

        # Narrow layouts, including forms and long model states.
        for width in [390, 320]:
            page.set_viewport_size({'width': width, 'height': 844})
            for view in ['overview', 'cameras', 'map', 'events', 'analysis', 'system']:
                page.locator('[data-view=' + view + ']').click()
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), (width, view)
                page.locator('#content .page-head .help-trigger').click()
                assert page.get_by_role('tooltip').evaluate('(el) => { const r=el.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight; }'), (width, view)
                page.keyboard.press('Escape')
                page.evaluate('scrollTo(0,0)')
                if view == 'analysis': page.screenshot(path=str(ROOT / f'data/ui-mobile-{width}.png'), full_page=True)
            page.locator('[data-view=cameras]').click()
            page.locator('.head-actions [data-action=add-camera]').click()
            page.locator('#modal-help .help-trigger').click()
            assert page.get_by_role('tooltip').evaluate('(el) => el.getBoundingClientRect().right<=innerWidth')
            page.locator('#modal-close').click()
        touch_context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        touch_page = touch_context.new_page()
        touch_page.goto(BASE, wait_until='domcontentloaded')
        touch_help = touch_page.get_by_role('button', name='Помощь со входом')
        touch_help.tap()
        touch_page.wait_for_timeout(300)
        assert touch_page.get_by_role('tooltip').is_visible()
        touch_help.tap()
        assert not touch_page.get_by_role('tooltip').is_visible()
        touch_help.tap()
        touch_page.locator('input[name=username]').tap()
        assert not touch_page.get_by_role('tooltip').is_visible()
        touch_context.close()
        assert not errors, errors
        print(json.dumps({'result':'PASS','checks':['hidden explanations','hover/focus/tap help','Escape and dialog layering','camera creation','all six views at 1440/390/320','file input and webcam','analysis result rendering','no JavaScript errors']}, ensure_ascii=False))
    finally:
        if created:
            response = context.request.delete(BASE + '/api/cameras/' + camera_id, headers=headers)
            assert response.status == 200, response.text()
        browser.close()
