import json
import time
import uuid
import pytest
from app.platform import modules_for
from app.store import Store
from app.admission import candidate,MODEL


def site(logged,profile='retail',modules=None):
    org=logged.post('/api/platform/organizations',json={'name':'Тестовая сеть'}).json()['id']
    body={'organization_id':org,'name':'Филиал','profile':profile}
    if modules is not None:body['modules']=modules
    response=logged.post('/api/platform/sites',json=body)
    assert response.status_code==201,response.text
    return response.json()['id'],org


def test_platform_requires_auth_and_csrf(client,logged):
    token=logged.cookies.get('glazza_session');logged.cookies.clear()
    assert logged.get('/api/platform').status_code==401
    logged.cookies.set('glazza_session',token)
    assert logged.post('/api/platform/organizations',json={'name':'Новая'},headers={'X-CSRF-Token':'bad'}).status_code==403


def test_profiles_are_explicit_and_planned_modules_cannot_be_enabled(logged):
    p=logged.get('/api/platform').json()
    assert p['access_model']=='single_trusted_administrator'
    assert {'education','retail','warehouse','office','parking','general'}==set(p['profiles'])
    assert p['modules']['queue']['state']=='planned'
    org=p['organizations'][0]['id']
    for module in ['queue','pos','missing']:
        assert logged.post('/api/platform/sites',json={'name':'Магазин','organization_id':org,'profile':'retail','modules':[module]}).status_code==422
    assert logged.post('/api/platform/sites',json={'name':'Объект','organization_id':'missing','profile':'general'}).status_code==422


def test_retail_defaults_do_not_claim_theft_or_face(logged,app):
    identifier,_=site(logged)
    assert modules_for(app.state.store,identifier)=={'person','intrusion','fight'}
    assert 'theft' not in modules_for(app.state.store,identifier)


def test_site_filters_camera_assignment_and_historical_attribution(logged,app,camera):
    identifier,_=site(logged)
    assert logged.post('/api/cameras',json=camera).status_code==201
    old=app.state.store.event(source='test',source_id='old',camera=camera['id'],kind='person',score=.9,started=time.time())
    assert logged.put('/api/platform/cameras/'+camera['id']+'/site',json={'site_id':identifier}).status_code==200
    newer=app.state.store.event(source='test',source_id='new',camera=camera['id'],kind='person',score=.9,started=time.time())
    assert logged.get('/api/cameras?site_id=default').json()==[]
    assert logged.get('/api/cameras?site_id='+identifier).json()[0]['id']==camera['id']
    assert [e['id'] for e in logged.get('/api/events?site_id=default').json()]==[old]
    assert [e['id'] for e in logged.get('/api/events?site_id='+identifier).json()]==[newer]
    assert logged.get('/api/overview?site_id='+identifier).json()['cameras']==1
    assert logged.get('/api/overview?site_id=default').json()['counts']['person']==1
    # Older clients cannot accidentally move a camera back to the default site.
    assert logged.put('/api/cameras/'+camera['id'],json=camera).status_code==200
    assert logged.get('/api/cameras?site_id='+identifier).json()


@pytest.mark.parametrize('route',['/api/cameras','/api/events','/api/overview','/api/jobs','/api/admission'])
def test_unknown_site_not_silently_all_objects(logged,route):
    assert logged.get(route+'?site_id=missing').status_code==404


def test_disabled_modules_prevent_new_fight_and_admission_and_intrusion(logged,app,camera):
    identifier,org=site(logged,modules=[])
    camera['site_id']=identifier
    assert logged.post('/api/cameras',json=camera).status_code==201
    logged.put('/api/admission/entrances',json={'camera':camera['id'],'zone':'restricted'})
    now=time.time()
    app.state.engine.ingest([{'id':'zone-1','label':'person','camera':camera['id'],'start_time':now-60,'end_time':now,'zones':['restricted'],'has_clip':True}])
    assert app.state.store.events()[0]['kind']=='person'
    assert app.state.store.rows('SELECT * FROM jobs')==[]
    assert logged.get('/api/admission').json()['entries']==[]
    app.state.engine.health.update(frigate='online',analyzer={'state':'ready','models':[{'id':'fight','state':'ready'}]})
    app.state.engine.schedule_fights(now)
    assert app.state.store.rows('SELECT * FROM jobs')==[]


def test_ingested_job_remembers_site_after_camera_move(logged,app,camera):
    identifier,_=site(logged)
    camera['site_id']=identifier;logged.post('/api/cameras',json=camera)
    now=time.time()
    app.state.engine.ingest([{'id':'job-source','label':'person','camera':camera['id'],'start_time':now-10,'end_time':now,'has_clip':True}])
    job=app.state.store.rows('SELECT * FROM jobs')[0]
    assert job['site_id']==identifier
    logged.put('/api/platform/cameras/'+camera['id']+'/site',json={'site_id':'default'})
    app.state.engine.apply_result(job,{'detections':[{'kind':'fight','score':.9}]})
    event=logged.get('/api/events?kind=fight&site_id='+identifier).json()[0]
    assert event['site_id']==identifier
    assert logged.get('/api/events?kind=fight&site_id=default').json()==[]


def test_face_roster_candidates_are_scoped_to_site(logged,app):
    identifier,_=site(logged,profile='education')
    body={'name':'Тестовый сотрудник','category':'staff','valid_from':time.time()-60,'valid_until':time.time()+3600,'basis':'Разрешение','site_id':identifier}
    person=logged.post('/api/admission/people',json=body).json()['id']
    vector=[1.0]+[0.0]*127
    app.state.store.execute('UPDATE admission_people SET model=?,embedding=? WHERE id=?',(MODEL,json.dumps(vector),person))
    assert candidate(app.state.store,vector,time.time())[0] is None
    assert candidate(app.state.store,vector,time.time(),identifier)[0]==person
    assert logged.get('/api/admission?site_id=default').json()['people']==[]


def test_upload_job_site_and_persistent_migration(logged,app,settings):
    identifier,_=site(logged)
    r=logged.post('/api/jobs/upload?site_id='+identifier,content=b'0000ftyp'+b'0'*24,headers={'Content-Type':'video/mp4'})
    assert r.status_code==202,r.text
    assert logged.get('/api/jobs?site_id='+identifier).json()[0]['site_id']==identifier
    assert logged.get('/api/jobs?site_id=default').json()==[]
    reopened=Store(settings.data)
    assert modules_for(reopened,identifier)=={'person','intrusion','fight'}
    assert len(reopened.rows('SELECT * FROM organizations'))==2
    assert reopened.rows('SELECT site_id FROM jobs')[0]['site_id']==identifier


def test_no_video_upload_when_only_observation_modules_enabled(logged,app):
    identifier,_=site(logged,modules=[])
    assert logged.post('/api/jobs/upload?site_id='+identifier,content=b'0000ftyp'+b'0'*24,headers={'Content-Type':'video/mp4'}).status_code==422
    assert app.state.store.rows('SELECT * FROM jobs')==[]


def test_worker_does_not_run_unrequested_face_or_fight(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    import analyzer.main as worker
    calls=[]
    class Vad:
        def __init__(self,root):pass
        def analyze(self,frames):
            calls.append('theft')
            return [{'kind':'theft','score':.9},{'kind':'fight','score':.95}]
    class Face:
        def __init__(self,root):self.model=self
        def analyze(self,frames):raise AssertionError('Face must not run')
        def close(self):pass
    class Fight:
        def __init__(self,root):pass
        def analyze(self,frames):raise AssertionError('Fight must not run')
    class Identity:
        def __init__(self,root):pass
    monkeypatch.setattr(worker,'VadClipModel',Vad)
    monkeypatch.setattr(worker,'FaceModel',Face)
    monkeypatch.setattr(worker,'FightModel',Fight)
    monkeypatch.setattr(worker,'IdentityModel',Identity)
    monkeypatch.setattr(worker,'read_video',lambda *a,**kw:([None],10))
    monkeypatch.setenv('GLAZZA_WORKER_KEY','worker-test-key-'*3)
    monkeypatch.setenv('GLAZZA_DATA',str(tmp_path))
    (tmp_path/'jobs').mkdir()
    identifier=str(uuid.uuid4());(tmp_path/'jobs'/f'{identifier}.mp4').write_bytes(b'video')
    with TestClient(worker.create_app()) as client:
        result=client.post('/analyze',json={'job_id':identifier,'modules':['theft']},headers={'X-Worker-Key':'worker-test-key-'*3})
        assert result.status_code==200,result.text
        assert result.json()['checked']==['theft']
        assert [d['kind'] for d in result.json()['detections']]==['theft']
        assert calls==['theft']
