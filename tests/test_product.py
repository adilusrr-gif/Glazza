import asyncio
from dataclasses import replace
import hashlib
import json
import time
from pathlib import Path
import uuid

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.frigate import make_config


def test_auth_required_for_private_routes(client):
    for route in ['/api/events','/api/cameras','/api/overview','/api/jobs','/api/audit','/api/session']:
        assert client.get(route).status_code == 401
    assert client.get('/healthz').status_code == 200


def test_auth_cookie_and_logout(logged):
    assert logged.get('/api/session').json()['username'] == 'admin'
    assert logged.post('/api/logout').status_code == 200
    assert logged.get('/api/events').status_code == 401


def test_password_not_stored_in_plaintext(logged, app):
    password = app.state.store.rows('SELECT password FROM users')[0]['password']
    assert password != 'test-password-12345'
    assert ':' in password
    token = logged.cookies.get('glazza_session')
    assert app.state.store.rows('SELECT token FROM sessions')[0]['token'] == hashlib.sha256(token.encode()).hexdigest()


def test_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/login',json={'username':'admin','password':'incorrect'}).status_code == 401
    assert client.post('/api/login',json={'username':'admin','password':'incorrect'}).status_code == 429


def test_csrf_and_origin(logged, camera):
    assert logged.post('/api/cameras',json=camera,headers={'X-CSRF-Token':'wrong'}).status_code == 403
    assert logged.post('/api/cameras',json=camera,headers={'Origin':'https://attacker.example'}).status_code == 403
    assert logged.post('/api/cameras',json=camera,headers={'Origin':'http://testserver'}).status_code == 201


def test_camera_roundtrip_redacts_stream(logged, camera):
    assert logged.post('/api/cameras',json=camera).status_code == 201
    response = logged.get('/api/cameras')
    assert response.json()[0]['name'] == camera['name']
    assert 'secret' not in response.text
    assert 'stream' not in response.json()[0]
    assert logged.post('/api/cameras',json=camera).status_code == 409


@pytest.mark.parametrize('stream', ['file:///etc/passwd','http://169.254.169.254/','rtsp://host/{SECRET}','rtsp://host:99999','rtsp://host/with space'])
def test_invalid_camera_urls(logged, camera, stream):
    camera['stream'] = stream
    response=logged.post('/api/cameras',json=camera)
    assert response.status_code == 422
    assert stream not in response.text


def test_zones_require_area_and_unique_names(logged,camera):
    camera['zones'][0]['points'] = [[0,0],[0.5,0],[1,0]]
    assert logged.post('/api/cameras',json=camera).status_code == 422
    camera['zones'][0]['points'] = [[0,0],[1,0],[1,1]]
    camera['zones'].append(camera['zones'][0].copy())
    assert logged.post('/api/cameras',json=camera).status_code == 422


def test_apply_config_offline_saves_and_reports_restart_failure(logged,app,camera,settings):
    logged.post('/api/cameras',json=camera)
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
    response=logged.post('/api/config/apply')
    assert response.status_code == 202
    assert response.json()['saved'] and not response.json()['restarted']
    config=yaml.safe_load(settings.frigate_config.read_text())
    assert config['record']['motion']['days'] == 1
    assert 'retain' not in config['record']  # Frigate >=0.17 schema.
    assert config['cameras']['entrance']['zones']['restricted']['objects'] == ['person']
    assert settings.frigate_config.stat().st_mode & 0o777 == 0o600


def test_frigate_ingestion_dedupes_preserves_review_and_queues(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    event={'id':'1234-test','camera':'entrance','label':'person','start_time':time.time(),'end_time':time.time()+10,
           'zones':['restricted'],'has_clip':True,'has_snapshot':True,'data':{'top_score':.95}}
    app.state.engine.ingest([event,event])
    events=logged.get('/api/events').json()
    assert len(events)==1 and events[0]['kind']=='intrusion'
    assert len(logged.get('/api/jobs').json())==1
    response=logged.patch('/api/events/'+events[0]['id'],json={'status':'dismissed','note':'Сотрудник, разрешённый доступ'})
    assert response.status_code==200
    app.state.engine.ingest([event])
    assert logged.get('/api/events').json()[0]['status']=='dismissed'
    assert any(a['action']=='event.review' for a in logged.get('/api/audit').json())


def test_unknown_camera_false_positive_and_other_objects_ignored(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    base={'id':'123-test','camera':'entrance','label':'person','start_time':time.time()}
    app.state.engine.ingest([{**base,'camera':'unknown'},{**base,'label':'car'},{**base,'false_positive':True},{**base,'id':'../evil'}])
    assert logged.get('/api/events').json()==[]


def test_poll_revisits_active_objects_and_closes_events(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    now=time.time()
    event={'id':'123-active','camera':'entrance','label':'person','start_time':now-7200,'end_time':None,'zones':[]}
    app.state.engine.ingest([event])
    calls=[]
    def transport(request):
        calls.append(str(request.url))
        if request.url.path.endswith('/123-active'):
            return httpx.Response(200,json={**event,'end_time':now,'zones':['restricted'],'has_clip':True})
        return httpx.Response(200,json=[])
    app.state.engine.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    asyncio.run(app.state.engine.sync())
    assert any('/123-active' in c for c in calls)
    assert logged.get('/api/events?kind=intrusion').json()
    assert logged.get('/api/jobs').json()


def test_poll_does_not_advance_cursor_after_failure(logged,app):
    app.state.engine.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(app.state.engine.sync())
    assert app.state.store.rows("SELECT * FROM meta WHERE key='poll_cursor'")==[]


def test_upload_stream_limit_cleans_partial_file(logged,app,settings):
    object.__setattr__(settings,'max_upload',32)
    response=logged.post('/api/jobs/upload',content=b'0000ftyp'+b'x'*100,headers={'Content-Type':'video/mp4'})
    assert response.status_code==413
    assert list((settings.data/'jobs').iterdir())==[]


def test_upload_validate_and_create_durable_job(logged,settings):
    assert logged.post('/api/jobs/upload',content=b'notvideo',headers={'Content-Type':'video/mp4'}).status_code==422
    response=logged.post('/api/jobs/upload',content=b'0000ftyp'+b'0'*24,headers={'Content-Type':'video/mp4'})
    assert response.status_code==202
    job_id=response.json()['id']
    assert (settings.data/'jobs'/f'{job_id}.mp4').exists()
    assert logged.get('/api/jobs').json()[0]['state']=='queued'
    assert logged.get('/api/jobs/'+job_id+'/video').status_code==200


def test_analysis_result_saved_as_review_candidate(logged,app):
    job={'id':str(uuid.uuid4()),'camera':'upload','created':time.time(),'event_id':None}
    app.state.store.execute('INSERT INTO jobs(id,camera,state,created,updated) VALUES(?,?,?,?,?)',(job['id'],'upload','running',job['created'],job['created']))
    result={'model':'test-adapter','detections':[{'kind':'fight','score':.82,'label':'Fighting'}],'warnings':[]}
    app.state.engine.apply_result(job,result)
    events=logged.get('/api/events').json()
    assert events[0]['status']=='new'
    assert events[0]['details']['review_required']
    assert logged.get('/api/jobs').json()[0]['state']=='succeeded'


def test_retention_removes_finished_upload_only(logged,app,settings):
    old=time.time()-90000
    ids=[str(uuid.uuid4()),str(uuid.uuid4())]
    for job_id,state in zip(ids,['succeeded','queued']):
        (settings.data/'jobs'/f'{job_id}.mp4').write_bytes(b'video')
        app.state.store.execute('INSERT INTO jobs(id,camera,state,created,updated,file) VALUES(?,?,?,?,?,?)',(job_id,'upload',state,old,old,f'{job_id}.mp4'))
    app.state.engine.cleanup()
    assert not (settings.data/'jobs'/f'{ids[0]}.mp4').exists()
    assert (settings.data/'jobs'/f'{ids[1]}.mp4').exists()


def test_demo_cannot_mutate_camera_and_has_no_fake_media(settings,camera):
    with TestClient(create_app(replace(settings,demo=True),background=False)) as client:
        login=client.post('/api/login',json={'username':'admin','password':settings.password}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        assert client.post('/api/cameras',json=camera).status_code==409
        assert client.post('/api/config/apply').status_code==409
        events=client.get('/api/events').json()
        assert len(events)==4 and all(e['source']=='demo' for e in events)
        assert client.get('/api/events/'+events[0]['id']+'/media/clip').status_code==404
    with pytest.raises(RuntimeError,match='separate'):
        create_app(settings,background=False)


def test_secure_headers_and_cookie(settings):
    with TestClient(create_app(replace(settings,secure_cookie=True),background=False),base_url='https://testserver') as client:
        r=client.post('/api/login',json={'username':'admin','password':settings.password})
        assert 'HttpOnly' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
        assert r.headers['x-frame-options']=='DENY'
        assert r.headers['cache-control']=='no-store'


def test_boot_refuses_weak_secrets(settings):
    with pytest.raises(RuntimeError):create_app(replace(settings,password='admin'))
    with pytest.raises(RuntimeError):create_app(replace(settings,worker_key='short'))


def test_source_scoped_camera_id_prevents_path_traversal(logged):
    assert logged.get('/api/cameras/not-configured/snapshot').status_code==404
    assert logged.get('/api/events/unknown/media/clip').status_code==404


def test_csv_like_note_not_interpreted(logged,app):
    id_=app.state.store.event(source='test',source_id='1',camera='entrance',kind='person',score=.8,started=time.time())
    note='<script>alert(1)</script> =IMPORTDATA("bad")'
    assert logged.patch('/api/events/'+id_,json={'status':'confirmed','note':note}).status_code==200
    assert logged.get('/api/events').json()[0]['note']==note


def test_zone_cannot_use_another_camera_id(logged,camera):
    assert logged.post('/api/cameras',json=camera).status_code==201
    another={**camera,'id':'restricted','zones':[]}
    assert logged.post('/api/cameras',json=another).status_code==422
    another['id']='second'
    assert logged.post('/api/cameras',json=another).status_code==201
    zones={'zones':[{**camera['zones'][0],'name':'second'}]}
    assert logged.put('/api/cameras/entrance/zones',json=zones).status_code==422


def test_delete_camera_closes_old_active_events(logged,app,camera):
    logged.post('/api/cameras',json=camera)
    app.state.store.event(source='frigate',source_id='active',camera='entrance',kind='person',score=.9,started=time.time())
    assert logged.delete('/api/cameras/entrance').status_code==200
    assert logged.get('/api/events').json()[0]['ended'] is not None
