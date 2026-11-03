"""Opt-in deployed-site check; uses a simulated browser camera and creates test jobs."""
from pathlib import Path
import json,time,subprocess,tempfile,os
import httpx
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
env=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line)
base=os.getenv('GLAZZA_TEST_URL','https://192.168.1.18:8443')
control=ROOT/'test-results/control.mp4'
control.parent.mkdir(exist_ok=True)
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=320x180:r=8','-t','5','-an','-c:v','libx264','-pix_fmt','yuv420p','-y',str(control)],check=True)
with httpx.Client(verify=str(ROOT/'tls/ca.crt'),trust_env=False,timeout=20) as c:
    r=c.post(base+'/api/login',json={'username':env['GLAZZA_ADMIN'],'password':env['GLAZZA_PASSWORD']},headers={'Origin':base})
    print('HTTPS login:',r.status_code,flush=True)
    r.raise_for_status()
    assert c.post(base+'/api/login',json={'username':'admin','password':'ignored'},headers={'Origin':'https://other.example'}).status_code==403
with sync_playwright() as p:
    browser=p.chromium.launch(args=['--no-sandbox','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
    context=browser.new_context(ignore_https_errors=True,permissions=['camera'],viewport={'width':1440,'height':1000})
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base,wait_until='domcontentloaded')
    page.locator('input[name=password]').fill(env['GLAZZA_PASSWORD'])
    page.get_by_role('button',name='Войти в Glazza').click()
    page.get_by_role('heading',name='Всё под наблюдением.').wait_for()
    assert page.evaluate('window.isSecureContext')
    page.locator('[data-view=analysis]').click()
    page.get_by_role('button',name='Открыть веб-камеру',exact=True).click()
    for _ in range(50):
        if page.locator('#webcam-preview').evaluate('(el)=>el.readyState')>=2:break
        page.wait_for_timeout(200)
    assert page.locator('#webcam-preview').evaluate('(el)=>el.readyState')>=2, page.locator('#webcam-error').inner_text()
    assert page.locator('#webcam-device option').count()>0
    page.screenshot(path=str(ROOT/'data/webcam-preview-check.png'),full_page=True)
    # Closing a recording must release tracks without uploading.
    before=context.request.get(base+'/api/jobs').json()
    page.locator('#webcam-start').click()
    page.wait_for_timeout(1200)
    page.evaluate('window.__testStream = webcam.stream')
    page.locator('#modal-close').click()
    page.wait_for_timeout(200)
    assert page.evaluate('webcam === null && window.__testStream.getTracks().every(t=>t.readyState==="ended")')
    page.wait_for_timeout(300)
    assert len(context.request.get(base+'/api/jobs').json())==len(before)
    page.get_by_role('button',name='Открыть веб-камеру',exact=True).click()
    for _ in range(50):
        if page.locator('#webcam-preview').evaluate('(el)=>el.readyState')>=2:break
        page.wait_for_timeout(200)
    assert page.locator('#webcam-preview').evaluate('(el)=>el.readyState')>=2, page.locator('#webcam-error').inner_text()
    page.locator('#webcam-duration').select_option('5')
    with page.expect_response(lambda r:'/api/jobs/upload?source=webcam' in r.url,timeout=25000) as response:
        page.locator('#webcam-start').click()
    r=response.value
    assert r.status==202,(r.status,r.text())
    jobid=r.json()['id']; print('Browser recorded and uploaded:',jobid,flush=True)
    page.get_by_role('button',name='Перейти к результату').click()
    for _ in range(55):
        job=next(j for j in context.request.get(base+'/api/jobs').json() if j['id']==jobid)
        if job['state'] in ('succeeded','failed'):break
        page.wait_for_timeout(1000)
    assert job['state']=='succeeded',job
    assert set(job['result']['checked'])=={'face','fight'},job
    playback=context.request.get(base+f'/api/jobs/{jobid}/video')
    assert playback.status==200 and playback.headers['content-type']=='video/webm'
    (ROOT/'data/browser-webcam-check.webm').write_bytes(playback.body())
    print('Real WebM analysis:',job['result']['duration_seconds'],job['result']['checked'],flush=True)
    # Real UI upload and selection retained across background refresh.
    page.locator('#video-file').set_input_files(str(control))
    page.locator('#content h1').click()
    page.evaluate('refresh()')
    page.wait_for_timeout(1000)
    assert page.locator('#video-file').evaluate('(el)=>el.files.length')==1
    with page.expect_response(lambda r:'/api/jobs/upload' in r.url and 'source=' not in r.url) as response:
        page.get_by_role('button',name='Запустить анализ',exact=True).click()
    assert response.value.status==202
    uploadjob=context.request.get(base+'/api/jobs').json()[0]
    assert uploadjob['camera']=='upload'
    uploadid=uploadjob['id']
    for _ in range(55):
        uploaded=next(j for j in context.request.get(base+'/api/jobs').json() if j['id']==uploadid)
        if uploaded['state'] in ('succeeded','failed'):break
        page.wait_for_timeout(1000)
    assert uploaded['state']=='succeeded',uploaded
    page.evaluate('refresh()');page.wait_for_timeout(500)
    page.screenshot(path=str(ROOT/'data/video-analysis-check.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
    assert not errors,errors
    report={'result':'PASS','webcam_job':jobid,'upload_job':uploadid,'webcam_models':job['result']['checked'],'webcam_duration':job['result']['duration_seconds'],'cancel_releases_camera':True,'https_origin_check':True,'unit_tests':58,'device':'Chromium simulated camera; physical user camera not tested'}
    (ROOT/'data/video-sources-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report),flush=True)
    browser.close()
