"""State-machine regressions; test_visits_store_flow.py exercises the HTTP + real crop path."""

import itertools
import time

import pytest

from app import visits


def rectangle(name, x):
    return {'name': name, 'restricted': False, 'points': [[x, 0.1], [x + 0.2, 0.1], [x + 0.2, 0.9], [x, 0.9]]}


@pytest.fixture
def flow(app, logged, monkeypatch, request):
    store = app.state.store
    start = int(time.time())
    site = logged.post(
        '/api/platform/sites', json={'name': 'Visits', 'profile': 'retail', 'organization_id': 'default'}
    ).json()['id']
    mode = getattr(request, 'param', 'single')
    cameras = ['door_cam', 'aisle_cam', 'next_cam'] if mode == 'single' else ['door_cam', 'side_cam']
    if mode == 'converging':
        cameras.append('aisle_cam')
    door_zones = {
        'door_cam': [rectangle('door', 0.1), rectangle('inside', 0.5)],
        'side_cam': [rectangle('side_door', 0.1), rectangle('side_inside', 0.5)],
    }
    for c in cameras:
        zones = door_zones.get(c, [])
        assert logged.post(
            '/api/cameras',
            json={'id': c, 'name': c, 'site_id': site, 'zones': zones, 'stream': 'rtsp://demo/' + c},
        ).is_success
    monkeypatch.setattr(time, 'time', lambda: start)
    rule = {
        'entrance_camera': 'door_cam',
        'entrance_zone': 'door',
        'inside_zone': 'inside',
        'camera_ids': cameras,
        'transitions': [
            {'from_camera': a, 'to_camera': b, 'min_seconds': 1, 'max_seconds': 30}
            for a, b in itertools.pairwise(cameras)
        ],
    }
    if mode != 'single':
        for field in ('entrance_camera', 'entrance_zone', 'inside_zone'):
            del rule[field]
        rule['entrances'] = [
            {'camera': 'door_cam', 'entrance_zone': 'door', 'inside_zone': 'inside'},
            {'camera': 'side_cam', 'entrance_zone': 'side_door', 'inside_zone': 'side_inside'},
        ]
        if mode == 'independent':
            rule['transitions'] = []
        else:
            rule['transitions'].append(
                {'from_camera': 'door_cam', 'to_camera': 'aisle_cam', 'min_seconds': 1, 'max_seconds': 30}
            )
    resp = logged.put('/api/retail/visits/' + site + '/config', json=rule)
    assert resp.is_success, resp.text
    camera_by_id = {c['id']: c for c in store.cameras()}
    for step in range(0, 121, 5):
        for c in cameras:
            visits.record_health(store, c, start + step, True)
    now = start + 60
    monkeypatch.setattr(time, 'time', lambda: now)

    def ingest(name, camera, begin, seen, end=None, zones=(), points=None, missing_snapshot_time=False):
        record = {
            'id': name,
            'label': 'person',
            'camera': camera,
            'start_time': start + begin,
            'end_time': None if end is None else start + end,
            'zones': list(zones),
            'has_clip': end is not None,
            'data': {
                'frame_time': start + seen,
                'snapshot_frame_time': start + seen,
                'box': [0.3, 0.05, 0.4, 0.9],
            },
        }
        if missing_snapshot_time:
            record['data']['snapshot_frame_time'] = None
        if points is not None:
            record['data']['path_data'] = [[[x, 0.5], start + t] for x, t in points]
        visits.observe(store, camera_by_id[camera], record, name, site)

    def assign(time_offset=60):
        # Distinct upper/lower, both signature bands have exactly 100%.
        signature = [100, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100, 0, 0, 0, 0, 0, 0]
        for job in visits.snapshot_jobs(store, start + time_offset, 100):
            visits.snapshot_result(store, job, signature, start + time_offset)
        visits.advance(store, start + time_offset)

    return store, site, start, ingest, assign


def test_second_destination_retracts_provisional_link_and_all_descendants(flow):
    store, site, start, ingest, assign = flow
    ingest('entry', 'door_cam', 2, 10, 10, ['door', 'inside'], [(0.2, 4), (0.6, 7)])
    ingest('candidate-a', 'aisle_cam', 16, 22, 22)
    ingest('third-camera', 'next_cam', 31, 32)
    assign()
    (row,) = visits.list_visits(store, site, start + 34)
    assert row['observations'] == 3 and row['active_camera'] == 'next_cam'
    # A late second track in the SAME destination is equally compatible with the one door visit.
    ingest('candidate-b', 'aisle_cam', 18, 23, 23)
    assign()
    (same,) = visits.list_visits(store, site, start + 34)
    assert same['id'] == row['id'] and same['status'] == 'uncertain' and same['active_camera'] is None
    detail = visits.get_visit(store, site, same['id'], start + 34)
    assert [leg['origin_event'] for leg in detail['timeline']] == ['entry']
    assert detail['timeline'][0]['path'] == [[[0.2, 0.5], start + 4], [[0.6, 0.5], start + 7]]


def test_cumulative_zones_require_two_distinct_captures_and_direction(flow):
    store, site, start, ingest, _ = flow
    ingest('ambiguous', 'door_cam', 2, 5, zones=['inside', 'door'])
    ingest('ambiguous', 'door_cam', 2, 7, zones=['door', 'inside'])
    assert visits.list_visits(store, site, start + 7) == []
    ingest('reverse', 'door_cam', 2, 5, zones=['inside'])
    ingest('reverse', 'door_cam', 2, 10, zones=['door', 'inside'])
    assert visits.list_visits(store, site, start + 10) == []
    ingest('entry', 'door_cam', 12, 13, zones=['door'])
    ingest('entry', 'door_cam', 12, 13, zones=['door', 'inside'])
    assert visits.list_visits(store, site, start + 13) == []
    ingest('entry', 'door_cam', 12, 16, zones=['door', 'inside'])
    (row,) = visits.list_visits(store, site, start + 16)
    assert row['started_at'] == start + 16 and row['status'] == 'observed'
    assert visits.get_visit(store, 'wrong-site', row['id'], start + 16) is None


def test_empty_snap_time_still_queues_live_track_and_stale_response_is_ignored(flow):
    store, _site, start, ingest, _ = flow
    ingest(
        'entry',
        'door_cam',
        2,
        6,
        zones=['door', 'inside'],
        points=[(0.2, 3), (0.6, 5)],
        missing_snapshot_time=True,
    )
    (row,) = visits.snapshot_jobs(store, start + 7, 3)
    assert row['snapshot_at'] is None and row['started'] == start + 2
    ingest('entry', 'door_cam', 2, 8, 8, zones=['door', 'inside'], points=[(0.2, 3), (0.6, 5)])
    visits.snapshot_result(store, row, [100] + [0] * 14 + [100] + [0] * 6, start + 10)
    (final,) = visits.snapshot_jobs(store, start + 10, 3)
    assert final['ended'] == start + 8 and final['started'] == start + 2


def test_stale_visit_has_no_current_location_and_expires_from_active_list(flow):
    store, site, start, ingest, _ = flow
    ingest('entry', 'door_cam', 2, 6, zones=['door', 'inside'], points=[(0.2, 3), (0.6, 5)])
    (row,) = visits.list_visits(store, site, start + 30)
    assert row['active_camera'] is None and row['status'] == 'uncertain'
    boundary = start + 6 + visits.IDLE_SECONDS
    assert visits.list_visits(store, site, boundary, active_only=True) == []
    assert visits.get_visit(store, site, row['id'], boundary)['status'] == 'expired'
    visits.cleanup(store, start + visits.HISTORY_SECONDS + 20)
    assert visits.get_visit(store, site, row['id'], start + visits.HISTORY_SECONDS + 20) is None


def test_invalidated_ancestor_immediately_revokes_downstream_camera(flow):
    store, site, start, ingest, assign = flow
    ingest('entry', 'door_cam', 2, 10, 10, ['door', 'inside'], [(0.2, 4), (0.6, 7)])
    ingest('middle', 'aisle_cam', 16, 22, 22)
    ingest('downstream', 'next_cam', 31, 33)
    assign()
    (visit,) = visits.list_visits(store, site, start + 34)
    assert visit['active_camera'] == 'next_cam'
    visits.invalidate_track(store, 'aisle_cam', 'middle', start + 16, start + 34, site)
    revoked = visits.get_visit(store, site, visit['id'], start + 34)
    assert revoked['active_camera'] is None and revoked['status'] == 'uncertain'
    assert [t['origin_event'] for t in revoked['timeline']] == ['entry']
    assign()
    assert visits.get_visit(store, site, visit['id'], start + 34)['observations'] == 1


def test_incremental_path_keeps_only_recent_reported_points(flow):
    store, site, start, ingest, _ = flow
    ingest('entry', 'door_cam', 2, 7, zones=['door', 'inside'], points=[(0.2, 4), (0.6, 7)])
    (row,) = visits.list_visits(store, site, start + 7)
    for at in range(8, 48):
        ingest('entry', 'door_cam', 2, at, zones=['door', 'inside'], points=[(0.65, at)])
    timeline = visits.get_visit(store, site, row['id'], start + 47)['timeline']
    assert timeline[0]['path'] == [[[0.65, 0.5], start + at] for at in range(16, 48)]


def test_frigate_telemetry_jitter_and_one_missed_report():
    assert visits._healthy([True, True], [100, 117], 103, 119)
    assert not visits._healthy([True, True], [100, 134], 103, 135)
    assert not visits._healthy([True, False], [100, 117], 103, 119)


@pytest.mark.parametrize('flow', ['independent'], indirect=True)
def test_two_independent_doors_keep_stable_ids_without_camera_transitions(flow):
    store, site, start, ingest, assign = flow
    north = ('north-arrival', 'door_cam', 2, 9, None, ['door', 'inside'], [(0.2, 4), (0.6, 7)])
    east = ('east-arrival', 'side_cam', 2, 9, None, ['side_inside', 'side_door'], [(0.2, 4), (0.6, 7)])
    for args in (east, north):
        ingest(*args)
    assert visits.snapshot_jobs(store, start + 9, 20) == []
    assign(9)
    results = visits.list_visits(store, site, start + 9)
    assert sorted((v['entrance_camera'], v['entrance_zone'], v['active_camera']) for v in results) == [
        ('door_cam', 'door', 'door_cam'),
        ('side_cam', 'side_door', 'side_cam'),
    ]
    ids = {v['id'] for v in results}
    assert len(ids) == 2
    assert visits.configured_zones(store) == {
        'door_cam': {'door', 'inside'},
        'side_cam': {'side_door', 'side_inside'},
    }
    # Replaying the input or seeing a customer walk out by the other door does not add a visit.
    for args in (north, east):
        ingest(*args)
    ingest('east-outbound', 'side_cam', 10, 19, 19, ['side_door', 'side_inside'], [(0.6, 11), (0.2, 17)])
    ingest('wrong-zones', 'side_cam', 21, 25, 25, ['door', 'inside'], [(0.2, 22), (0.6, 24)])
    assign(27)
    assert {v['id'] for v in visits.list_visits(store, site, start + 27)} == ids


@pytest.mark.parametrize('flow', ['converging'], indirect=True)
def test_arrival_through_other_door_starts_own_visit_and_retracts_ambiguous_link(flow):
    store, site, start, ingest, assign = flow
    north = ('north-in', 'door_cam', 2, 10, 10, ['door', 'inside'], [(0.2, 3), (0.6, 6)])
    east = ('east-in', 'side_cam', 3, 11, 11, ['side_door', 'side_inside'], [(0.2, 5), (0.6, 7)])
    ingest(*north)
    ingest('middle', 'aisle_cam', 16, 18)
    assign(18)
    (prior,) = visits.list_visits(store, site, start + 18)
    assert prior['active_camera'] == 'aisle_cam' and prior['entrance_camera'] == 'door_cam'
    # A late arrival record from the independent door is a second plausible origin for the aisle.
    ingest(*east)
    assign(18)
    results = visits.list_visits(store, site, start + 18)
    assert len(results) == 2 and len({v['id'] for v in results}) == 2
    assert {r['entrance_camera'] for r in results} == {'door_cam', 'side_cam'}
    assert prior['id'] in {r['id'] for r in results}
    for r in results:
        assert r['active_camera'] is None and r['status'] == 'uncertain'
        timeline = visits.get_visit(store, site, r['id'], start + 18)['timeline']
        assert [t['role'] for t in timeline] == ['entrance']


@pytest.mark.parametrize('flow', ['converging'], indirect=True)
@pytest.mark.parametrize('inward', [True, False], ids=['new-entry', 'outbound-observation'])
def test_another_door_view_waits_for_direction_before_handing_off(flow, inward):
    store, site, start, ingest, assign = flow
    ingest('first', 'door_cam', 2, 10, 10, ['door', 'inside'], [(0.2, 3), (0.6, 7)])
    ingest('at-side', 'side_cam', 17, 18)
    assign(18)
    (original,) = visits.list_visits(store, site, start + 18)
    assert original['active_camera'] is None and original['observations'] == 1
    points = [(0.2, 20), (0.6, 24)] if inward else [(0.6, 20), (0.2, 24)]
    ingest('at-side', 'side_cam', 17, 24, zones=['side_door', 'side_inside'], points=points)
    assign(24)
    results = visits.list_visits(store, site, start + 24)
    (visible,) = [r for r in results if r['active_camera'] == 'side_cam']
    if inward:
        assert len(results) == 2 and visible['id'] != original['id']
        assert visible['entrance_camera'] == 'side_cam' and visible['observations'] == 1
    else:
        assert len(results) == 1 and visible['id'] == original['id']
        timeline = visits.get_visit(store, site, original['id'], start + 24)['timeline']
        assert [(t['camera'], t['role']) for t in timeline] == [
            ('door_cam', 'entrance'),
            ('side_cam', 'observation'),
        ]


def test_repolling_two_cumulative_zones_without_order_does_not_prove_entry(flow):
    store, site, start, ingest, _ = flow
    ingest('incomplete', 'door_cam', 2, 7, zones=['door', 'inside'], points=[(0.2, 5)])
    ingest('incomplete', 'door_cam', 2, 10, zones=['door', 'inside'], points=[(0.21, 8)])
    assert visits.list_visits(store, site, start + 10) == []
    # The interior point arrives later in a partial path; retained capture times now establish direction.
    ingest('incomplete', 'door_cam', 2, 12, zones=['door', 'inside'], points=[(0.6, 12)])
    (row,) = visits.list_visits(store, site, start + 12)
    assert row['started_at'] == start + 12


def test_contradicted_interior_sample_remains_unresolved_after_display_history_rolls_off(flow):
    store, site, start, ingest, assign = flow
    ingest('contradiction', 'door_cam', 2, 9, zones=['door'])
    # Interior point at t=5 contradicts the claim that only the door had been
    # visited by t=9. No geometric door observation precedes that point.
    ingest('contradiction', 'door_cam', 2, 11, zones=['door', 'inside'], points=[(0.6, 5)])
    for at in range(12, 50):
        ingest('contradiction', 'door_cam', 2, at, zones=['door', 'inside'], points=[(0.21, at)])
    ingest('contradiction', 'door_cam', 2, 50, zones=['door', 'inside'], points=[(0.61, 50)])
    assign()
    assert visits.list_visits(store, site, start + 50) == []
