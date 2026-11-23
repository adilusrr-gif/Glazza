import json
import subprocess
import sys

import pytest

from scripts.evaluate_routes import field_report


@pytest.fixture
def route_period(app):
    store = app.state.store

    def card(key, left, right, source='retail_route', camera='aisle', site='shop'):
        return store.event(source_id=key, source=source, camera=camera, site_id=site, kind='theft',
                           score=None, started=1000+left, ended=1000+right)

    first = card('g1', 4, 8)
    duplicate = card('g2', 5, 9)
    gap = card('g3', 42, 46)
    card('other-model', 4, 8, source='analysis')
    card('other-camera', 4, 8, camera='door')
    card('other-site', 4, 8, site='shop-2')
    card('next-period', 110, 120)
    # Queue escalation and independent quality review are intentionally different.
    store.execute("UPDATE events SET status='confirmed' WHERE id=?", (duplicate,))
    period = dict(site_id='shop', camera='aisle', start=1000, end=1120, reference_complete=True,
                  gaps=[[40, 50]], events=[{'id':'A','span':[3, 9]}, {'id':'B','span':[65, 73]},
                                           {'id':'lost','span':[41, 48]}],
                  reviews={first:{'reference_id':'A','operator_seconds':12},
                           duplicate:{'reference_id':None,'operator_seconds':8}})
    return store, period, first, duplicate, gap


def test_real_snapshot_cli_separates_source_scope_outages_and_operational_status(route_period, tmp_path):
    store, period, first, duplicate, gap = route_period
    reference = tmp_path / 'reference.json'
    reference.write_text(json.dumps({'periods':[period]}))
    result = subprocess.run([sys.executable,'-m','scripts.evaluate_routes',str(store.path),str(reference)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['reconciled']
    p = report['periods'][0]
    assert [c['id'] for c in p['cards']] == [first, duplicate, gap]
    assert not p['cards'][2]['observable']
    assert p['missed_reference_ids'] == ['B'] and p['unobservable_reference_ids'] == ['lost']
    assert report['precision'] == report['recall'] == .5
    assert report['false_alerts_per_observable_camera_hour'] == pytest.approx(3600/110)
    assert report['measured_operator_seconds'] == 20 and report['timed_cards'] == 2
    assert report['escalated_cards'] == 1
    del period['reviews'][first]
    reference.write_text(json.dumps({'periods':[period]}))
    pending = subprocess.run([sys.executable,'-m','scripts.evaluate_routes',str(store.path),str(reference)],
                             capture_output=True, text=True)
    assert pending.returncode == 1
    report = json.loads(pending.stdout)
    assert report['precision'] is report['recall'] is report['false_alerts_per_observable_camera_hour'] is None
    assert report['periods'][0]['pending_card_ids'] == [first]


@pytest.mark.parametrize('problem', ['unknown_card','wrong_time','duplicate_match','overlapping_periods','unknown_camera'])
def test_reconciliation_rejects_unreliable_matches(route_period, problem):
    store, p, first, duplicate, _ = route_period
    periods = [p]
    if problem == 'unknown_card':
        p['reviews']['unrelated'] = {'reference_id':None}
    elif problem == 'wrong_time':
        p['reviews'][first]['reference_id'] = 'B'
    elif problem == 'duplicate_match':
        p['reviews'][duplicate]['reference_id'] = 'A'
    elif problem == 'overlapping_periods':
        periods.append(p)
    else:
        p['camera'] = 'missing'
    with store.connect() as db, pytest.raises(ValueError):
        field_report(db, periods)


def test_no_precision_from_incomplete_reference_even_if_all_cards_examined(route_period):
    store, period, *_ = route_period
    period['reference_complete'] = False
    with store.connect() as db:
        report = field_report(db, [period])
    assert not report['reconciled'] and report['precision'] is report['missed'] is None
