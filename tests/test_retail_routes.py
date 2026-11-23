import copy
import time

import pytest

ROUTE = {'shelf_zone':'shelf', 'checkout_zone':'till', 'exit_zone':'exit'}


def zone(name, x, y, width=.20, height=.18):
    return {'name':name, 'restricted':False,
            'points':[[x,y],[x+width,y],[x+width,y+height],[x,y+height]]}


@pytest.fixture
def retail_camera(logged, camera):
    payload = {'organization_id':'default', 'name':'Магазин', 'profile':'retail', 'modules':['person']}
    site_id = logged.post('/api/platform/sites', json=payload).json()['id']
    data = {**camera, 'id':'aisle', 'analyze':False, 'site_id':site_id,
            'zones':[zone('shelf',.05,.60),zone('till',.42,.12),zone('exit',.72,.60)]}
    assert logged.post('/api/cameras', json=data).status_code == 201
    return data


def sample_track():
    start = time.time() + .01
    return {'id':'some-person', 'label':'person', 'camera':'aisle',
            'start_time':start,'end_time':start+7,'false_positive':False,'has_clip':True,'has_snapshot':True,
            'zones':['exit','shelf'],  # Deliberately the opposite of capture order.
            'data':{'path_data':[[[x,.68],start+t] for x,t in [(.07,1),(.21,2),(.5,3),(.73,4),(.89,5)]]}}


def test_route_review_lifecycle(app, logged, retail_camera):
    url = '/api/retail/routes/aisle?site_id=' + retail_camera['site_id']
    store, engine = app.state.store, app.state.engine
    assert logged.get(url).json() == {'rule':None}
    assert logged.put(url,json=ROUTE).json()['rule']['state'] == 'active'
    event = sample_track()
    # No operator evidence or alert until the track is complete.
    engine.ingest([{**event,'end_time':None}])
    assert not store.events(kind='theft')
    engine.ingest([event])
    alert, = store.events(kind='theft')
    assert alert['source']=='retail_route' and alert['score'] is None
    assert alert['site_id']==retail_camera['site_id']
    detail = alert['details']
    assert detail['signal']=='shelf_to_exit' and detail['review_required']
    assert (detail['start_seconds'],detail['end_seconds'])==(2,5)
    assert store.events(kind='person')[0]['id']==detail['origin_event']
    assert not store.rows('SELECT id FROM jobs')  # This shop has no AI weights or camera AI enabled.
    assert logged.patch('/api/events/'+alert['id'],json={'status':'dismissed','note':'Посетитель ничего не взял'}).is_success
    engine.ingest([event,event])
    same, = store.events(kind='theft')
    assert same['status']=='dismissed' and same['note']=='Посетитель ничего не взял'
    assert logged.delete(url).json()=={'rule':None}
    engine.ingest([{**sample_track(),'id':'disabled'}])
    assert len(store.events(kind='theft'))==1


def test_normal_and_incomplete_routes(app, logged, retail_camera):
    assert logged.put('/api/retail/routes/aisle',json=ROUTE).is_success
    event = sample_track()
    start = event['start_time']
    path = event['data']['path_data']
    examples = {
        'entered_checkout': {'zones':['shelf','till','exit']},
        'zone_names_only': {'data':{}},
        'omitted_exit_zone': {'zones':['shelf']},
        'incomplete': {'end_time':None},
        'missing_recording': {'has_clip':False},
        'shelf_only': {'data':{'path_data':path[:2]}},
        'single_shelf_point': {'data':{'path_data':path[1:]}},
        'single_exit_point': {'data':{'path_data':path[:-1]}},
        'wrong_order': {'data':{'path_data':[[p[0],start+i] for i,p in enumerate(reversed(path))]}},
        'register_sample': {'data':{'path_data':path[:2]+[[[.45,.19],start+3]]+path[3:]}},
        'register_crossing': {'data':{'path_data':path[:2]+[[[.48,.02],start+2.5],[[.48,.42],start+3]]+path[3:]}},
        'left_then_returned': {'data':{'path_data':path+[[[.18,.67],start+6]]}},
        'turned_back_inside': {'data':{'path_data':path+[[[.5,.67],start+6]]}},
        'lost_after_exit': {'end_time':start+18},
        'broken_time': {'data':{'path_data':[path[0],path[2],path[1],*path[3:]]}},
        'stale_track': {'start_time':1,'end_time':8},
        'malformed': {'data':{'path_data':path[:2]+[[[0,'bad'],start+3]]+path[3:]}},
        'track_gap': {'end_time':start+25,'data':{'path_data':path[:3]+[[p[0],p[1]+10] for p in path[3:]]}},
    }
    for identifier, patch in examples.items():
        app.state.engine.ingest([{**event,**patch,'id':identifier}])
        assert not app.state.store.events(kind='theft'), identifier
    # Missing optional metadata on one upstream event must not stall the entire camera batch.
    app.state.engine.ingest([{**event, 'id':'missing-data', 'data': None}, {**event, 'id':'valid-track'}])
    assert len(app.state.store.events(kind='theft')) == 1


def test_validate_and_invalidate_config(app, logged, retail_camera):
    url = '/api/retail/routes/aisle'
    site = retail_camera['site_id']
    assert logged.put(url+'?site_id=default',json=ROUTE).status_code==404
    assert logged.put(url,json={**ROUTE,'checkout_zone':'shelf'}).status_code==422
    assert logged.put(url,json={**ROUTE,'checkout_zone':'missing'}).status_code==422
    original = logged.put(url,json=ROUTE).json()['rule']
    assert logged.put(url,json=ROUTE).json()['rule']==original
    assert logged.get(url+'?site_id=default').status_code==404
    assert logged.delete(url+'?site_id=default').status_code==404
    zones = copy.deepcopy(retail_camera['zones'])
    zones[1]['points'] = zones[0]['points']
    assert logged.put('/api/cameras/aisle/zones',json={'zones':zones}).is_success
    invalid = logged.get(url).json()['rule']
    assert invalid['state']=='invalidated' and invalid['reason']=='camera_changed'
    assert logged.put(url,json=ROUTE).status_code==422
    assert logged.put('/api/cameras/aisle/zones',json={'zones':retail_camera['zones']}).is_success
    event = sample_track()
    app.state.engine.ingest([event])
    assert not app.state.store.events(kind='theft')
    # An old event presented after the operator re-activates cannot emit.
    rule = logged.put(url,json=ROUTE).json()['rule']
    old = {**event,'id':'previous','start_time':rule['activated_at']-1}
    app.state.engine.ingest([old])
    assert not app.state.store.events(kind='theft')
    assert logged.put('/api/platform/sites/'+site,json={'organization_id':'default','name':'Склад','profile':'warehouse'}).is_success
    assert logged.get(url).json()['rule']['reason']=='profile_changed'
    assert logged.put(url,json=ROUTE).status_code==422
    assert logged.delete('/api/cameras/aisle').is_success
    assert logged.post('/api/cameras',json=retail_camera).is_success
    assert logged.get(url).json()=={'rule':None}
