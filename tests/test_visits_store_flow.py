"""Black-box visit IDs through a ten-camera store and a simulated Frigate HTTP feed.

The application, SQLite, API authentication, person crops and appearance comparison run
normally. Camera events and garment images are synthetic, so this measures continuity of
the application workflow, not person detector accuracy on a shop's RTSP video.
"""
import asyncio
from io import BytesIO
import math
import time

from fastapi.testclient import TestClient
import httpx
from PIL import Image, ImageDraw
import pytest

from app.frigate import make_config
from app.main import create_app


CAMERAS = ('door', 'produce', 'bread', 'dairy', 'tea', 'freezer', 'drinks', 'household', 'cash', 'vestibule')
ENTRANCE = 'entry_line'
INSIDE = 'inside_store'
BACK = CAMERAS[-1]
BACK_ENTRANCE = 'back_entry'
BACK_INSIDE = 'back_inside'
DOOR_POLYGONS = {
    ENTRANCE: [[.25, .24], [.51, .24], [.51, .92], [.25, .92]],
    INSIDE: [[.58, .24], [.89, .24], [.89, .92], [.58, .92]],
}


class VisitStoreFeed:
    def __init__(self):
        self.base = math.ceil(time.time())
        self.now = self.base
        self.tracks = {}
        self.rendered = {}
        self.snapshots = set()
        self.outbound = set()
        self.door_zones = {CAMERAS[0]: DOOR_POLYGONS}

    def track(self, name, camera, offset, *, outfit='red', seconds=10):
        self.tracks[name] = (camera, self.base + offset, self.base + offset + seconds, outfit)

    def event(self, name):
        camera, start, end, _ = self.tracks[name]
        last = min(self.now, end)
        forward = [.16, .38, .62, .68, .74, .80]
        reverse = [.68, .38, .16, .12, .08, .04]
        if camera == BACK and camera in self.door_zones:
            # This door's camera looks in the opposite direction.
            forward, reverse = [.95, .68, .38, .33, .28, .25], forward
        steps = reverse if name in self.outbound else forward
        path = [([steps[min(len(steps) - 1, (t - int(start)) // 2)], .68], t) for t in range(int(start), int(last) + 1, 2)]
        zones = [zone for zone, polygon in self.door_zones.get(camera, {}).items()
                 if any(polygon[0][0] < xy[0] < polygon[1][0] for xy, _ in path)]
        return {'id': name, 'camera': camera, 'label': 'person', 'false_positive': False,
                'start_time': start, 'end_time': end if end <= self.now else None,
                'has_snapshot': last >= start + 2, 'has_clip': end <= self.now,
                'zones': zones,
                'data': {'top_score': .95, 'path_data': path,
                         'snapshot_frame_time': min(start + 3, last), 'box': [.32, .05, .36, .90]}}

    def photo(self, name, fmt):
        camera, _, _, outfit = self.tracks[name]
        key = (camera, outfit, fmt)
        if key not in self.rendered:
            shift = CAMERAS.index(camera) % 4
            image = Image.new('RGB', (240, 240), '#aaa79d')
            draw = ImageDraw.Draw(image)
            upper = (190-shift, 51+shift, 35) if outfit == 'red' else (31, 175-shift, 74)
            pants = (39, 68+shift, 165) if outfit == 'red' else (199, 145+shift, 46)
            draw.ellipse((101, 16, 139, 57), fill='#aa816d')
            draw.rectangle((78, 61, 164, 128), fill=upper)
            draw.rectangle((82, 128, 158, 203), fill=pants)
            data = BytesIO()
            if fmt == 'JPEG':
                # The source is 240x240, smaller than the Frigate minimum crop (300).
                # crop=1 thus includes the whole source, then scales to the requested 320px.
                image = image.resize((320, 320), Image.Resampling.BOX)
            image.save(data, format=fmt)
            self.rendered[key] = data.getvalue()
        return self.rendered[key]

    def request(self, req):
        assert req.method == 'GET'
        path = req.url.path
        if path == '/api/config':
            return httpx.Response(200, json=self.config)
        if path == '/api/stats':
            stats = dict(camera_fps=5., process_fps=5., detection_fps=5., skipped_fps=0., detection_enabled=True)
            return httpx.Response(200, json={'service': {'last_updated': self.now},
                                              'detectors': {'cpu': {'pid': 1234, 'detection_start': 0}},
                                              'cameras': {c: stats for c in CAMERAS}})
        if path == '/api/events':
            a, b = float(req.url.params['after']), float(req.url.params['before'])
            return httpx.Response(200, json=[self.event(name) for name, (_, start, _, _) in self.tracks.items()
                                              if a <= start <= min(self.now, b)])
        assert path.startswith('/api/events/'), path
        name, _, media = path.removeprefix('/api/events/').partition('/')
        event = self.event(name)
        assert event['start_time'] <= self.now
        if not media:
            return httpx.Response(200, json=event)
        if media == 'snapshot.jpg':
            if event['end_time'] is not None:
                return httpx.Response(200, content=self.photo(name, 'JPEG'),
                                      headers={'Content-Type': 'image/jpeg', 'Cache-Control': 'private, max-age=31536000'})
            assert req.url.params['crop'] == '1'
            self.snapshots.add((name, 'active'))
            return httpx.Response(200, content=self.photo(name, 'JPEG'),
                                  headers={'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store',
                                           'X-Frame-Time': str(event['data']['snapshot_frame_time'])})
        if media == 'snapshot-clean.webp' and event['end_time'] is not None:
            self.snapshots.add((name, 'ended'))
            return httpx.Response(200, content=self.photo(name, 'WEBP'),
                                  headers={'Content-Type': 'image/webp',
                                           'X-Frame-Time': str(event['data']['snapshot_frame_time'])})
        return httpx.Response(404)

    def use(self, engine):
        self.engine = engine
        asyncio.run(engine.client.aclose())
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(self.request))

    def until(self, target):
        async def poll():
            for at in range(int(self.now - self.base) + 1, target + 1):
                if at % 3 and at != target:
                    continue
                self.now = self.base + at
                await self.engine.sync_camera_signals()
                await self.engine.sync()
                await self.engine.sync_retail_snapshots()
        asyncio.run(poll())


@pytest.fixture
def shop(app, logged, monkeypatch, request):
    feed = VisitStoreFeed()
    two_doors = getattr(request, 'param', None) == 'two_doors'
    if two_doors:
        feed.door_zones[BACK] = {BACK_ENTRANCE: DOOR_POLYGONS[INSIDE], BACK_INSIDE: DOOR_POLYGONS[ENTRANCE]}
    monkeypatch.setattr(time, 'time', lambda: feed.now)
    site = logged.post('/api/platform/sites', json={'organization_id': 'default', 'name': 'Тестовый магазин',
                                                    'profile': 'retail', 'modules': ['person']}).json()['id']
    cameras = []
    for camera in CAMERAS:
        cam = {'id': camera, 'name': camera, 'site_id': site, 'stream': 'rtsp://camera.test/'+camera,
               'analyze': False, 'zones': [{'name': zone, 'restricted': False, 'points': poly}
                                           for zone, poly in feed.door_zones.get(camera, {}).items()]}
        response = logged.post('/api/cameras', json=cam)
        assert response.status_code == 201, response.text
        cameras.append(cam)
    feed.config = make_config(cameras)
    for camera in feed.config['cameras'].values():
        camera['detect'].update(width=240, height=240)  # Effective Frigate configuration.
    graph = {'entrance_camera': CAMERAS[0], 'entrance_zone': ENTRANCE, 'inside_zone': INSIDE, 'camera_ids': list(CAMERAS),
             'transitions': [{'from_camera': a, 'to_camera': b, 'min_seconds': 1, 'max_seconds': 20}
                             for a, b in zip(CAMERAS, CAMERAS[1:])]}
    if two_doors:
        graph = {'camera_ids': list(CAMERAS), 'entrances': [
            {'camera': CAMERAS[0], 'entrance_zone': ENTRANCE, 'inside_zone': INSIDE},
            {'camera': BACK, 'entrance_zone': BACK_ENTRANCE, 'inside_zone': BACK_INSIDE}],
            'transitions': [*graph['transitions'],
                            {'from_camera': BACK, 'to_camera': CAMERAS[1], 'min_seconds': 1, 'max_seconds': 20}]}
    url = '/api/retail/visits/' + site
    response = logged.put(url + '/config', json=graph)
    assert response.is_success, response.text
    assert len(response.json()['config']['camera_ids']) == 10
    feed.use(app.state.engine)
    return feed, url


def listing(client, url):
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()['visits']


def detail(client, url, identifier):
    response = client.get(f'{url}/{identifier}')
    assert response.status_code == 200, response.text
    return response.json()


def test_ten_camera_visit_enter_live_continue_restart_and_distinct_arrival(shop, logged, settings):
    feed, url = shop
    for i, camera in enumerate(CAMERAS):
        feed.track('track-' + camera, camera, 20 + 18*i)
    feed.until(26)
    [first] = listing(logged, url)
    visit_id = first['id']
    assert first['active_camera'] == CAMERAS[0]
    assert detail(logged, url, visit_id)['timeline'][0]['ended_at'] is None
    for i in range(1, 4):
        feed.until(26 + 18*i)
        [item] = listing(logged, url)
        assert (item['id'], item['active_camera']) == (visit_id, CAMERAS[i])
        timeline = detail(logged, url, visit_id)['timeline']
        assert [leg['camera'] for leg in timeline] == list(CAMERAS[:i+1])
        assert timeline[-1]['ended_at'] is None

    # Recreate Glazza on the same database while the persisted dairy observation is open.
    restarted = create_app(settings, background=False)
    with TestClient(restarted) as client:
        login = client.post('/api/login', json={'username': settings.username, 'password': settings.password})
        assert login.is_success
        feed.use(restarted.state.engine)
        assert detail(client, url, visit_id)['timeline'][-1]['camera'] == 'dairy'
        for i in range(4, 10):
            feed.until(26 + 18*i)
            [item] = listing(client, url)
            assert (item['id'], item['active_camera']) == (visit_id, CAMERAS[i])
        timeline = detail(client, url, visit_id)['timeline']
        assert [leg['camera'] for leg in timeline] == list(CAMERAS)
        assert len({leg['origin_event'] for leg in timeline}) == 10
        assert [leg['role'] for leg in timeline] == ['entrance'] + ['observation'] * 9
        for leg in timeline:
            source = client.get('/api/events/' + leg['origin_event'])
            assert source.is_success
            assert (source.json()['source'], source.json()['camera']) == ('frigate', leg['camera'])
            assert len(leg['path']) >= 2
            times = [point[1] for point in leg['path']]
            assert times == sorted(set(times))
        assert timeline[-1]['ended_at'] is None
        assert all(('track-' + camera, 'active') in feed.snapshots for camera in CAMERAS[1:])

        feed.track('new-entrance', CAMERAS[0], 201, outfit='green')
        feed.until(207)
        people = listing(client, url)
        assert len(people) == 2
        [other] = [p for p in people if p['id'] != visit_id]
        assert other['active_camera'] == CAMERAS[0]
        assert [leg['camera'] for leg in detail(client, url, other['id'])['timeline']] == [CAMERAS[0]]
        assert [leg['camera'] for leg in detail(client, url, visit_id)['timeline']] == list(CAMERAS)


def test_two_plausible_visitors_never_bind_an_unidentified_aisle_track(shop, logged):
    feed, url = shop
    feed.track('first-arrival', CAMERAS[0], 20)
    feed.track('second-arrival', CAMERAS[0], 22)
    feed.track('indistinguishable-track', CAMERAS[1], 39)
    feed.until(27)
    arrivals = listing(logged, url)
    assert len({v['id'] for v in arrivals}) == 2
    feed.until(60)
    people = listing(logged, url)
    assert {v['id'] for v in people} == {v['id'] for v in arrivals}
    for visit in people:
        assert [leg['camera'] for leg in detail(logged, url, visit['id'])['timeline']] == [CAMERAS[0]]
    assert ('indistinguishable-track', 'active') in feed.snapshots


def test_outgoing_track_crossing_entrance_does_not_create_new_visit(shop, logged):
    feed, url = shop
    feed.track('leaving-store', CAMERAS[0], 20)
    feed.outbound.add('leaving-store')
    feed.until(36)
    assert listing(logged, url) == []


@pytest.mark.parametrize('shop', ['two_doors'], indirect=True)
@pytest.mark.parametrize('same_outfit', [False, True], ids=['distinct-at-convergence', 'ambiguous-at-convergence'])
def test_simultaneous_arrivals_from_two_doors_converge_without_exchanging_ids(shop, logged, same_outfit):
    feed, url = shop
    second_outfit = 'red' if same_outfit else 'green'
    feed.track('front-100', CAMERAS[0], 20)
    feed.track('back-200', BACK, 20, outfit=second_outfit)
    # Deliberately reverse the arrival order in the downstream camera feed.
    feed.track('aisle-412', CAMERAS[1], 39, outfit=second_outfit)
    feed.track('aisle-895', CAMERAS[1], 39)
    feed.track('bread-337', CAMERAS[2], 57)
    feed.track('bread-674', CAMERAS[2], 57, outfit=second_outfit)
    feed.until(26)
    arrivals = listing(logged, url)
    assert len(arrivals) == 2
    by_door = {item['active_camera']: item['id'] for item in arrivals}
    assert set(by_door) == {CAMERAS[0], BACK} and len(set(by_door.values())) == 2
    expected = {
        CAMERAS[0]: ['front-100', 'aisle-895', 'bread-337'],
        BACK: ['back-200', 'aisle-412', 'bread-674'],
    }
    for count, at in enumerate([45, 63], start=2):
        feed.until(at)
        current = {v['id']: v for v in listing(logged, url)}
        assert set(current) == set(by_door.values())
        for door, identifier in by_door.items():
            legs = detail(logged, url, identifier)['timeline']
            assert legs[0]['zone'] == (BACK_ENTRANCE if door == BACK else ENTRANCE)
            origins = [logged.get('/api/events/' + leg['origin_event']).json()['source_id'] for leg in legs]
            assert [leg['role'] for leg in legs] == ['entrance'] + ['observation'] * (len(legs)-1)
            if same_outfit:
                assert origins == expected[door][:1]
                assert current[identifier]['active_camera'] is None
            else:
                assert origins == expected[door][:count]
                assert current[identifier]['active_camera'] == CAMERAS[count-1]
                assert legs[-1]['ended_at'] is None
    assert all((name, 'active') in feed.snapshots for name in feed.tracks)


@pytest.mark.parametrize('shop', ['two_doors'], indirect=True)
def test_reverse_traffic_at_secondary_door_does_not_seed_visit(shop, logged):
    feed, url = shop
    feed.track('out-back', BACK, 20)
    feed.outbound.add('out-back')
    feed.track('in-front', CAMERAS[0], 20, outfit='green')
    feed.until(36)
    [only] = listing(logged, url)
    [entry] = detail(logged, url, only['id'])['timeline']
    assert logged.get('/api/events/' + entry['origin_event']).json()['source_id'] == 'in-front'
