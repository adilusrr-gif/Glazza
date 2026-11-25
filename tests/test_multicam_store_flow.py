"""Ten configured cameras, a simulated Frigate HTTP feed and real Glazza processing.

Synthetic drawings stand in for person crops. This regression exercises event fetching,
clothing descriptors, association, persistence and evidence delivery; it does not
decode ten RTSP streams or measure person-matching accuracy on store footage.
"""
import asyncio
from io import BytesIO
import math
from pathlib import Path
import time

import httpx
from PIL import Image, ImageDraw
import pytest


CAMERAS = [f'camera_{i:02}' for i in range(1, 11)]
SHELF, CHECKOUT, EXIT = CAMERAS[0], CAMERAS[4], CAMERAS[9]
ROLES = {SHELF: 'shelf', CHECKOUT: 'checkout', EXIT: 'exit'}
ZONE = [[.2, .3], [.8, .3], [.8, .92], [.2, .92]]


def outfit_image(camera, distractor, image_format='JPEG'):
    upper, lower = ((46, 172, 69), (202, 137, 45)) if distractor else ((184, 52, 39), (42, 70, 168))
    delta = {SHELF: 0, CHECKOUT: 5, EXIT: -3}.get(camera, 2)
    image = Image.new('RGB', (320, 320), (163, 162, 156))
    brush = ImageDraw.Draw(image)
    brush.ellipse((132, 24, 187, 80), fill=(96, 92, 85))
    brush.polygon([(116, 79), (203, 79), (217, 173), (103, 173)],
                  fill=tuple(n+delta for n in upper))
    brush.rectangle((115, 173, 203, 275), fill=tuple(n+delta for n in lower))
    buf = BytesIO()
    image.save(buf, format=image_format)
    return buf.getvalue()


@pytest.fixture(scope='module')
def clips():
    """Each pre-encoded clip shows the synthetic shopper in its own camera's lighting."""
    directory = Path(__file__).with_name('fixtures')
    media = {camera: (directory / f'retail-{role}.mp4').read_bytes() for camera, role in ROLES.items()}
    assert len(set(media.values())) == len(ROLES)
    return media


class StoreFeed:
    def __init__(self, engine, clips):
        self.engine = engine
        self.clips = clips
        self.base = math.ceil(time.time())
        self.now = self.base
        self.events = {}
        self.snapshots = set()
        self.outages = {}
        self.config = {}

    def person(self, identifier, camera, start, *, distractor=False, finishes_at=None, visible_at=None):
        self.events[identifier] = dict(camera=camera, start=start, end=start+4, distractor=distractor,
                                       finishes_at=finishes_at if finishes_at is not None else start+4,
                                       visible_at=visible_at if visible_at is not None else start)

    def event(self, identifier):
        row = self.events[identifier]
        at = self.base + row['start']
        complete = self.now >= self.base + row['finishes_at']
        zone = ROLES.get(row['camera'], 'aisle')
        points = [([.30+.10*i, .68], at+i) for i in range(5) if at+i <= self.now]
        return {'id': identifier, 'camera': row['camera'], 'label': 'person', 'start_time': at,
                'end_time': self.base + row['end'] if complete else None, 'zones': [zone],
                'false_positive': False, 'has_snapshot': complete, 'has_clip': complete,
                'data': {'top_score': .98, 'path_data': points, 'snapshot_frame_time':at+2, 'box':[.32,.05,.36,.90]}}

    def request(self, req):
        assert req.method == 'GET'
        path = req.url.path
        if path == '/api/stats':
            cameras = {}
            for camera in self.config:
                first, last = self.outages.get(camera, (0, -1))
                fps = 0 if first <= self.now-self.base <= last else 5
                cameras[camera] = dict(camera_fps=fps, process_fps=fps, detection_fps=fps,
                                       skipped_fps=0, detection_enabled=True)
            return httpx.Response(200, json={'service': {'last_updated': self.now}, 'cameras': cameras,
                                             'detectors': {'cpu': {'pid':1234,'detection_start':0}}})
        if path == '/api/config':
            return httpx.Response(200, json={'cameras': self.config})
        if path == '/api/events':
            params = req.url.params
            start, end = float(params.get('after', 0)), float(params.get('before', self.now))
            cameras = params.get('cameras',params.get('camera','all')).split(',')
            records = [self.event(k) for k,r in self.events.items()
                       if start <= self.base+r['start'] <= end and self.base+r['visible_at'] <= self.now
                       and ('all' in cameras or r['camera'] in cameras)]
            if params.get('in_progress') in ('1','true'):
                records = [r for r in records if r['end_time'] is None]
            return httpx.Response(200, json=records)
        prefix = '/api/events/'
        assert path.startswith(prefix), path
        identifier, _, media = path.removeprefix(prefix).partition('/')
        event = self.events[identifier]
        if media == 'snapshot-clean.webp':
            self.snapshots.add(identifier)
            return httpx.Response(200, content=outfit_image(event['camera'], event['distractor'], 'WEBP'),
                                  headers={'Content-Type': 'image/webp'})
        if media == 'clip.mp4':
            return httpx.Response(200, content=self.clips[event['camera']], headers={'Content-Type': 'video/mp4'})
        assert media == ''
        return httpx.Response(200, json=self.event(identifier))

    def poll(self, start=0, end=100):
        async def advance():
            for offset in range(start, end+1, 5):
                self.now = self.base + offset
                await self.engine.sync_camera_signals()
                await self.engine.sync()
                await self.engine.sync_retail_snapshots()
        asyncio.run(advance())


@pytest.fixture
def shop(app, logged, clips, monkeypatch):
    engine = app.state.engine
    feed = StoreFeed(engine, clips)
    monkeypatch.setattr(time, 'time', lambda: feed.now)
    site = logged.post('/api/platform/sites', json={'organization_id': 'default', 'name': '10 камер',
                                                    'profile': 'retail', 'modules': ['person']}).json()['id']
    for camera in CAMERAS:
        zone = ROLES.get(camera, 'aisle')
        cam = {'id': camera, 'name': camera, 'site_id': site, 'stream': 'rtsp://camera.test/'+camera,
               'analyze': False, 'zones': [{'name': zone, 'restricted': False, 'points': ZONE}]}
        assert logged.post('/api/cameras', json=cam).status_code == 201
        feed.config[camera] = {'enabled': True, 'detect': {'enabled': True, 'fps': 5, 'width': 640, 'height': 360},
                               'objects': {'track': ['person']}, 'record': {'enabled': True},
                               'snapshots': {'enabled': True},
                               'zones': {zone: {'coordinates': ','.join(str(n) for pt in ZONE for n in pt),
                                                'objects': ['person']}}}
    rule = {'min_seconds': 1, 'max_seconds': 24}
    for camera, role in ROLES.items():
        rule[role+'_camera'] = camera
        rule[role+'_zone'] = role
    url = '/api/retail/multicam/'+site
    response = logged.put(url, json=rule)
    assert response.is_success, response.text
    assert response.json()['rule']['state'] == 'active'
    asyncio.run(engine.client.aclose())
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(feed.request))
    # Distractors from the other seven cameras should never form evidence on this route.
    for camera in CAMERAS:
        if camera not in ROLES:
            feed.person('background-'+camera, camera, 47, distractor=False)
    feed.person('shelf-person', SHELF, 40)
    feed.person('exit-person', EXIT, 56)
    return site, feed


def decisions(client, site):
    result = client.get('/api/events', params={'site_id': site, 'limit': 100})
    assert result.is_success
    return [r for r in result.json() if r['source'] == 'retail_multicam']


@pytest.mark.parametrize('with_checkout', [True, False], ids=['via-checkout', 'bypass'])
def test_ten_camera_route_evidence_and_replay(shop, logged, with_checkout):
    site, feed = shop
    if with_checkout:
        # The register track starts on time but remains open after the exit has closed.
        feed.person('checkout-person', CHECKOUT, 48, finishes_at=68)
    feed.poll(end=65)
    assert decisions(logged, site) == []
    feed.poll(70, 100)
    decision, = decisions(logged, site)
    cameras = [SHELF, CHECKOUT, EXIT] if with_checkout else [SHELF, EXIT]
    signal = 'multicam_via_checkout' if with_checkout else 'multicam_shelf_to_exit'
    assert (decision['kind'], decision['details']['signal']) == ('retail_journey' if with_checkout else 'theft', signal)
    evidence = decision['details']['evidence']
    assert [leg['camera'] for leg in evidence] == cameras
    assert [leg['role'] for leg in evidence] == [ROLES[c] for c in cameras]
    assert set(feed.snapshots) == {ROLES[c]+'-person' for c in cameras}
    for leg in evidence:
        origin = logged.get('/api/events/' + leg['origin_event']).json()
        assert (origin['source'], origin['camera'], origin['site_id']) == ('frigate', leg['camera'], site)
        clip = logged.get('/api/events/'+leg['origin_event']+'/media/clip')
        assert clip.is_success and clip.headers['content-type'] == 'video/mp4'
        assert clip.content == feed.clips[leg['camera']]
    if with_checkout:
        # Neutral journeys are operational history, never items in the theft review queue.
        assert decision['status'] == 'confirmed' and not decision['details']['review_required']
        overview = logged.get('/api/overview', params={'site_id':site}).json()
        assert overview['pending_thefts']==0
        return
    reviewed = logged.patch('/api/events/'+decision['id'], json={'status': 'dismissed', 'note': 'Проверено на стенде'})
    assert reviewed.is_success, reviewed.text
    feed.poll(105, 120)
    reviewed, = decisions(logged, site)
    assert reviewed['id'] == decision['id'] and reviewed['status'] == 'dismissed'
    assert decisions(logged, 'default') == []


@pytest.mark.parametrize('obstacle', ['similar-shelf', 'different-checkout', 'checkout-outage'])
def test_ten_camera_uncertainty_suppresses_bypass(shop, logged, obstacle):
    site, feed = shop
    if obstacle == 'similar-shelf':
        feed.person('second-shelf', SHELF, 41)
    elif obstacle == 'different-checkout':
        feed.person('other-customer', CHECKOUT, 48, distractor=True)
    else:
        feed.outages[CHECKOUT] = (45, 55)
    feed.poll()
    assert 'shelf-person' in feed.snapshots and 'exit-person' in feed.snapshots
    assert decisions(logged, site) == []


def test_late_frigate_checkout_withdraws_open_theft_card(shop, logged):
    site, feed = shop
    feed.person('late-checkout', CHECKOUT, 48, visible_at=117)
    feed.poll(end=110)
    card, = decisions(logged, site)
    assert card['status']=='new' and card['kind']=='theft'
    feed.poll(115,175)
    resolved, = decisions(logged, site)
    assert resolved['id']==card['id'] and resolved['status']=='dismissed'
    assert resolved['details']['association_state']=='withdrawn'
    overview = logged.get('/api/overview', params={'site_id':site}).json()
    assert overview['pending_thefts']==0
