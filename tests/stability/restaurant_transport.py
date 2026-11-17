"""Disposable restaurant transport/notification fault checks, no real messages.

Run: .venv/bin/python tests/stability/restaurant_transport.py
All camera/MQTT publications are synthetic. HTTP uses MockTransport only.
This script records failures rather than hiding them behind a green exit code.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace

import httpx
from amqtt.broker import Broker
import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.engine import Engine
from app.restaurant import RestaurantService, StaffInput, TableInput
from app.restaurant_feed import RestaurantFeed
from app.store import Store

RUN = ROOT / 'docs/audits/restaurant-stability-2026-11-17'


def settings(directory, **extra):
    return Settings(data=Path(directory)/'data', frigate_config=Path(directory)/'nvr.yml',
                    frigate_url='http://fixture.invalid', analyzer_url='http://fixture.invalid',
                    worker_key='isolated-transport-fixture-'*3, username='fixture',
                    password='isolated-transport-password', demo=False, secure_cookie=False, **extra)


class Fixture:
    def __init__(self, directory, **extra):
        self.settings = settings(directory, **extra)
        self.store = Store(self.settings.data)
        self.store.execute("UPDATE sites SET modules=? WHERE id='default'", ('["person","restaurant"]',))
        self.store.execute("INSERT INTO cameras(id,name,stream,zones,site_id) VALUES('dining','Synthetic dining','rtsp://127.0.0.1:9/test','[]','default')")
        self.service = RestaurantService(self.store, self.settings)
        self.staff = {}
        for i, role in enumerate(['waiter','manager']):
            self.staff[role] = self.service.save_staff(StaffInput(site_id='default', name=role,
                    role=role, telegram_chat_id=str(111+i)), 'fixture')
        self.tables = []

    def add_table(self, name='Table 1'):
        body = TableInput(site_id='default',camera='dining',name=name,
                          points=[(.1,.1),(.7,.1),(.7,.7),(.1,.7)],
                          waiter_id=self.staff['waiter']['id'], arrival_seconds=1, vacancy_seconds=10)
        table = self.service.save_table(body,'fixture')
        self.tables.append(table)
        return table

    def seat_all(self, start):
        counts={table['zone']:1 for table in self.tables}
        self.service.observe('dining',counts,available=True,now=start)
        self.service.observe('dining',counts,available=True,now=start+1)

    def current(self, identifier):
        return next(t for t in self.service.snapshot()['tables'] if t['id']==identifier)


async def until(predicate, label, action=None, timeout=12):
    started=time.monotonic()
    while time.monotonic()-started<timeout:
        if action: action()
        if predicate(): return time.monotonic()-started
        await asyncio.sleep(.025)
    raise AssertionError('Timed out: '+label)


async def broker_main(port):
    broker=Broker({'listeners':{'default':{'type':'tcp','bind':f'127.0.0.1:{port}'}},
        'sys_interval':0,'plugins':{'amqtt.plugins.authentication.AnonymousAuthPlugin':{'allow_anonymous':True}}})
    await broker.start()
    print('BROKER_READY',flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


async def mqtt_checks():
    results=[]
    old_hook=threading.excepthook
    callback_errors=[]
    def thread_error(args):
        callback_errors.append({'type':args.exc_type.__name__,'message':str(args.exc_value),
            'traceback':''.join(traceback.format_exception(args.exc_type,args.exc_value,args.exc_traceback))})
    threading.excepthook=thread_error
    with tempfile.TemporaryDirectory(prefix='glazza-transport-mqtt-') as directory:
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.close()
        broker_log=open(Path(directory)/'broker.log','w+')
        def launch_broker():
            return subprocess.Popen([sys.executable,str(Path(__file__)),'--broker',str(port)],stdout=broker_log,stderr=broker_log)
        broker=launch_broker()
        def listening():
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1): return True
            except OSError: return False
        await until(listening,'broker starts')
        env=Fixture(directory,mqtt_host='127.0.0.1',mqtt_port=port)
        table=env.add_table();zone=table['zone'];service=env.service
        nvr=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='transport-synthetic-nvr')
        state={'respond':True,'connected':False,'objects':[{'id':'guest','label':'person',
                'stationary':True,'active':False,'current_zones':[zone]}],'config':{'enabled':True,'detect':True}}
        requests=[]
        def snapshot():
            nvr.publish('frigate/camera_activity',json.dumps({'dining':{'objects':state['objects'],'config':state['config']}}),qos=1)
        def on_connect(client,userdata,flags,reason,properties):
            if reason==0:
                state['connected']=True
                client.subscribe('frigate/onConnect',qos=1)
                for topic,payload in {'frigate/available':'online','frigate/dining/enabled/state':'ON',
                        'frigate/dining/detect/state':'ON',f'frigate/dining/zone/{zone}/state':'ON'}.items():
                    client.publish(topic,payload,qos=1,retain=True)
        def on_disconnect(*args): state['connected']=False
        def on_message(client,userdata,message):
            if message.topic=='frigate/onConnect':
                requests.append(time.monotonic())
                if state['respond']: snapshot()
        nvr.on_connect=on_connect;nvr.on_disconnect=on_disconnect;nvr.on_message=on_message
        nvr.reconnect_delay_set(1,2);nvr.connect_async('127.0.0.1',port,keepalive=5);nvr.loop_start()
        feed=RestaurantFeed(env.settings)
        signal_state={'signal':'online'}
        def pump():
            feed.pump(service,lambda *args,**kwargs:signal_state)
            service.tick()
        def current(): return env.current(table['id'])
        try:
            feed.start()
            await until(lambda:current()['state']=='waiting' and current()['color']=='red','initial real MQTT seating',pump)
            original=current();visit=original['visit_id'];deadline=original['deadline_at']
            nvr.publish(f'frigate/{zone}/person/active','0',qos=1)
            await asyncio.sleep(.1);pump()
            assert current()['people']==1 and current()['visit_id']==visit
            results.append({'case':'stationary_guest','status':'passed','people':current()['people']})

            recoveries=[]
            for cycle in range(8):
                broker.kill();await asyncio.to_thread(broker.wait)
                await until(lambda:not feed.connected,'disconnect observed',pump)
                assert current()['color']=='gray' and current()['visit_id']==visit
                started=time.monotonic();broker=launch_broker()
                await until(lambda:current()['color']=='red','recover after broker SIGKILL',pump,timeout=20)
                elapsed=time.monotonic()-started;recoveries.append(elapsed)
                assert current()['visit_id']==visit and current()['deadline_at']==deadline
            results.append({'case':'broker_sigkill_restart','status':'passed','cycles':len(recoveries),
                'recovery_seconds':recoveries,'visit_preserved':True,'deadline_preserved':True})

            for kind,topic in [('detect','frigate/dining/detect/state'),('enabled','frigate/dining/enabled/state'),
                              ('zone',f'frigate/dining/zone/{zone}/state')]:
                if kind!='zone':state['config'][kind]=False
                nvr.publish(topic,'OFF',qos=1,retain=True)
                await until(lambda:current()['color']=='gray',kind+' disabled',pump)
                assert current()['visit_id']==visit
                if kind!='zone':state['config'][kind]=True
                nvr.publish(topic,'ON',qos=1,retain=True);snapshot()
                await until(lambda:current()['color']=='red',kind+' enabled',pump)
                results.append({'case':kind+'_off_on','status':'passed','visit_preserved':True})
            signal_state['signal']='offline';pump();assert current()['color']=='gray'
            signal_state['signal']='online';pump();assert current()['color']=='red'
            results.append({'case':'camera_fps_offline','status':'passed'})

            state['respond']=False
            nvr.publish('frigate/camera_activity',json.dumps({'dining':{}}),qos=1)
            await until(lambda:current()['color']=='gray','partial snapshot unknown',pump)
            assert current()['visit_id']==visit
            state['respond']=True;snapshot()
            await until(lambda:current()['color']=='red','snapshot recovery',pump)
            results.append({'case':'partial_snapshot','status':'passed'})

            # The live publisher first goes silent; old retained zero is replayed
            # only after a fresh subscriber connects, as MQTT RETAIN semantics require.
            state['respond']=False
            await asyncio.to_thread(feed.close)
            nvr.publish(f'frigate/{zone}/person','0',qos=1,retain=True)
            nvr.publish('frigate/camera_activity',json.dumps({'dining':{'objects':[],'config':state['config']}}),qos=1,retain=True)
            await asyncio.sleep(.1)
            feed=RestaurantFeed(env.settings);feed.set_tables(service.configuration_tables());feed.start()
            await until(lambda:feed.connected and feed.online,'new feed connects',pump)
            for _ in range(6):pump();await asyncio.sleep(.1)
            assert current()['color']=='gray' and current()['visit_id']==visit
            state['respond']=True;snapshot()
            await until(lambda:current()['color']=='red','fresh snapshot after retained replay',pump)
            nvr.publish('frigate/camera_activity','',qos=1,retain=True)
            results.append({'case':'retained_stale_empty_replay','status':'passed'})

            state['respond']=False
            initial_snapshot=feed.snapshot_at['dining'];started=time.monotonic()
            while time.time()-initial_snapshot<=16:
                pump();await asyncio.sleep(.1)
            assert current()['color']=='gray' and current()['visit_id']==visit and current()['state']=='waiting'
            results.append({'case':'snapshot_silence_16_seconds','status':'passed','elapsed_seconds':time.monotonic()-started})
            state['respond']=True;snapshot()
            await until(lambda:current()['color']=='red','silence recovery',pump)

            # Establish late zone states before the feed knows their whitelist entry.
            new=env.add_table('Late table')
            nvr.publish('frigate/dining/zone/'+new['zone']+'/state','ON',qos=1,retain=True)
            await asyncio.sleep(.1)
            await until(lambda:env.current(new['id'])['color']=='yellow','late zone retained replay',pump)
            results.append({'case':'late_table_binding','status':'passed'})

            state['respond']=False
            for payload in [b'\xff',b'{bad json',b'null',b'0',b'x'*2_000_001]:
                nvr.publish('frigate/camera_activity',payload,qos=1)
            await asyncio.sleep(.3);pump()
            assert not callback_errors
            state['respond']=True;snapshot();await until(lambda:current()['color']=='red','valid after malformed',pump)
            results.append({'case':'bounded_malformed_inputs','status':'passed','payloads':5})

            before=feed.snapshot_at.get('dining')
            payload='['*20000+'0'+']'*20000
            nvr.publish('frigate/camera_activity',payload,qos=1)
            await asyncio.sleep(.5)
            snapshot();await asyncio.sleep(1);pump()
            alive=bool(feed.client._thread and feed.client._thread.is_alive())
            after=feed.snapshot_at.get('dining')
            results.append({'case':'deep_json_thread_survival','status':'failed' if not alive else 'passed',
                'payload_bytes':len(payload),'thread_alive':alive,'connected_flag':feed.connected,
                'valid_snapshot_processed_after_fault':after!=before,'exceptions':callback_errors,
                'expected':'Malformed message is ignored; MQTT worker continues to accept subsequent valid snapshots'})
            (RUN/'evidence/transport-mqtt.json').write_text(json.dumps({'protocol':'real loopback TCP MQTT',
                'publisher':'synthetic NVR','checks':results,'snapshot_requests':len(requests)},ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'mqtt_checks':len(results),'failed':[x['case'] for x in results if x['status']=='failed']},ensure_ascii=False),flush=True)
            return results
        finally:
            await asyncio.to_thread(feed.close)
            nvr.disconnect();await asyncio.to_thread(nvr.loop_stop)
            broker.kill();await asyncio.to_thread(broker.wait)
            broker_log.close();threading.excepthook=old_hook


async def delivery_checks():
    results=[]
    with tempfile.TemporaryDirectory(prefix='glazza-transport-delivery-') as directory:
        env=Fixture(directory,telegram_bot_token='synthetic-never-sent')
        env.add_table();env.seat_all(10000)
        attempts=[];outcomes=[429,500,'timeout',200]
        async def provider(request):
            outcome=outcomes.pop(0);attempts.append(outcome)
            if outcome=='timeout':raise httpx.ReadTimeout('synthetic provider timeout',request=request)
            return httpx.Response(outcome,json={'ok':outcome==200})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            for _ in range(4):
                row=env.store.rows('SELECT * FROM restaurant_alerts')[0]
                await env.service.deliver(client,now=max(10001,row['retry_at']))
            row=env.store.rows('SELECT * FROM restaurant_alerts')[0]
            assert row['delivery_state']=='sent' and row['delivery_attempts']==4
            results.append({'case':'429_500_timeout_then_success','status':'passed','attempts':list(attempts),'final_state':row['delivery_state']})
            env.store.execute("UPDATE restaurant_alerts SET delivery_state='sending',delivery_started=20000,delivery_attempts=1,delivered_at=NULL")
            attempts.clear();outcomes.append(200)
            await env.service.deliver(client,now=20059)
            assert not attempts
            await env.service.deliver(client,now=20061)
            assert attempts==[200]
            results.append({'case':'sending_lease_recovery','status':'passed','eligible_after_seconds':61})

    with tempfile.TemporaryDirectory(prefix='glazza-transport-rate-limit-') as directory:
        env=Fixture(directory,telegram_bot_token='synthetic-never-sent')
        env.add_table();env.seat_all(10000)
        now=10001.0;attempt_times=[]
        async def limited(request):
            attempt_times.append(now)
            return httpx.Response(429,json={'ok':False,'error_code':429,'parameters':{'retry_after':3600}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
            for _ in range(8):
                await env.service.deliver(client,now=now)
                row=env.store.rows('SELECT * FROM restaurant_alerts')[0]
                now=row['retry_at']
            before=len(attempt_times)
            await env.service.deliver(client,now=14001)
            row=env.store.rows('SELECT * FROM restaurant_alerts')[0]
            respects_window=all(b-a>=3600 for a,b in zip(attempt_times,attempt_times[1:]))
            results.append({'case':'provider_retry_after_respected','status':'passed' if respects_window else 'failed',
                'provider_retry_after_seconds':3600,'attempt_elapsed_virtual_seconds':[at-attempt_times[0] for at in attempt_times],
                'final_state':row['delivery_state'],'attempts_after_provider_window':len(attempt_times)-before,
                'expected':'Honor provider retry_after; keep task retryable through the declared temporary limit window',
                'clock':'virtual; HTTP outcomes mocked'})

    with tempfile.TemporaryDirectory(prefix='glazza-transport-queue-') as directory:
        env=Fixture(directory,telegram_bot_token='synthetic-never-sent')
        for i in range(10):env.add_table('Busy table '+str(i))
        engine=Engine(env.store,env.settings);env.service=engine.restaurant
        started_wall=time.time();started_mono=time.monotonic()
        env.seat_all(started_wall-119)
        first_manager=asyncio.Event();provider_calls=[];ticks=[]
        original_tick=engine.restaurant.tick
        def tick(*args,**kwargs):
            now=kwargs.get('now',time.time());original_tick(*args,**kwargs);ticks.append(now)
        # Test-only instrumentation observes the unchanged implementation.
        engine.restaurant.tick=tick
        async def slow(request):
            payload=json.loads(request.content);role='waiter' if payload['chat_id']=='111' else 'manager'
            provider_calls.append({'role':role,'elapsed_seconds':time.monotonic()-started_mono})
            if role=='waiter':
                await asyncio.sleep(2)
                return httpx.Response(500,json={'ok':False})
            first_manager.set();return httpx.Response(200,json={'ok':True})
        await engine.client.aclose();engine.client=httpx.AsyncClient(transport=httpx.MockTransport(slow))
        engine.tasks=[asyncio.create_task(engine.restaurant_work()),asyncio.create_task(engine.restaurant_delivery())]
        try:
            await asyncio.wait_for(first_manager.wait(),timeout=100)
            await asyncio.sleep(.1)
            alerts=env.store.rows("SELECT kind,at,delivery_state,delivery_attempts FROM restaurant_alerts ORDER BY at")
            first_escalation=min(a['at'] for a in alerts if a['kind']=='escalation')
            manager_call=next(x['elapsed_seconds'] for x in provider_calls if x['role']=='manager')
            lag=first_escalation-(started_wall+1)
            results.append({'case':'slow_provider_timer_isolation','status':'passed' if lag<1.5 else 'failed',
                'measured_tick_deadline_lag_seconds':lag,'tick_count':len(ticks),
                'max_tick_gap_seconds':max(b-a for a,b in zip(ticks,ticks[1:])),
                'clock':'real elapsed; visits seeded119seconds before start to exercise120second deadline'})
            prior_waiter_attempts=sum(x['role']=='waiter' for x in provider_calls if x['elapsed_seconds']<manager_call)
            results.append({'case':'healthy_manager_queue_delay','status':'failed' if prior_waiter_attempts>10 else 'passed',
                'waiter_alerts_before_escalation':10,'slow_waiter_provider_seconds':2,
                'waiter_attempts_before_first_manager':prior_waiter_attempts,
                'manager_provider':'immediate HTTP200','manager_first_request_elapsed_seconds':manager_call,
                'manager_delay_after_in_app_escalation_seconds':started_wall+manager_call-first_escalation,
                'provider_calls':provider_calls,'expected':'A healthy manager destination should not wait behind repeated failures for a different recipient'})
        finally:
            await engine.close()
    (RUN/'evidence/transport-delivery.json').write_text(json.dumps({'transport':'httpx.MockTransport only; no external requests',
        'checks':results},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'delivery_checks':len(results),'failed':[x['case'] for x in results if x['status']=='failed']},ensure_ascii=False),flush=True)
    return results


async def run(scope='all'):
    RUN.joinpath('evidence').mkdir(parents=True,exist_ok=True)
    mqtt_result=await mqtt_checks() if scope in ('all','mqtt') else []
    delivery_result=await delivery_checks() if scope in ('all','delivery') else []
    all_checks=mqtt_result+delivery_result
    summary={'checks':len(all_checks),'passed':sum(x['status']=='passed' for x in all_checks),
             'failed':sum(x['status']=='failed' for x in all_checks)}
    print(json.dumps(summary),flush=True)
    return 1 if summary['failed'] else 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--broker',type=int)
    parser.add_argument('--scope',choices=['all','mqtt','delivery'],default='all')
    args=parser.parse_args();logging.basicConfig(level=logging.CRITICAL)
    if args.broker:asyncio.run(broker_main(args.broker))
    else:raise SystemExit(asyncio.run(run(args.scope)))
