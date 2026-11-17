"""Role-specific historical-alert snapshot latency; disposable loopback fixture.

Run: .venv/bin/python tests/stability/load_history.py --output <evidence-directory>
Seeded history is synthetic resolved alert rows, not a camera workload.
"""
import argparse
import asyncio
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import time

import httpx
from restaurant_load import server_process, distribution


async def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='glazza-load-history-') as tmp:
        context=mp.get_context('spawn');parent,child=context.Pipe()
        process=context.Process(target=server_process,args=(tmp,child));process.start();child.close()
        fixture=await asyncio.to_thread(parent.recv);parent.close()
        url=f"http://127.0.0.1:{fixture['port']}"
        results=[];raw=[]
        try:
            async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as admin, httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as staff:
                for _ in range(100):
                    try:
                        if (await admin.get('/healthz')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.05)
                response=await admin.post('/api/login',json={'username':'admin','password':'synthetic-load-password'});response.raise_for_status()
                total=0
                for target in [0,1000,5000,10000]:
                    if target>total:
                        response=await admin.post('/__fixture/history',json={'count':target-total});response.raise_for_status();total=target
                    scenarios=[('admin',admin,'/api/restaurant?site_id=default',{},60),
                               ('manager_with_history',staff,'/api/restaurant/portal',{'Authorization':'Bearer '+fixture['managers'][0]['token']},60),
                               ('waiter_with_history',staff,'/api/restaurant/portal',{'Authorization':'Bearer '+fixture['workers'][0]['token']},3),
                               ('waiter_without_history',staff,'/api/restaurant/portal',{'Authorization':'Bearer '+fixture['workers'][19]['token']},3)]
                    for role,client,path,headers,tables in scenarios:
                        durations=[];statuses=[]
                        for _ in range(20):
                            start=time.monotonic();response=await client.get(path,headers=headers);elapsed=(time.monotonic()-start)*1000
                            response.raise_for_status();payload=response.json()
                            assert len(payload['tables'])==tables
                            durations.append(elapsed);statuses.append(response.status_code)
                            raw.append({'history_rows':target,'role':role,'latency_ms':elapsed,'status':response.status_code,'response_bytes':len(response.content)})
                        results.append({'resolved_history_rows':target,'role':role,'latency':distribution(durations),'statuses':sorted(set(statuses)),'returned_tables':len(payload['tables']),'returned_alerts':len(payload['alerts'])})
            result={'kind':'measured','target_commit':'98a22174f44097d53c12e1febb870951ff914aba','environment':'Isolated real Uvicorn process, loopback HTTP; seeded synthetic history; serial snapshots, no video processing',
                    'results':results,'raw_samples':raw,'verdict':'PASS','requests':len(raw),'limitations':['Latency samples describe this local host only.','Synthetic history is inserted directly; this measures snapshot read cost, not 10000 real-world table visits.','No notification delivery tested.']}
            (output/'load-history-roles.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'verdict':'PASS','requests':len(raw),'results':results},ensure_ascii=False),flush=True)
        finally:
            if process.is_alive():process.terminate()
            await asyncio.to_thread(process.join,10)
            if process.is_alive():process.kill();await asyncio.to_thread(process.join,5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    args=parser.parse_args();asyncio.run(run(args.output))
