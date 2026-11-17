"""Measure event-loop responsiveness while another SQLite writer holds a lock.

Only a temporary database is locked. This is an injected failure condition, not
a claim that ordinary staff requests take this long.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.main import create_app


async def run():
    with tempfile.TemporaryDirectory(prefix='glazza-loop-stall-') as directory:
        root=Path(directory)
        settings=Settings(data=root/'data',frigate_config=root/'config.yml',frigate_url='http://unused.invalid',
                          analyzer_url='http://unused.invalid',username='admin',password='synthetic-stall-password',
                          worker_key='synthetic-loop-worker-'*3,demo=False,secure_cookie=False)
        app=create_app(settings,background=False)
        engine,store=app.state.engine,app.state.store
        acquired=threading.Event()
        lock_interval=[]
        def writer():
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                lock_interval.append(time.monotonic());acquired.set()
                time.sleep(3.0)
            lock_interval.append(time.monotonic())
        intervals=[]
        running=True
        async def heartbeat():
            previous=time.monotonic()
            while running:
                await asyncio.sleep(.02)
                now=time.monotonic();intervals.append(now-previous);previous=now
        worker=asyncio.create_task(engine.restaurant_work())
        heart=asyncio.create_task(heartbeat())
        try:
            await asyncio.sleep(.25)
            warm_max=max(intervals)
            thread=threading.Thread(target=writer);thread.start()
            await asyncio.to_thread(acquired.wait,1)
            await asyncio.sleep(4.0)
            await asyncio.to_thread(thread.join,1)
            maximum=max(intervals)
            assert len(lock_interval)==2
            assert maximum>1.5,'Reproduction changed: event-loop stall is no longer present'
            return {'environment':'Actual Engine.restaurant_work and Store on temporary SQLite; deliberate external writer lock; real elapsed time',
                    'observed':{'writer_lock_s':lock_interval[1]-lock_interval[0],
                                'heartbeat_target_s':.02,'maximum_heartbeat_gap_s':maximum,
                                'warmup_maximum_gap_s':warm_max,'heartbeat_samples':len(intervals),
                                'database_integrity':store.rows('PRAGMA integrity_check')[0]['integrity_check']},
                    'expected':'Blocking SQLite lock acquisition must not suspend the shared async event loop.',
                    'source':['app/engine.py:44','app/restaurant.py:433','app/store.py:56']}
        finally:
            running=False;worker.cancel();heart.cancel()
            await asyncio.gather(worker,heart,return_exceptions=True)
            await engine.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    result=asyncio.run(run());data=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(data)
    print(data,end='')
