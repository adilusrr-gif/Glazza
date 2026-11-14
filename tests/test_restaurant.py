"""Lifecycle, durable deadlines, delivery and scoped staff capabilities."""
import asyncio
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from app.restaurant import RestaurantService, StaffInput, TableAction, TableInput


@pytest.fixture
def restaurant(logged, app, camera, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr('app.restaurant._at', lambda now=None: clock[0] if now is None else float(now))
    store = app.state.store
    store.execute("UPDATE sites SET modules=? WHERE id='default'", (json.dumps(['person', 'restaurant']),))
    camera['zones'] = []
    assert logged.post('/api/cameras', json=camera).status_code == 201
    staff = {}
    for role in ['waiter', 'manager', 'cleaner']:
        body = {'site_id': 'default', 'name': role, 'role': role, 'telegram_chat_id': str(100 + len(staff))}
        result = logged.post('/api/restaurant/staff', json=body)
        assert result.status_code == 201, result.text
        staff[role] = {**body, **result.json()}
    body = {'site_id': 'default', 'camera': camera['id'], 'name': 'Стол 7',
            'points': [[.1, .1], [.5, .1], [.5, .5], [.1, .5]],
            'waiter_id': staff['waiter']['id'], 'cleaner_id': staff['cleaner']['id'],
            'wait_seconds': 120, 'arrival_seconds': 5, 'vacancy_seconds': 30}
    result = logged.post('/api/restaurant/tables', json=body)
    assert result.status_code == 201, result.text
    service = app.state.engine.restaurant
    return {'service': service, 'store': store, 'clock': clock, 'staff': staff,
            'table_id': result.json()['id'], 'zone': result.json()['zone'], 'body': body,
            'client': logged, 'camera': camera['id']}


def table(env):
    return next(t for t in env['service'].snapshot()['tables'] if t['id'] == env['table_id'])


def observe(env, count, delta=0, available=True):
    env['clock'][0] += delta
    env['service'].observe(env['camera'], {env['zone']: count}, available=available)


def hold_count(env, count, seconds):
    for _ in range(seconds):
        observe(env, count, 1)
        env['service'].tick()


def seat(env):
    observe(env, 2)
    hold_count(env, 2, 5)
    return table(env)


def action(env, name, **override):
    current = table(env)
    body = {'action': name, 'visit_id': current['visit_id'], 'revision': current['revision'], **override}
    return env['client'].post('/api/restaurant/tables/' + current['id'] + '/actions', json=body)


def portal_headers(env, role):
    return {'Authorization': 'Bearer ' + env['staff'][role]['token']}


def test_initial_unknown_never_claims_clean_table(restaurant):
    env = restaurant
    assert table(env)['color'] == 'gray'
    assert table(env)['state'] == 'needs_cleaning'
    assert action(env, 'cleaned').status_code == 409
    observe(env, 0)
    assert table(env)['color'] == 'yellow'
    assert action(env, 'cleaned').status_code == 200
    assert table(env)['color'] == 'green'
    observe(env, 1)
    assert table(env)['color'] == 'gray'  # arrival debounce must not advertise a free table


def test_seated_once_and_exact_120_second_escalation(restaurant):
    env = restaurant
    current = seat(env)
    assert current['state'] == 'waiting' and current['color'] == 'red'
    assert current['arrived_at'] == 10000
    assert current['deadline_at'] == 10120
    for _ in range(3):
        observe(env, 2)
    initial = env['service'].snapshot()['alerts']
    assert len(initial) == 1 and initial[0]['kind'] == 'arrival'
    assert initial[0]['recipient_id'] == env['staff']['waiter']['id']
    env['clock'][0] = 10119.999
    observe(env, 2)
    env['service'].tick()
    assert not any(a['kind'] == 'escalation' for a in env['service'].snapshot()['alerts'])
    env['clock'][0] = 10120
    env['service'].tick()
    env['service'].tick()
    alerts = env['service'].snapshot()['alerts']
    escalation = [a for a in alerts if a['kind'] == 'escalation']
    assert len(escalation) == 1
    assert escalation[0]['recipient_id'] == env['staff']['manager']['id']
    assert len(env['store'].rows('SELECT * FROM restaurant_visits')) == 1


def test_acknowledgement_is_not_greeting(restaurant):
    env = restaurant
    seat(env)
    alert = env['service'].snapshot()['alerts'][0]
    result = env['client'].post('/api/restaurant/portal/alerts/' + alert['id'] + '/ack', headers=portal_headers(env, 'waiter'))
    assert result.status_code == 200
    assert table(env)['greeted_at'] is None
    env['clock'][0] = 10120
    env['service'].tick()
    assert any(a['kind'] == 'escalation' for a in env['service'].snapshot()['alerts'])


def test_greeting_keeps_table_red_and_stops_first_contact_timer(restaurant):
    env = restaurant
    seat(env)
    env['clock'][0] += 3
    assert action(env, 'greeted').status_code == 200
    first = table(env)['greeted_at']
    assert table(env)['state'] == 'served'
    assert table(env)['color'] == 'red'
    assert action(env, 'greeted').status_code == 200
    assert table(env)['greeted_at'] == first
    env['clock'][0] = 10200
    observe(env, 2)
    env['service'].tick()
    assert not any(a['kind'] == 'escalation' for a in env['service'].snapshot()['alerts'])


def test_restart_preserves_deadline_and_logical_alerts(restaurant, settings):
    env = restaurant
    original = seat(env)
    env['clock'][0] = 10090
    service = RestaurantService(env['store'], settings)
    env['service'] = service
    restored = table(env)
    assert restored['visit_id'] == original['visit_id']
    assert restored['deadline_at'] == 10120
    assert restored['occupancy'] == 'unknown' and restored['color'] == 'gray'
    env['clock'][0] = 10120
    service.tick()
    alerts = service.snapshot()['alerts']
    assert len([a for a in alerts if a['kind'] == 'escalation']) == 1
    assert 'недоступны' in next(a['message'] for a in alerts if a['kind'] == 'escalation')
    env['service'] = RestaurantService(env['store'], settings)
    env['service'].tick()
    assert len([a for a in env['service'].snapshot()['alerts'] if a['kind'] == 'escalation']) == 1


def test_short_absence_other_guests_and_unknown_feed_do_not_end_visit(restaurant):
    env = restaurant
    original = seat(env)
    observe(env, 1)
    hold_count(env, 1, 10)  # one guest remains
    observe(env, 0)
    hold_count(env, 0, 20)
    observe(env, 1)
    assert table(env)['visit_id'] == original['visit_id']
    observe(env, 0, available=False)
    env['clock'][0] += 90
    env['service'].tick()
    assert table(env)['state'] == 'waiting'
    assert table(env)['color'] == 'gray'
    observe(env, 0)
    hold_count(env, 0, 29)
    assert table(env)['state'] == 'waiting'
    observe(env, 0, 1)
    assert table(env)['state'] == 'needs_cleaning'
    assert table(env)['color'] == 'yellow'
    cleaning = [a for a in env['service'].snapshot()['alerts'] if a['kind'] == 'cleaning']
    assert len(cleaning) == 1 and cleaning[0]['recipient_id'] == env['staff']['cleaner']['id']


def test_hold_preserves_temporary_absence_and_resume_starts_fresh_grace(restaurant):
    env = restaurant
    seat(env)
    assert action(env, 'hold').status_code == 200
    observe(env, 0)
    hold_count(env, 0, 60)
    assert table(env)['state'] == 'waiting' and table(env)['hold']
    assert action(env, 'resume').status_code == 200
    observe(env, 0)
    hold_count(env, 0, 29)
    assert table(env)['state'] == 'waiting'
    observe(env, 0, 1)
    assert table(env)['state'] == 'needs_cleaning'


def test_stale_cleaning_task_cannot_clear_new_guests(restaurant):
    env = restaurant
    old = seat(env)
    observe(env, 0)
    hold_count(env, 0, 30)
    assert table(env)['state'] == 'needs_cleaning'
    new = seat(env)
    assert new['visit_id'] != old['visit_id']
    assert action(env, 'cleaned', visit_id=old['visit_id']).status_code == 409
    assert table(env)['state'] == 'waiting'
    assert action(env, 'cleaned').status_code == 409


def test_out_of_order_missing_and_invalid_counts_are_not_vacancy(restaurant):
    env = restaurant
    current = seat(env)
    env['service'].observe(env['camera'], {env['zone']: 0}, available=True, now=10001)
    assert table(env)['occupancy'] == 'occupied'
    for value in [None, True, -1, 1.5, '0', 1001]:
        env['service'].observe(env['camera'], {env['zone']: value}, available=True)
        assert table(env)['occupancy'] == 'unknown'
        assert table(env)['visit_id'] == current['visit_id']
    env['service'].observe(env['camera'], {}, available=True)
    assert table(env)['occupancy'] == 'unknown'


def test_worker_stall_snapshot_becomes_gray_without_tick(restaurant):
    env = restaurant
    seat(env)
    env['clock'][0] += 16
    current = table(env)
    assert current['occupancy'] == 'unknown'
    assert current['color'] == 'gray'
    assert current['state'] == 'waiting'


def test_staff_tokens_scoped_rotatable_and_no_secrets_in_snapshots(restaurant):
    env = restaurant
    seat(env)
    client = env['client']
    response = client.get('/api/restaurant/portal', headers=portal_headers(env, 'waiter'))
    assert response.status_code == 200
    portal = response.json()
    assert portal['current_staff']['role'] == 'waiter'
    assert len(portal['tables']) == 1
    assert portal['tables'][0]['allowed_actions'] == ['greeted', 'hold']
    for payload in [portal, client.get('/api/restaurant').json()]:
        text = json.dumps(payload)
        for employee in env['staff'].values():
            assert employee['token'] not in text
            assert hashlib.sha256(employee['token'].encode()).hexdigest() not in text
    # Bearer credentials never grant admin APIs, even if they belong to a manager.
    cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.get('/api/restaurant', headers=portal_headers(env, 'manager')).status_code == 401
    client.cookies.update(cookies)
    rotated = client.post('/api/restaurant/staff/' + env['staff']['waiter']['id'] + '/token').json()
    assert client.get('/api/restaurant/portal', headers=portal_headers(env, 'waiter')).status_code == 401
    assert client.get('/api/restaurant/portal', headers={'Authorization': 'Bearer ' + rotated['token']}).status_code == 200


def test_waiter_cannot_see_or_act_on_other_waiters_table_or_alert(restaurant):
    env = restaurant
    other = env['client'].post('/api/restaurant/staff', json={'site_id': 'default', 'name': 'Другой', 'role': 'waiter'}).json()
    second = env['client'].post('/api/restaurant/tables', json={**env['body'], 'name': 'Другой стол', 'waiter_id': other['id']}).json()
    seat(env)
    own = env['client'].get('/api/restaurant/portal', headers=portal_headers(env, 'waiter')).json()
    assert [t['id'] for t in own['tables']] == [env['table_id']]
    result = env['client'].post('/api/restaurant/portal/tables/' + second['id'] + '/actions',
                                headers=portal_headers(env, 'waiter'), json={'action': 'cleaned', 'visit_id': None})
    assert result.status_code == 403
    alert = env['service'].snapshot()['alerts'][0]
    assert env['client'].post('/api/restaurant/portal/alerts/' + alert['id'] + '/ack',
                             headers={'Authorization': 'Bearer ' + other['token']}).status_code == 403
    assert env['client'].post('/api/restaurant/portal/tables/' + env['table_id'] + '/actions',
                             headers=portal_headers(env, 'cleaner'), json={'action': 'greeted', 'visit_id': table(env)['visit_id']}).status_code == 409


def test_disabled_staff_link_and_cross_site_assignment(restaurant):
    env = restaurant
    org = env['client'].get('/api/platform').json()['organizations'][0]['id']
    site = env['client'].post('/api/platform/sites', json={'name': 'Другой ресторан', 'organization_id': org, 'profile': 'restaurant'}).json()['id']
    outsider = env['client'].post('/api/restaurant/staff', json={'site_id': site, 'name': 'Чужой', 'role': 'waiter'}).json()
    assert env['client'].put('/api/restaurant/tables/' + env['table_id'], json={**env['body'], 'waiter_id': outsider['id']}).status_code == 422
    outsider_snapshot = env['client'].get('/api/restaurant/portal', headers={'Authorization': 'Bearer ' + outsider['token']}).json()
    assert outsider_snapshot['tables'] == []
    employee = env['staff']['waiter']
    assert env['client'].put('/api/restaurant/staff/' + employee['id'], json={k: employee[k] for k in ['site_id', 'name', 'role', 'telegram_chat_id']} | {'enabled': False}).status_code == 200
    assert env['client'].get('/api/restaurant/portal', headers=portal_headers(env, 'waiter')).status_code == 401
    assert any(d['kind'] == 'waiter_missing' for d in env['service'].snapshot()['diagnostics'])


def test_geometry_change_requires_new_zone_and_active_visit_guard(restaurant):
    env = restaurant
    changed = {**env['body'], 'points': [[.6, .1], [.9, .1], [.9, .5], [.6, .5]]}
    response = env['client'].put('/api/restaurant/tables/' + env['table_id'], json=changed)
    assert response.status_code == 200
    assert response.json()['zone'] != env['zone']
    assert response.json()['apply_required'] is True
    env['zone'] = response.json()['zone']
    seat(env)
    assert env['client'].put('/api/restaurant/tables/' + env['table_id'], json=env['body']).status_code == 409
    assert env['client'].delete('/api/restaurant/tables/' + env['table_id']).status_code == 409
    assert env['service'].camera_in_use(env['camera'])


def test_module_off_stops_configuration_and_alerts(restaurant):
    env = restaurant
    seat(env)
    env['store'].execute("UPDATE sites SET modules='[\"person\"]' WHERE id='default'")
    assert env['service'].configuration_tables() == []
    env['clock'][0] += 200
    env['service'].tick()
    assert not any(a['kind'] == 'escalation' for a in env['service'].snapshot()['alerts'])
    assert table(env)['color'] == 'gray'
    assert action(env, 'greeted').status_code == 409


def test_invalid_input_and_csrf_do_not_change_configuration(restaurant):
    env = restaurant
    for patch in [{'wait_seconds': 0}, {'arrival_seconds': True}, {'points': [[0, 0], [1, 1], [2, 2]]}, {'name': '   '}]:
        assert env['client'].post('/api/restaurant/tables', json={**env['body'], **patch}).status_code == 422
    assert env['client'].post('/api/restaurant/tables', json=env['body'], headers={'X-CSRF-Token': 'bad'}).status_code == 403
    assert env['client'].post('/api/restaurant/tables/' + env['table_id'] + '/actions', json={'action': 'cleaned'}).status_code == 422


def test_missing_waiter_routes_arrival_to_manager_and_keeps_diagnostic(restaurant):
    env = restaurant
    assert env['client'].put('/api/restaurant/tables/' + env['table_id'], json={**env['body'], 'waiter_id': None}).status_code == 200
    seat(env)
    alert = env['service'].snapshot()['alerts'][0]
    assert alert['recipient_id'] == env['staff']['manager']['id']
    assert 'не назначен' in alert['message']
    assert any(d['kind'] == 'waiter_missing' for d in env['service'].snapshot()['diagnostics'])


def test_telegram_outbox_retries_and_does_not_send_resolved_tasks(restaurant, settings):
    env = restaurant
    env['service'].settings = replace(settings, telegram_bot_token='test-only-token')
    seat(env)
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, json={'description': 'https://secret-provider-token/'})
        return httpx.Response(200, json={'ok': True})

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await env['service'].deliver(client)

    asyncio.run(deliver())
    alert = env['service'].snapshot()['alerts'][0]
    assert alert['delivery_state'] == 'pending'
    assert 'secret-provider-token' not in json.dumps(alert)
    asyncio.run(deliver())
    assert len(calls) == 1  # retry deadline is durable
    env['clock'][0] = alert['retry_at']
    asyncio.run(deliver())
    assert len(calls) == 2
    assert env['service'].snapshot()['alerts'][0]['delivery_state'] == 'sent'
    assert action(env, 'greeted').status_code == 200
    env['clock'][0] += 200
    env['service'].tick()
    asyncio.run(deliver())
    assert len(calls) == 2


def test_camera_guards_and_unknown_site(restaurant):
    env = restaurant
    assert env['client'].delete('/api/cameras/' + env['camera']).status_code == 409
    assert env['client'].get('/api/restaurant?site_id=missing').status_code == 404


def test_manager_replacement_inherits_unresolved_escalation(restaurant):
    env = restaurant
    seat(env)
    env['clock'][0] = 10120
    env['service'].tick()
    manager = env['staff']['manager']
    body = {key: manager[key] for key in ['site_id', 'name', 'role', 'telegram_chat_id']}
    assert env['client'].put('/api/restaurant/staff/' + manager['id'], json={**body, 'enabled': False}).status_code == 200
    unassigned = [a for a in env['service'].snapshot()['alerts'] if a['kind'] == 'escalation' and not a['resolved_at']]
    assert len(unassigned) == 1 and unassigned[0]['recipient_id'] == ''
    replacement = env['client'].post('/api/restaurant/staff', json={**body, 'name': 'Новый менеджер'}).json()
    fresh = env['client'].get('/api/restaurant/portal', headers={'Authorization': 'Bearer ' + replacement['token']}).json()
    assert any(a['kind'] == 'escalation' and not a['resolved_at'] for a in fresh['alerts'])
    assert not any(a['recipient_id'] == '' and not a['resolved_at'] for a in env['service'].snapshot()['alerts'])


def test_moved_manager_does_not_receive_old_site_pending_delivery(restaurant, settings):
    env = restaurant
    env['service'].settings = replace(settings, telegram_bot_token='test-only-token')
    seat(env)
    env['clock'][0] = 10120
    env['service'].tick()
    org = env['client'].get('/api/platform').json()['organizations'][0]['id']
    site = env['client'].post('/api/platform/sites', json={'name': 'Новый объект', 'organization_id': org, 'profile': 'restaurant'}).json()['id']
    manager = env['staff']['manager']
    body = {key: manager[key] for key in ['name', 'role', 'telegram_chat_id']}
    assert env['client'].put('/api/restaurant/staff/' + manager['id'], json={**body, 'site_id': site}).status_code == 200
    pending_manager = [a for a in env['service'].snapshot()['alerts'] if a['recipient_id'] == manager['id'] and a['delivery_state'] == 'pending']
    assert pending_manager == []
    calls = []

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(json.loads(request.content)) or httpx.Response(200, json={'ok': True}))) as client:
            await env['service'].deliver(client)

    asyncio.run(deliver())
    assert not any(call['chat_id'] == manager['telegram_chat_id'] for call in calls)
    assert any(a['kind'] == 'escalation' and a['recipient_id'] == '' and not a['resolved_at'] for a in env['service'].snapshot()['alerts'])


def test_retention_preserves_current_visit_and_cleans_completed_history(restaurant, settings):
    env = restaurant
    env['service'].settings = replace(settings, retention_days=1)
    old_visit = seat(env)['visit_id']
    observe(env, 0)
    hold_count(env, 0, 30)
    assert action(env, 'cleaned').status_code == 200
    current_visit = seat(env)['visit_id']
    env['clock'][0] += 86400 * 2
    env['service'].cleanup()
    visits = env['store'].rows('SELECT id FROM restaurant_visits')
    assert [v['id'] for v in visits] == [current_visit]
    assert not env['store'].rows('SELECT id FROM restaurant_alerts WHERE visit_id=?', (old_visit,))


def test_crashed_last_delivery_attempt_becomes_visible_failure(restaurant, settings):
    env = restaurant
    env['service'].settings = replace(settings, telegram_bot_token='test-only-token')
    seat(env)
    env['store'].execute("UPDATE restaurant_alerts SET delivery_state='sending',delivery_attempts=8,delivery_started=?", (env['clock'][0] - 61,))

    def no_ninth_send(request):
        raise AssertionError('A ninth delivery must not be attempted')

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_ninth_send)) as client:
            await env['service'].deliver(client)

    asyncio.run(deliver())
    alert = env['service'].snapshot()['alerts'][0]
    assert alert['delivery_state'] == 'failed'
    assert 'не подтверждена' in alert['delivery_error']
