"""Opt-in restaurant state fault tests; no production data or external messages.

Run: PYTHONPATH=. .venv/bin/python tests/stability/restaurant_state.py
Only temporary SQLite databases and child processes are used. Camera observations
and 72-hour time advancement are synthetic; SIGKILL and SQLite commits are real.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import traceback

import httpx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.restaurant import RestaurantService, StaffInput, TableAction, TableInput
from app.store import Store

DEFAULT_OUTPUT = ROOT / 'docs/audits/restaurant-stability-2026-11-17/evidence/state-results.json'
BASE = 1_800_000_000.0


def settings_at(directory, telegram=False):
    return Settings(data=Path(directory), frigate_config=Path(directory)/'config.yml',
                    frigate_url='http://unused.invalid', analyzer_url='http://unused.invalid',
                    worker_key='synthetic-state-fixture-'*3, username='synthetic-admin',
                    password='synthetic-not-used-password', demo=False, secure_cookie=False,
                    retention_days=1, telegram_bot_token='synthetic-no-network' if telegram else '')


def make_env(directory, table_count=1, telegram=False, second_site=False):
    settings = settings_at(directory, telegram)
    store = Store(settings.data)
    service = RestaurantService(store, settings)
    store.execute("UPDATE sites SET modules='[\"person\",\"restaurant\"]' WHERE id='default'")
    if second_site:
        store.execute("INSERT INTO sites VALUES('second','default','Synthetic second site','restaurant','[\"person\",\"restaurant\"]')")
    sites = ['default', 'second'] if second_site else ['default']
    staff, tables = {}, []
    for site in sites:
        camera = 'camera_' + site
        store.execute('INSERT INTO cameras(id,name,stream,site_id) VALUES(?,?,?,?)',
                      (camera, camera, 'rtsp://127.0.0.1:9/synthetic', site))
        staff[site] = {}
        for role in ['manager', 'waiter', 'cleaner']:
            body = StaffInput(site_id=site, name=role + '_' + site, role=role, telegram_chat_id='100')
            staff[site][role] = {**service.save_staff(body, 'fixture', now=BASE), 'body': body}
        for index in range(table_count):
            body = TableInput(site_id=site, camera=camera, name=f'Table {index:03d}',
                              points=[(.1,.1),(.4,.1),(.4,.4),(.1,.4)],
                              waiter_id=staff[site]['waiter']['id'], cleaner_id=staff[site]['cleaner']['id'])
            tables.append({**service.save_table(body, 'fixture', now=BASE), 'body': body})
    return {'settings': settings, 'store': store, 'service': service, 'staff': staff, 'tables': tables}


def row(env, table_id=None):
    table_id = table_id or env['tables'][0]['id']
    return env['store'].rows('SELECT * FROM restaurant_tables WHERE id=?', (table_id,))[0]


def observe(env, count, now, available=True, selected=None):
    tables = selected or env['tables']
    for camera in {t['body'].camera for t in tables}:
        counts = {t['zone']: count for t in tables if t['body'].camera == camera}
        env['service'].observe(camera, counts, available=available, now=now)


def act(env, table, action, now, staff=None, override=None):
    current = row(env, table['id'])
    body = {'action': action, 'visit_id': current['visit_id'], 'revision': current['revision']}
    body.update(override or {})
    return env['service'].action(table['id'], TableAction(**body), 'fixture', staff=staff, now=now)


def integrity(store):
    with store.connect() as db:
        result = db.execute('PRAGMA integrity_check').fetchall()
        assert [r[0] for r in result] == ['ok'], result
    duplicates = store.rows('SELECT visit_id,kind,recipient_id,count(*) AS n FROM restaurant_alerts GROUP BY visit_id,kind,recipient_id HAVING count(*)>1')
    assert not duplicates, duplicates


def child_fault(directory, operation, now, conn, final_attempt=False):
    """Suspend at a deterministic actual transaction or mocked network boundary."""
    try:
        settings = settings_at(directory, telegram=operation.startswith('delivery'))
        store = Store(settings.data)
        service = RestaurantService(store, settings)
        table = store.rows('SELECT * FROM restaurant_tables ORDER BY id')[0]
        if operation.endswith('_uncommitted'):
            original = service._notify
            def notify(db, table, kind, now, *args, **kwargs):
                original(db, table, kind, now, *args, **kwargs)
                conn.send({'boundary': 'uncommitted_' + kind})
                while True:
                    time.sleep(1)
            service._notify = notify
        if operation.startswith('arrival'):
            service.observe(table['camera'], {table['zone']: 2}, available=True, now=now)
            service.observe(table['camera'], {table['zone']: 2}, available=True, now=now+5)
        elif operation.startswith('deadline'):
            service.tick(now=now)
        elif operation.startswith('delivery'):
            if final_attempt:
                store.execute("UPDATE restaurant_alerts SET delivery_attempts=7 WHERE delivery_state='pending'")
            async def block(request):
                conn.send({'boundary':'delivery_started'})
                await asyncio.Event().wait()
                return httpx.Response(200, json={'ok':True})
            async def send():
                async with httpx.AsyncClient(transport=httpx.MockTransport(block),trust_env=False) as client:
                    await service.deliver(client, now=now)
            asyncio.run(send())
        conn.send({'boundary':'committed','visits':len(store.rows('SELECT id FROM restaurant_visits'))})
        while True:
            time.sleep(1)
    except BaseException:
        conn.send({'error':traceback.format_exc()})


def kill_at_boundary(directory, operation, now, final_attempt=False):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=child_fault,args=(str(directory),operation,now,child,final_attempt))
    process.start()
    try:
        assert parent.poll(20), f'No fault boundary reached for {operation}'
        event=parent.recv()
        assert 'error' not in event, event.get('error')
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL, process.exitcode
        return {'operation':operation,'boundary':event['boundary'],'exit_code':process.exitcode}
    finally:
        if process.is_alive():
            process.kill();process.join(5)
        parent.close();child.close()


def crash_suite(root):
    started=time.monotonic();events=[]
    for iteration in range(6):
        directory=root/f'crash-{iteration}'
        env=make_env(directory)
        t=BASE+iteration*1000
        events.append(kill_at_boundary(directory,'arrival_uncommitted',t))
        assert not env['store'].rows('SELECT id FROM restaurant_visits')
        assert not env['store'].rows('SELECT id FROM restaurant_alerts')
        integrity(env['store'])
        events.append(kill_at_boundary(directory,'arrival_committed',t))
        visit=env['store'].rows('SELECT * FROM restaurant_visits')[0]
        assert visit['arrived_at']==t and visit['deadline_at']==t+120
        assert len(env['store'].rows('SELECT id FROM restaurant_visits'))==1
        integrity(env['store'])
        events.append(kill_at_boundary(directory,'deadline_uncommitted',t+120))
        persisted=env['store'].rows('SELECT * FROM restaurant_visits')[0]
        assert persisted['id']==visit['id'] and persisted['escalated_at'] is None
        assert not env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation'")
        integrity(env['store'])
        events.append(kill_at_boundary(directory,'deadline_committed',t+120))
        recovered=RestaurantService(env['store'],env['settings'])
        recovered.tick(now=t+125);recovered.tick(now=t+125)
        persisted=env['store'].rows('SELECT * FROM restaurant_visits')[0]
        assert persisted['id']==visit['id'] and persisted['deadline_at']==t+120 and persisted['escalated_at']==t+120
        assert len(env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation'"))==1
        assert row(env)['occupancy']=='unknown'
        integrity(env['store'])
    delivery_results=[]
    for iteration in range(4):
        directory=root/f'delivery-crash-{iteration}'
        env=make_env(directory,telegram=True)
        t=BASE+iteration*1000
        observe(env,2,t);observe(env,2,t+5)
        final_attempt=iteration==3
        events.append(kill_at_boundary(directory,'delivery',t+6,final_attempt))
        alert=env['store'].rows('SELECT * FROM restaurant_alerts')[0]
        assert alert['delivery_state']=='sending'
        assert alert['delivery_attempts']==(8 if final_attempt else 1)
        recovered=RestaurantService(env['store'],env['settings'])
        requests=[]
        async def successful(request):
            requests.append(json.loads(request.content)['chat_id'])
            return httpx.Response(200,json={'ok':True})
        async def recover():
            async with httpx.AsyncClient(transport=httpx.MockTransport(successful),trust_env=False) as client:
                await recovered.deliver(client,now=t+67)
        asyncio.run(recover())
        after=env['store'].rows('SELECT * FROM restaurant_alerts')[0]
        assert after['delivery_state']==('failed' if final_attempt else 'sent')
        assert len(requests)==(0 if final_attempt else 1)
        assert after['delivery_attempts']==(8 if final_attempt else 2)
        assert (after['delivered_at'] is None)==final_attempt
        delivery_results.append({'final_attempt':final_attempt,'state_after_restart':after['delivery_state'],
                                 'delivery_attempts':after['delivery_attempts'],'mocked_requests':len(requests)})
        integrity(env['store'])
    return {'result':'PASS','real_sigkill_count':len(events),'integrity_checks':len(events),
            'events':events,'delivery_recovery':delivery_results,'elapsed_seconds':time.monotonic()-started,
            'clock':'virtual values for deterministic boundary placement; process death and transactions are real'}


def expect_http(code, callback):
    try:
        callback()
    except HTTPException as error:
        assert error.status_code==code,(code,error.status_code,error.detail)
        return code
    raise AssertionError(f'Expected HTTPException({code})')


def concurrent_suite(root):
    env=make_env(root/'concurrency');table=env['tables'][0]
    statuses={};checks=0
    for iteration in range(25):
        t=BASE+iteration*1000
        observe(env,2,t);observe(env,2,t+5)
        before=row(env);visit=before['visit_id']
        barrier=threading.Barrier(12)
        def race(index):
            barrier.wait()
            if index==0:
                env['service'].tick(now=t+120)
                return 'tick'
            try:
                env['service'].action(table['id'],TableAction(action='greeted',visit_id=visit,revision=before['revision']),f'actor-{index}',now=t+120)
                return '200'
            except HTTPException as error:
                return str(error.status_code)
        with ThreadPoolExecutor(max_workers=12) as pool:
            result=list(pool.map(race,range(12)))
        for status in result:statuses[status]=statuses.get(status,0)+1
        assert result.count('200')==1 and result.count('409')==10 and result.count('tick')==1,result
        assert row(env)['state']=='served'
        assert not env['store'].rows("SELECT id FROM restaurant_alerts WHERE visit_id=? AND kind='escalation' AND resolved_at IS NULL",(visit,))
        assert len(env['store'].rows('SELECT id FROM restaurant_visits WHERE id=?',(visit,)))==1
        # No revision supplied: repeated greeting is an idempotent acknowledgement.
        result=env['service'].action(table['id'],TableAction(action='greeted',visit_id=visit),'same',now=t+121)
        assert result.get('already_done') is True
        # Complete this party, start another, then replay the old cleanup task.
        for offset in (130,140,150,160,170,180,190):observe(env,0,t+offset)
        assert row(env)['state']=='needs_cleaning'
        old_body=TableAction(action='cleaned',visit_id=visit,revision=row(env)['revision'])
        observe(env,1,t+191);observe(env,1,t+196)
        assert row(env)['visit_id']!=visit
        expect_http(409,lambda:env['service'].action(table['id'],old_body,'old-cleaner',now=t+196))
        assert row(env)['state']=='waiting'
        for offset in (200,210,220,230,240,250,260):observe(env,0,t+offset)
        act(env,table,'cleaned',t+260)
        integrity(env['store']);checks+=1
    return {'result':'PASS','rounds':25,'concurrent_operations':300,'status_distribution':statuses,
            'stale_cleanup_replays_rejected':checks,'distinct_visits_created':50,'integrity_checks':25}


def virtual_suite(root):
    started=time.monotonic()
    env=make_env(root/'virtual-72h',table_count=7)
    active, regular=env['tables'][-1],env['tables'][:-1]
    observe(env,1,BASE,selected=[active]);observe(env,1,BASE+5,selected=[active])
    act(env,active,'hold',BASE+6)
    active_visit=row(env,active['id'])['visit_id']
    visits=0;escalations=0;faults=0;restarts=0;phase_checks=0
    rounds=500;step=72*3600/rounds
    for iteration in range(rounds):
        t=BASE+20+iteration*step
        observe(env,0,t,selected=regular)
        for table in regular:
            if row(env,table['id'])['state']=='needs_cleaning':
                act(env,table,'cleaned',t)
        observe(env,2,t+1,selected=regular)
        env['service'].tick(now=t+5.999)
        assert all(row(env,table['id'])['visit_id'] is None for table in regular)
        observe(env,2,t+6,selected=regular)
        current=[row(env,table['id'])['visit_id'] for table in regular]
        assert len(set(current))==6
        visits+=6
        for table in regular[:3]:act(env,table,'greeted',t+7)
        alert=env['store'].rows("SELECT id FROM restaurant_alerts WHERE visit_id=? AND kind='arrival'",(current[3],))[0]
        env['service'].acknowledge(alert['id'],'ack-only',now=t+8)
        assert row(env,regular[3]['id'])['state']=='waiting'
        if iteration%10==0:
            # Unknown and older observations do not finish or duplicate a party.
            observe(env,0,t+9,available=False,selected=regular)
            observe(env,0,t+8,selected=regular)
            assert all(row(env,table['id'])['visit_id']==visit for table,visit in zip(regular,current))
            faults+=2
        if iteration%25==0:
            env['service']=RestaurantService(env['store'],env['settings']);restarts+=1
            assert all(row(env,table['id'])['occupancy']=='unknown' for table in regular)
        env['service'].tick(now=t+120.999)
        assert not env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation' AND visit_id IN (%s)" % ','.join('?'*6),tuple(current))
        env['service'].tick(now=t+121)
        current_escalations=env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation' AND visit_id IN (%s)" % ','.join('?'*6),tuple(current))
        assert len(current_escalations)==3,len(current_escalations)
        escalations+=len(current_escalations)
        # Fresh empty samples across all 60 seconds are required for departure.
        for offset in (122,132,142,152,162,172,181.999):observe(env,0,t+offset,selected=regular)
        assert all(row(env,table['id'])['state'] in ('waiting','served') for table in regular)
        observe(env,0,t+182,selected=regular)
        assert all(row(env,table['id'])['state']=='needs_cleaning' for table in regular)
        for table in regular:act(env,table,'cleaned',t+182)
        assert all(row(env,table['id'])['state']=='ready' and row(env,table['id'])['visit_id'] is None for table in regular)
        if iteration%25==24:env['service'].cleanup(now=t+182)
        assert row(env,active['id'])['visit_id']==active_visit and row(env,active['id'])['hold']==1
        phase_checks+=1
        if iteration%100==99:
            print(json.dumps({'phase':'virtual72h','rounds_complete':iteration+1,'visits':visits}),flush=True)
    final=BASE+72*3600+20
    env['service'].cleanup(now=final)
    integrity(env['store'])
    remaining=env['store'].rows('SELECT count(*) AS n FROM restaurant_visits')[0]['n']
    # Retention applies independently to visit end and notification creation.
    # A cleanup notification is 60 s newer than the departure timestamp; an old
    # ended visit may correctly remain while its newer notification is retained.
    removable=env['store'].rows('''SELECT v.id FROM restaurant_visits v WHERE v.ended_at<?
        AND NOT EXISTS(SELECT 1 FROM restaurant_tables t WHERE t.visit_id=v.id)
        AND NOT EXISTS(SELECT 1 FROM restaurant_alerts a WHERE a.visit_id=v.id
                       AND (a.at>=? OR (a.resolved_at IS NULL AND a.ack_at IS NULL)))''',
        (final-86400,final-86400))
    assert not removable,removable
    retained_ended=env['store'].rows('SELECT id FROM restaurant_visits WHERE ended_at<?',(final-86400,))
    assert env['store'].rows('SELECT id FROM restaurant_visits WHERE id=?',(active_visit,))
    assert env['store'].rows('SELECT id FROM restaurant_alerts WHERE visit_id=? AND resolved_at IS NULL',(active_visit,))
    assert remaining < visits
    return {'result':'PASS','virtual_hours':72,'real_elapsed_seconds':time.monotonic()-started,
            'completed_visits':visits,'regular_visit_escalations':escalations,'rounds':rounds,
            'boundary_checks_per_round':['arrival at5s, not4.999','escalation at120s, not119.999','vacancy at60s, not59.999'],
            'fault_observations':faults,'service_reinitializations':restarts,'retention_days':1,
            'remaining_visits_after_cleanup':remaining,'ended_visits_retained_for_newer_alerts':len(retained_ended),
            'eligible_expired_visits_remaining':len(removable),'active_old_visit_preserved':True,
            'open_old_alerts_preserved':True,'duplicate_logical_alerts':0,'integrity_check':'ok'}


def scope_suite(root):
    env=make_env(root/'scope',second_site=True)
    service=env['service'];a,b=env['tables'];t=BASE
    observe(env,2,t);observe(env,2,t+5)
    waiter_a=service.authenticate_staff(env['staff']['default']['waiter']['token'])
    waiter_b=service.authenticate_staff(env['staff']['second']['waiter']['token'])
    assert {item['id'] for item in service.snapshot(staff_id=waiter_a['id'],now=t+5)['tables']}=={a['id']}
    assert {item['id'] for item in service.snapshot(staff_id=waiter_b['id'],now=t+5)['tables']}=={b['id']}
    expect_http(403,lambda:act(env,b,'greeted',t+6,staff=waiter_a))
    alert_b=env['store'].rows("SELECT id FROM restaurant_alerts WHERE site_id='second'")[0]['id']
    expect_http(403,lambda:service.acknowledge(alert_b,'a',staff=waiter_a,now=t+6))
    old_token=env['staff']['default']['waiter']['token']
    rotated=service.rotate_token(waiter_a['id'],'fixture',now=t+7)
    expect_http(401,lambda:service.authenticate_staff(old_token))
    expect_http(401,lambda:act(env,a,'greeted',t+7,staff=waiter_a))
    current=service.authenticate_staff(rotated['token'])
    assert current['id']==waiter_a['id']
    # Cross-site assignment and moving an assigned waiter are rejected.
    expect_http(422,lambda:service.save_table(a['body'].model_copy(update={'waiter_id':waiter_b['id']}),'fixture',identifier=a['id'],now=t+8))
    expect_http(409,lambda:service.save_staff(env['staff']['default']['waiter']['body'].model_copy(update={'site_id':'second'}),'fixture',identifier=waiter_a['id'],now=t+8))
    service.save_staff(env['staff']['default']['waiter']['body'].model_copy(update={'enabled':False}),'fixture',identifier=waiter_a['id'],now=t+9)
    expect_http(401,lambda:service.authenticate_staff(rotated['token']))
    open_recipients=env['store'].rows('SELECT recipient_id,site_id FROM restaurant_alerts WHERE table_id=? AND resolved_at IS NULL',(a['id'],))
    assert open_recipients==[{'recipient_id':env['staff']['default']['manager']['id'],'site_id':'default'}],open_recipients
    integrity(env['store'])
    return {'result':'PASS','sites':2,'foreign_table_action':403,'foreign_alert_ack':403,
            'old_token_after_rotation':401,'already_authenticated_stale_token_action':401,
            'cross_site_assignment':422,'assigned_waiter_site_move':409,
            'disabled_waiter_token':401,'fallback_recipient':'same-site manager only'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument('--only',choices=['crash','concurrency','virtual','scope'])
    args=parser.parse_args();started=time.monotonic()
    report={'synthetic':True,'target_commit':'98a22174f44097d53c12e1febb870951ff914aba',
            'environment':'temporary SQLite DBs, real SIGKILL, synthetic observations and mocked HTTP delivery',
            'external_messages_sent':0,'checks':{},'failures':[]}
    suites={'crash':crash_suite,'concurrency':concurrent_suite,'virtual':virtual_suite,'scope':scope_suite}
    with tempfile.TemporaryDirectory(prefix='glazza-state-stability-') as directory:
        for name,callback in suites.items():
            if args.only and name!=args.only:continue
            try:
                report['checks'][name]=callback(Path(directory))
                print(json.dumps({'phase':name,'result':report['checks'][name]},ensure_ascii=False),flush=True)
            except Exception:
                report['failures'].append({'phase':name,'traceback':traceback.format_exc()})
                print(report['failures'][-1]['traceback'],flush=True)
    report['real_elapsed_seconds']=time.monotonic()-started
    report['result']='FAIL' if report['failures'] else 'PASS'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'result':report['result'],'artifact':str(args.output),'elapsed_seconds':report['real_elapsed_seconds']}),flush=True)
    return int(bool(report['failures']))


if __name__=='__main__':
    raise SystemExit(main())
