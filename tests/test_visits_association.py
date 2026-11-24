"""State-machine regressions; test_visits_store_flow.py exercises the HTTP + real crop path."""

import itertools
import time

import pytest

from app import visits


def rectangle(name, x):
    return {'name': name, 'restricted': False, 'points': [[x, 0.1], [x + 0.2, 0.1], [x + 0.2, 0.9], [x, 0.9]]}


@pytest.fixture
def flow(app, logged, monkeypatch):
    store = app.state.store
    start = int(time.time())
    site = logged.post(
        '/api/platform/sites', json={'name': 'Visits', 'profile': 'retail', 'organization_id': 'default'}
    ).json()['id']
    cameras = ['door_cam', 'aisle_cam', 'next_cam']
    for c in cameras:
        zones = [rectangle('door', 0.1), rectangle('inside', 0.5)] if c == 'door_cam' else []
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
    assert logged.put('/api/retail/visits/' + site + '/config', json=rule).is_success
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
