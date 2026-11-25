"""Exercise durable resolution and correction with out-of-order Frigate track updates."""
import json

import pytest

from app import retail_multicam as m
from app import retail_multicam_config
from app.store import Store

ROLES = ('shelf','checkout','exit')
POLYGON = [[.2,.3],[.8,.3],[.8,.9],[.2,.9]]
DESCRIPTOR = [100,0,0,0,0,0,0,0,0,0,0, 0,0,0,0,100,0,0,0,0,0,0]


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path/'shop')
    retail_multicam_config.initialize(st)
    m.initialize(st)
    st.execute("INSERT INTO sites VALUES('shop','default','Store','retail','[]')")
    for role in ROLES:
        zones = [{'name':role,'points':POLYGON,'restricted':False}]
        st.execute('INSERT INTO cameras(id,site_id,name,stream,zones) VALUES(?,?,?,?,?)',
                   (role,'shop',role,'rtsp://'+role,json.dumps(zones)))
    st.execute("""INSERT INTO retail_multicam_routes VALUES(
        'shop','shelf','shelf','checkout','checkout','exit','exit',1,24,100,'active',NULL)""")
    for at in range(100,221,5):
        for role in ROLES:
            m.record_health(st,role,at,True)
    return st


def track(st,role,identifier,start,*,close=True,end=None,snapshot=True,zone=True,points=None):
    end = start+4 if end is None else end
    path = points if points is not None else [[([.3+.08*t,.6] if zone else [.02+.03*t,.2]),start+t] for t in range(5)]
    data = {'path_data':path,'box':[.25,.12,.13,.6],'snapshot_frame_time':start+2}
    event = {'id':identifier,'camera':role,'label':'person','start_time':start,
             'end_time':end if close else None,'has_clip':close,'has_snapshot':snapshot,'data':data,
             'zones':[role] if zone else []}
    orig = st.event(source='frigate',source_id=identifier,camera=role,kind='person',started=start,
                    ended=end if close else None,score=None,site_id='shop')
    cam = next(c for c in st.cameras() if c['id']==role)
    m.observe(st,cam,event,orig,'shop')


def appearance(st, now=182, choose=lambda job:True):
    for job in m.snapshot_jobs(st,now):
        if choose(job):
            m.snapshot_result(st,job,DESCRIPTOR,now)


def cards(st):
    return [e for e in st.events(limit=100,site_id='shop') if e['source']=='retail_multicam']


def prepare(st):
    track(st,'shelf','s',140)
    track(st,'exit','e',156)
    appearance(st)


def test_unique_observations_need_health_signatures_and_settle(store):
    prepare(store)
    m.advance(store,180)
    assert not cards(store)
    m.advance(store,195)
    card, = cards(store)
    assert card['kind']=='theft' and card['score'] is None and card['details']['association_state']=='linked'
    assert [e['role'] for e in card['details']['evidence']]==['shelf','exit']
    assert not m.snapshot_jobs(store,198)
    assert m.configured_zones(store)=={role:role for role in ROLES}
    # Replay exact same observations even after restart.
    m.initialize(store)
    prepare(store)
    m.advance(store,210)
    assert [e['id'] for e in cards(store)]==[card['id']]


@pytest.mark.parametrize('status',['new','confirmed','dismissed'])
def test_late_checkout_withdraws_link_and_preserves_review(store,status):
    prepare(store)
    m.advance(store,195)
    old,=cards(store)
    store.execute("UPDATE events SET status=?,note='review note' WHERE id=?", (status,old['id']))
    track(store,'checkout','late',147)
    m.advance(store,215)
    withdrawn,=cards(store)
    assert withdrawn['id']==old['id'] and withdrawn['details']['association_state']=='withdrawn'
    assert not withdrawn['details']['review_required'] and withdrawn['note']=='review note'
    assert withdrawn['status']==('dismissed' if status=='new' else status)
    m.advance(store,220)
    assert len(store.rows("SELECT 1 FROM audit WHERE action='retail.multicam.withdraw'"))==1


@pytest.mark.parametrize('obstacle', ['camera_gap','lost_track','crowd_shelf','crowd_exit','unknown_pose','late_snapshot','wrong_order'])
def test_uncertain_intervals_abstain(store,obstacle):
    prepare(store)
    if obstacle=='camera_gap':
        store.execute("DELETE FROM retail_multicam_health WHERE camera='checkout' AND at BETWEEN 140 AND 165")
    if obstacle=='lost_track':
        m.invalidate_track(store,'checkout','lost',145,152,'shop')
    if obstacle=='crowd_shelf':
        track(store,'shelf','second',143)
    if obstacle=='crowd_exit':
        track(store,'exit','second',160)
    if obstacle=='unknown_pose':
        track(store,'checkout','unknown',146,zone=False,points=[])
    if obstacle=='late_snapshot':
        track(store,'checkout','other',146,snapshot=False)
    if obstacle=='wrong_order':
        track(store,'checkout','other',143)
    m.advance(store,200)
    assert cards(store)==[]


def test_full_route_waits_for_open_checkout_and_fetches_snapshot_afterward(store):
    prepare(store)
    track(store,'checkout','c',148,close=False)
    m.advance(store,190)
    assert not cards(store)
    track(store,'checkout','c',148)
    m.advance(store,195)
    assert not cards(store)
    appearance(store,200)
    m.advance(store,201)
    card,=cards(store)
    assert card['kind']=='retail_journey' and card['status']=='confirmed'
    assert len(card['details']['evidence'])==3 and not card['details']['review_required']


def test_strictly_absent_checkout_bystander_and_boundary_time(store):
    prepare(store)
    track(store,'checkout','bystander',147,zone=False)
    m.advance(store,195)
    assert cards(store)[0]['kind']=='theft'


def test_a_late_camera_outage_withdraws_unsupported_case(store):
    prepare(store)
    m.advance(store,195)
    m.record_health(store,'checkout',150,False)
    m.advance(store,200)
    assert cards(store)[0]['details']['association_state']=='withdrawn'
