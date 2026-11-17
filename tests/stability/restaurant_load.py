"""Opt-in restaurant stability load test; loopback HTTP, disposable DB, synthetic occupancy.

No production code is patched and no Telegram request is sent. The production
restaurant_work loop runs with a synthetic feed.pump; other NVR/AI jobs are excluded.
Run: .venv/bin/python tests/stability/restaurant_load.py --duration 600 --output <directory>
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import csv
from datetime import datetime, timezone
import json
import multiprocessing as mp
import os
from pathlib import Path
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
import psutil
import uvicorn
from app.config import Settings
from app.main import create_app
from app.restaurant import StaffInput, TableInput


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    x=(len(ordered)-1)*q
    lo=int(x);hi=min(len(ordered)-1,lo+1)
    return round(ordered[lo]+(ordered[hi]-ordered[lo])*(x-lo),3)


def distribution(values):
    return {'count':len(values),'p50_ms':percentile(values,.5),'p95_ms':percentile(values,.95),
            'p99_ms':percentile(values,.99),'max_ms':round(max(values),3) if values else None}


def server_process(directory, channel):
    directory=Path(directory)
    settings=Settings(data=directory/'data',frigate_config=directory/'config.yml',
        frigate_url='http://127.0.0.1:1',analyzer_url='http://127.0.0.1:1',
        worker_key='synthetic-load-worker-'*3,username='admin',password='synthetic-load-password',
        demo=False,secure_cookie=False)
    app=create_app(settings,background=False)
    store=app.state.store;engine=app.state.engine;service=engine.restaurant
    store.execute("UPDATE sites SET modules=? WHERE id='default'",(json.dumps(['person','restaurant']),))
    for i in range(3):
        store.execute('INSERT INTO cameras(id,name,stream,zones,site_id) VALUES(?,?,?,?,?)',
                      (f'loadcam{i}',f'Load camera {i}','rtsp://127.0.0.1:9/test','[]','default'))
    workers=[]
    for i in range(20):
        workers.append(service.save_staff(StaffInput(site_id='default',name=f'Waiter {i:02}',role='waiter'),'fixture'))
    managers=[service.save_staff(StaffInput(site_id='default',name=f'Manager {i}',role='manager'),'fixture') for i in range(2)]
    tables=[]
    for i in range(60):
        table=service.save_table(TableInput(site_id='default',camera=f'loadcam{i//20}',name=f'Table {i:02}',
            points=[(.1,.1),(.9,.1),(.9,.9),(.1,.9)],waiter_id=workers[i%20]['id']),'fixture')
        tables.append({**table,'index':i,'camera':f'loadcam{i//20}','waiter_id':workers[i%20]['id']})
    metrics={'ticks':[],'tick_exceptions':[],'started_at':None}
    t0=time.time()
    original_tick=service.tick
    def timed_tick(now=None):
        start=time.monotonic()
        try:original_tick(now)
        except Exception as error:
            metrics['tick_exceptions'].append({'type':type(error).__name__,'at':time.time()})
            raise
        finally:metrics['ticks'].append({'at':time.time(),'duration_ms':(time.monotonic()-start)*1000})
    service.tick=timed_tick
    def synthetic_pump(unused_service,unused_signal,now=None):
        elapsed=now-t0
        for camera in ['loadcam0','loadcam1','loadcam2']:
            counts={t['zone']:(2 if ((elapsed+(t['index']%6)*10)%240)<160 else 0) for t in tables if t['camera']==camera}
            service.observe(camera,counts,available=True,now=now)
        service.set_health({'state':'online','detail':'Synthetic occupancy: 160s occupied / 80s empty; real wall clock'})
    engine.restaurant_feed.pump=synthetic_pump
    original_lifespan=app.router.lifespan_context
    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            metrics['started_at']=time.time()
            task=asyncio.create_task(engine.restaurant_work())
            try:yield
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
    app.router.lifespan_context=lifespan
    @app.get('/__fixture/metrics')
    def fixture_metrics():
        with store.connect() as db:
            return {**metrics,'visits':db.execute('SELECT count(*) FROM restaurant_visits').fetchone()[0],
              'alerts':db.execute('SELECT count(*) FROM restaurant_alerts').fetchone()[0],
              'alert_kinds':[dict(r) for r in db.execute('SELECT kind,count(*) AS count FROM restaurant_alerts GROUP BY kind')],
              'escalation_lags_s':[r[0] for r in db.execute('SELECT escalated_at-deadline_at FROM restaurant_visits WHERE escalated_at IS NOT NULL')],
              'duplicate_logical_alerts':db.execute('SELECT count(*) FROM (SELECT count(*) c FROM restaurant_alerts GROUP BY visit_id,kind,recipient_id HAVING c>1)').fetchone()[0],
              'duplicate_active_visits':db.execute('SELECT count(*) FROM (SELECT table_id,count(*) c FROM restaurant_visits WHERE ended_at IS NULL GROUP BY table_id HAVING c>1)').fetchone()[0],
              'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}
    @app.post('/__fixture/history')
    def fixture_history(body:dict):
        n=int(body['count']);at=time.time();manager=managers[0];worker=workers[0]
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.executemany('''INSERT INTO restaurant_alerts(id,site_id,table_id,table_name,visit_id,kind,recipient_id,recipient_name,at,message,ack_at,resolved_at,delivery_state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[(uuid.uuid4().hex,'default',tables[i%60]['id'],f'Table {i%60:02}',f'history-{uuid.uuid4().hex}','arrival',
                 worker['id'] if i%2 else manager['id'],'Synthetic recipient',at-i*.001,'Synthetic resolved alert',at,at,'in_app') for i in range(n)])
        return {'inserted':n}
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    channel.send({'port':listener.getsockname()[1],'workers':workers,'managers':managers,'tables':tables,'db_path':str(store.path)})
    channel.close()
    uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False)).run(sockets=[listener])


async def run(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    raw_path=out/'load-requests.csv'
    with tempfile.TemporaryDirectory(prefix='glazza-load-') as tmp:
        context=mp.get_context('spawn');parent,child=context.Pipe()
        process=context.Process(target=server_process,args=(tmp,child));process.start();child.close()
        fixture=await asyncio.to_thread(parent.recv);parent.close()
        url=f"http://127.0.0.1:{fixture['port']}";proc=psutil.Process(process.pid)
        rows=[];errors=[];resource_samples=[];scope_errors=[]
        phase='warmup';started=time.monotonic()
        async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=20,limits=httpx.Limits(max_connections=150,max_keepalive_connections=100)) as client:
            try:
                for _ in range(100):
                    try:
                        response=await client.get('/healthz')
                        if response.status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.05)
                else:raise RuntimeError('Fixture HTTP server did not become healthy')
                login=await client.post('/api/login',json={'username':'admin','password':'synthetic-load-password'});login.raise_for_status()
                admin_headers={'X-CSRF-Token':login.json()['csrf']}
                # Staff requests share this synthetic transport (including its admin cookie).
                # Staff endpoints still authenticate their explicitly supplied Bearer.
                # This load test does not substitute for cookie-free authorization tests.
                async def request(kind,method,path,**kwargs):
                    before=time.monotonic();status='error';size=0;payload=None
                    try:
                        response=await client.request(method,path,**kwargs);status=response.status_code;size=len(response.content)
                        if response.headers.get('content-type','').startswith('application/json'):payload=response.json()
                        if status>=500 or status not in (200,409):errors.append({'phase':phase,'kind':kind,'status':status,'detail':str(payload)[:300]})
                    except (httpx.HTTPError,ValueError) as error:
                        errors.append({'phase':phase,'kind':kind,'type':type(error).__name__})
                    rows.append((phase,round(time.monotonic()-started,3),kind,str(status),round((time.monotonic()-before)*1000,3),size))
                    return payload,status
                staff=fixture['workers']+fixture['managers']
                async def poller(index,employee,deadline):
                    headers={'Authorization':'Bearer '+employee['token']}
                    rounds=0;due=time.monotonic()+index*.075
                    while time.monotonic()<deadline:
                        await asyncio.sleep(max(0,due-time.monotonic()));due+=2
                        if time.monotonic()>=deadline:break
                        payload,status=await request('staff_read','GET','/api/restaurant/portal',headers=headers)
                        rounds+=1
                        if status!=200 or not payload:continue
                        if index<20 and any(table['waiter_id']!=employee['id'] for table in payload['tables']):scope_errors.append({'employee_index':index,'kind':'unexpected_table'})
                        if index<20:
                            for table in payload['tables']:
                                table_index=int(table['name'].split()[-1]);allowed=table['allowed_actions'];action=None
                                if 'cleaned' in allowed:action='cleaned'
                                elif 'greeted' in allowed and table_index%3!=0 and payload['server_time']-table['arrived_at']>=10:action='greeted'
                                if action:
                                    await request('staff_action','POST',f"/api/restaurant/portal/tables/{table['id']}/actions",headers=headers,
                                        json={'action':action,'visit_id':table['visit_id'],'revision':table['revision']})
                        for alert in [a for a in payload['alerts'] if a['ack_at'] is None and a['resolved_at'] is None][:3]:
                            await request('staff_ack','POST',f"/api/restaurant/portal/alerts/{alert['id']}/ack",headers=headers)
                async def admin_poller(index,deadline):
                    due=time.monotonic()+index*.2
                    while time.monotonic()<deadline:
                        await asyncio.sleep(max(0,due-time.monotonic()));due+=2
                        if time.monotonic()>=deadline:break
                        await request('admin_read','GET','/api/restaurant?site_id=default',headers=admin_headers)
                async def resources(deadline):
                    due=time.monotonic()
                    while time.monotonic()<deadline:
                        with proc.oneshot():
                            sample={'elapsed_s':round(time.monotonic()-started,3),'rss_bytes':proc.memory_info().rss,'fds':proc.num_fds(),'threads':proc.num_threads(),'cpu_s':sum(proc.cpu_times()[:2])}
                        sample['db_bytes']=sum(p.stat().st_size for p in Path(tmp,'data').glob('glazza.db*'))
                        resource_samples.append(sample)
                        (out/'load-progress.json').write_text(json.dumps({'phase':phase,'elapsed_s':sample['elapsed_s'],'requests':len(rows),'errors':len(errors),'resource':sample},indent=2))
                        due+=10
                        await asyncio.sleep(max(0,min(due,deadline)-time.monotonic()))
                phase='soak';started=time.monotonic();deadline=started+args.duration
                print(json.dumps({'event':'soak_started','at_utc':datetime.now(timezone.utc).isoformat(),'duration_s':args.duration,'tables':60,'clients':24,'poll_interval_s':2,'server_pid':process.pid,'source':'synthetic occupancy; defaults arrival5,vacancy60,wait120; real wall time'}),flush=True)
                await asyncio.gather(*(poller(i,e,deadline) for i,e in enumerate(staff)),*(admin_poller(i,deadline) for i in range(2)),resources(deadline))
                actual_elapsed=time.monotonic()-started
                metrics=(await client.get('/__fixture/metrics')).json()
                (out/'load-soak-runtime.json').write_text(json.dumps(metrics,indent=2)+'\n')
                print(json.dumps({'event':'soak_finished','elapsed_s':actual_elapsed,'requests':len(rows),'errors':len(errors),'visits':metrics['visits'],'alerts':metrics['alerts']}),flush=True)
                burst_results=[]
                for concurrency in [50,100]:
                    phase=f'burst_{concurrency}';mark=len(rows)
                    for _ in range(5):
                        await asyncio.gather(*(request('admin_read','GET','/api/restaurant?site_id=default',headers=admin_headers) for _ in range(concurrency)))
                    new=rows[mark:]
                    burst_results.append({'concurrency':concurrency,'waves':5,'requests':len(new),'latency':distribution([r[4] for r in new]),'statuses':{s:sum(r[3]==s for r in new) for s in set(r[3] for r in new)}})
                history_results=[]
                total=0
                for target in [0,1000,5000,10000]:
                    phase=f'history_{target}'
                    if target>total:
                        response=await client.post('/__fixture/history',json={'count':target-total});response.raise_for_status();total=target
                    samples=[]
                    for _ in range(12):
                        before=time.monotonic();payload,status=await request('admin_read','GET','/api/restaurant?site_id=default',headers=admin_headers)
                        samples.append((time.monotonic()-before)*1000)
                    history_results.append({'resolved_history_inserted':target,'latency':distribution(samples),'returned_alerts':len(payload['alerts']) if payload else None})
                final_metrics=(await client.get('/__fixture/metrics')).json()
                with proc.oneshot():
                    final_resource={'rss_bytes':proc.memory_info().rss,'fds':proc.num_fds(),'threads':proc.num_threads(),'cpu_s':sum(proc.cpu_times()[:2])}
                soak_rows=[r for r in rows if r[0]=='soak']
                grouped={kind:{'latency':distribution([r[4] for r in soak_rows if r[2]==kind]),'statuses':{s:sum(r[2]==kind and r[3]==s for r in soak_rows) for s in set(r[3] for r in soak_rows if r[2]==kind)}} for kind in sorted(set(r[2] for r in soak_rows))}
                ticks=metrics['ticks'];gaps=[ticks[i]['at']-ticks[i-1]['at'] for i in range(1,len(ticks))]
                result={'kind':'measured','target_commit':args.commit,'environment':'Local disposable Linux server process; real HTTP, synthetic camera observations; no real Telegram, Frigate or AI workers',
                    'workload':{'tables':60,'cameras':3,'waiters':20,'managers':2,'admin_clients':2,'total_clients':24,'poll_interval_s':2,'occupied_s':160,'empty_s':80,'wait_seconds':120,'arrival_seconds':5,'vacancy_seconds':60},
                    'duration_s':round(actual_elapsed,3),'soak_requests':len(soak_rows),'soak_requests_per_second':round(len(soak_rows)/actual_elapsed,3),
                    'soak_groups':grouped,'errors':errors,'scope_errors':scope_errors,'resources':resource_samples,'final_resource_after_benchmarks':final_resource,
                    'tick_count':len(ticks),'tick_latency_ms':distribution([t['duration_ms'] for t in ticks]),
                    'tick_gap_s':{'max':round(max(gaps),3),'p95':percentile(gaps,.95)},
                    'escalations':len(metrics['escalation_lags_s']),'deadline_lag_s':{'max':round(max(metrics['escalation_lags_s']),3) if metrics['escalation_lags_s'] else None,'p95':percentile(metrics['escalation_lags_s'],.95)},
                    'logical':{k:metrics[k] for k in ['visits','alerts','alert_kinds','duplicate_logical_alerts','duplicate_active_visits','integrity','tick_exceptions']},
                    'burst':burst_results,'history':history_results,'final_integrity':final_metrics['integrity'],
                    'limitations':['Ten-minute soak is not a multi-day endurance claim.','Shared execution host is not the target production server.','Application HTTP/service loop only; video decoding, model inference and other background jobs are outside this workload.','No external notification delivery tested.','Synthetic camera occupancy follows deterministic cycles; no detector accuracy inferred.']}
                result['verdict']='PASS' if not errors and not scope_errors and final_metrics['integrity']=='ok' and metrics['duplicate_logical_alerts']==0 and metrics['duplicate_active_visits']==0 and actual_elapsed>=args.duration else 'FAIL'
                (out/'load-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                with raw_path.open('w',newline='') as f:
                    writer=csv.writer(f);writer.writerow(['phase','elapsed_s','kind','status','duration_ms','response_bytes']);writer.writerows(rows)
                print(json.dumps({'event':'complete','verdict':result['verdict'],'result':str(out/'load-results.json'),'groups':grouped,'burst':burst_results},ensure_ascii=False),flush=True)
            finally:
                if process.is_alive():process.terminate()
                await asyncio.to_thread(process.join,10)
                if process.is_alive():process.kill();await asyncio.to_thread(process.join,5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=float,default=600)
    parser.add_argument('--output',required=True)
    parser.add_argument('--commit',default='98a22174f44097d53c12e1febb870951ff914aba')
    args=parser.parse_args();asyncio.run(run(args))


if __name__=='__main__':main()
