import asyncio
from dataclasses import replace
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.admission import candidate, MODEL, process


def person(category='student'):
    return {'name':'Тестовый человек','category':category,'valid_from':time.time()-60,'valid_until':time.time()+3600,'host':'Сотрудник' if category=='guest' else '', 'basis':'Тестовое согласие'}


def entry(logged,app,camera):
    assert logged.post('/api/cameras',json=camera).status_code==201
    assert logged.put('/api/admission/entrances',json={'camera':camera['id'],'zone':'restricted'}).status_code==200
    event={'id':'entry-1','camera':camera['id'],'label':'person','start_time':time.time(),'zones':['restricted'],'has_snapshot':True}
    app.state.engine.ingest([event,event])
    return logged.get('/api/admission').json()['entries'][0],event


def test_private_registry_and_csrf(client,logged):
    # logged shares the client; remove cookie explicitly.
    saved=client.cookies.get('glazza_session');client.cookies.clear()
    assert client.get('/api/admission').status_code==401
    client.cookies.set('glazza_session',saved)
    assert logged.post('/api/admission/people',json=person(),headers={'X-CSRF-Token':'bad'}).status_code==403


def test_roster_validation_guest_expiration_and_revoke(logged):
    body=person('guest');body['host']=''
    assert logged.post('/api/admission/people',json=body).status_code==422
    body=person();body['valid_until']=body['valid_from']
    assert logged.post('/api/admission/people',json=body).status_code==422
    identifier=logged.post('/api/admission/people',json=person()).json()['id']
    assert logged.get('/api/admission/people/'+identifier+'/photo').status_code==404
    body=person();body['enabled']=False
    assert logged.put('/api/admission/people/'+identifier,json=body).status_code==200
    assert not logged.get('/api/admission').json()['people'][0]['enabled']
    assert logged.delete('/api/admission/people/'+identifier).status_code==200
    assert logged.get('/api/admission').json()['people']==[]


def test_camera_zone_required_and_ingest_deduplication(logged,app,camera):
    assert logged.put('/api/admission/entrances',json={'camera':'missing','zone':'none'}).status_code==422
    item,event=entry(logged,app,camera)
    assert item['decision'] is None and item['state']=='pending'
    assert len(logged.get('/api/admission').json()['entries'])==1
    assert logged.get('/api/admission').json()['pending']==1
    assert logged.put('/api/admission/entrances',json={'camera':camera['id'],'zone':'restricted','enabled':False}).status_code==200
    event['id']='another';app.state.engine.ingest([event])
    assert len(logged.get('/api/admission').json()['entries'])==1


def test_no_automatic_permission_review_and_audit(logged,app,camera):
    item,event=entry(logged,app,camera)
    identifier=logged.post('/api/admission/people',json=person()).json()['id']
    assert logged.post('/api/admission/entries/'+item['id']+'/decision',json={'decision':'allowed','note':'Проверка'}).status_code==422
    assert logged.post('/api/admission/entries/'+item['id']+'/decision',json={'decision':'guest','person_id':identifier,'note':'Проверка'}).status_code==422
    assert logged.post('/api/admission/entries/'+item['id']+'/decision',json={'decision':'allowed','person_id':identifier,'note':'Документ проверен'}).status_code==200
    app.state.engine.ingest([event])
    assert logged.get('/api/admission').json()['entries'][0]['decision']=='allowed'
    assert logged.get('/api/admission').json()['pending']==0
    assert logged.post('/api/admission/entries/'+item['id']+'/retry').status_code==409
    assert any(a['action']=='admission.decision' for a in logged.get('/api/audit').json())


def test_expired_person_cannot_be_allowed(logged,app,camera):
    item,_=entry(logged,app,camera);body=person();body['valid_from']=time.time()-3600;body['valid_until']=time.time()-60
    identifier=logged.post('/api/admission/people',json=body).json()['id']
    assert logged.post('/api/admission/entries/'+item['id']+'/decision',json={'decision':'allowed','person_id':identifier,'note':'Проверка'}).status_code==422


def test_candidate_active_only_and_ambiguous_never_confirms(logged,app):
    store=app.state.store;vector=[1.0]+[0.0]*127
    identifier=logged.post('/api/admission/people',json=person()).json()['id']
    store.execute('UPDATE admission_people SET model=?,embedding=? WHERE id=?',(MODEL,json.dumps(vector),identifier))
    assert candidate(store,vector,time.time())[0]==identifier
    second=logged.post('/api/admission/people',json=person()).json()['id']
    store.execute('UPDATE admission_people SET model=?,embedding=? WHERE id=?',(MODEL,json.dumps(vector),second))
    assert candidate(store,vector,time.time())[0] is None
    store.execute('UPDATE admission_people SET enabled=0')
    assert candidate(store,vector,time.time())[2]=='no_active_enrollment'
    with pytest.raises(ValueError):candidate(store,[float('nan')]*128,time.time())


def test_unavailable_model_keeps_review_and_can_retry(logged,app,camera):
    item,_=entry(logged,app,camera)
    app.state.engine.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
    for _ in range(3):asyncio.run(process(app.state.engine))
    row=logged.get('/api/admission').json()['entries'][0]
    assert row['state']=='review' and row['decision'] is None and row['reason']=='recognition_unavailable'
    assert logged.post('/api/admission/entries/'+item['id']+'/retry').status_code==200
    assert logged.get('/api/admission').json()['entries'][0]['attempts']==0


def test_photo_limit_and_no_embeddings_in_summary(logged,app):
    identifier=logged.post('/api/admission/people',json=person()).json()['id']
    assert logged.post('/api/admission/people/'+identifier+'/photo',content=b'x'*(2*1024*1024+1)).status_code==413
    assert 'embedding' not in logged.get('/api/admission').json()['people'][0]


def test_delete_removes_biometrics_and_entry_links(logged,app,camera):
    item,_=entry(logged,app,camera)
    identifier=logged.post('/api/admission/people',json=person()).json()['id']
    app.state.store.execute('UPDATE admission_people SET photo=?,embedding=? WHERE id=?',(b'photo','[]',identifier))
    app.state.store.execute('UPDATE admission_entries SET candidate_id=?,person_id=?',(identifier,identifier))
    logged.delete('/api/admission/people/'+identifier)
    assert app.state.store.rows('SELECT * FROM admission_people')==[]
    row=logged.get('/api/admission').json()['entries'][0]
    assert row['candidate_id'] is None and row['person_id'] is None


def test_demo_read_only(settings):
    with TestClient(create_app(replace(settings,demo=True),background=False)) as client:
        login=client.post('/api/login',json={'username':'admin','password':settings.password}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        assert client.get('/api/admission').status_code==200
        assert client.post('/api/admission/people',json=person()).status_code==409
