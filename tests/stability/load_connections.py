"""Bounded follow-up with independent persistent HTTP connection pools per virtual user."""
import argparse,asyncio,json,multiprocessing as mp,tempfile,time
from pathlib import Path
import httpx,psutil
from restaurant_load import server_process,distribution

async def run(output,duration):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='glazza-load-connections-') as tmp:
        ctx=mp.get_context('spawn');parent,child=ctx.Pipe();process=ctx.Process(target=server_process,args=(tmp,child));process.start();child.close()
        fixture=await asyncio.to_thread(parent.recv);parent.close();url=f"http://127.0.0.1:{fixture['port']}";proc=psutil.Process(process.pid)
        rows=[];errors=[];resources=[]
        try:
            async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as health:
                for _ in range(100):
                    try:
                        if (await health.get('/healthz')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.05)
            clients=[httpx.AsyncClient(base_url=url,trust_env=False,timeout=20,limits=httpx.Limits(max_connections=1,max_keepalive_connections=1,keepalive_expiry=10)) for _ in range(24)]
            accounts=fixture['workers']+fixture['managers']*2
            start=time.monotonic();deadline=start+duration
            async def user(index):
                due=start;client=clients[index];account=accounts[index]
                while time.monotonic()<deadline:
                    await asyncio.sleep(max(0,due-time.monotonic()));due+=2
                    if time.monotonic()>=deadline:break
                    before=time.monotonic()
                    try:
                        response=await client.get('/api/restaurant/portal',headers={'Authorization':'Bearer '+account['token']})
                        payload=response.json();status=response.status_code
                        if status!=200 or len(payload.get('tables',[]))!=(3 if index<20 else 60):errors.append({'client':index,'status':status})
                    except Exception as error:
                        status='error';errors.append({'client':index,'type':type(error).__name__})
                    rows.append({'client':index,'at_s':time.monotonic()-start,'latency_ms':(time.monotonic()-before)*1000,'status':status})
            async def sample():
                while time.monotonic()<deadline:
                    resources.append({'at_s':time.monotonic()-start,'rss_bytes':proc.memory_info().rss,'fds':proc.num_fds(),'threads':proc.num_threads()})
                    await asyncio.sleep(5)
            print(json.dumps({'event':'persistent_connections_started','clients':24,'duration_s':duration}),flush=True)
            await asyncio.gather(*(user(i) for i in range(24)),sample())
            elapsed=time.monotonic()-start
            for client in clients:await client.aclose()
            result={'kind':'measured','target_commit':'98a22174f44097d53c12e1febb870951ff914aba','duration_s':elapsed,'clients':24,'connection_pools':24,'poll_interval_s':2,'requests':len(rows),'latency':distribution([r['latency_ms'] for r in rows]),'errors':errors,'resources':resources,'raw_samples':rows,'verdict':'PASS' if not errors else 'FAIL','limitations':['Independent local fixture, synthetic occupancy; no camera inference.','This shorter follow-up runs alongside the end of the ten-minute shared-pool soak on the shared host.','Synchronized polling intentionally exercises bursty real-device connection behavior.']}
            (output/'load-connections.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ['verdict','duration_s','requests','latency','errors']}),flush=True)
        finally:
            if process.is_alive():process.terminate()
            await asyncio.to_thread(process.join,10)
            if process.is_alive():process.kill();await asyncio.to_thread(process.join,5)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--duration',type=float,default=120);a=p.parse_args();asyncio.run(run(a.output,a.duration))
