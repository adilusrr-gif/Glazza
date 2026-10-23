"""Opt-in real MediaPipe and HTTP queue smoke test; requires models/face_landmarker.task."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

import cv2
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    processes=[]
    with tempfile.TemporaryDirectory(prefix='glazza-model-') as temporary:
        root=Path(temporary)
        with urllib.request.urlopen('https://storage.googleapis.com/mediapipe-assets/portrait.jpg',timeout=30) as source:
            (root/'portrait.jpg').write_bytes(source.read())
        frame=cv2.imread(str(root/'portrait.jpg'))
        # Preserve facial geometry in the test source too.
        height,width=frame.shape[:2]
        frame=cv2.resize(frame,(width//2*2,height//2*2))
        writer=cv2.VideoWriter(str(root/'sample.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),16,(frame.shape[1],frame.shape[0]))
        for _ in range(32):writer.write(frame)
        writer.release()
        env={**os.environ,'GLAZZA_PASSWORD':'test-password-12345','GLAZZA_WORKER_KEY':'model-smoke-worker-key-1234567890123456',
             'GLAZZA_DATA':str(root/'data'),'GLAZZA_MODELS':str(ROOT/'models'),'GLAZZA_DEMO':'false',
             'FRIGATE_CONFIG':str(root/'config.yml'),'FRIGATE_URL':'http://127.0.0.1:59999','ANALYZER_URL':'http://127.0.0.1:18090'}
        with (root/'servers.log').open('w') as logs:
            try:
                for app,port in [('analyzer.main:create_app',18090),('app.main:create_app',18091)]:
                    processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',app,'--factory','--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=ROOT,env=env,stdout=logs,stderr=logs))
                with httpx.Client(trust_env=False,timeout=10) as client:
                    for _ in range(90):
                        try:
                            a=client.get('http://127.0.0.1:18090/health',headers={'X-Worker-Key':env['GLAZZA_WORKER_KEY']})
                            b=client.get('http://127.0.0.1:18091/healthz')
                            if a.status_code==b.status_code==200 and a.json()['state']=='ready':break
                        except httpx.HTTPError:pass
                        time.sleep(1)
                    else:raise RuntimeError('Services did not start')
                    base='http://127.0.0.1:18091/api'
                    login=client.post(base+'/login',json={'username':'admin','password':env['GLAZZA_PASSWORD']});login.raise_for_status()
                    client.headers['X-CSRF-Token']=login.json()['csrf']
                    upload=client.post(base+'/jobs/upload',content=(root/'sample.mp4').read_bytes(),headers={'Content-Type':'video/mp4'});upload.raise_for_status()
                    for _ in range(40):
                        job=next(j for j in client.get(base+'/jobs').json() if j['id']==upload.json()['id'])
                        if job['state'] in ('succeeded','failed'):break
                        time.sleep(1)
                    assert job['state']=='succeeded',job
                    face=job['result']['face']
                    assert face['frames_with_faces']>0 and len(face['peak_blendshapes'])==52,job
                    assert any('не проверялись' in warning for warning in job['result']['warnings'])
                    print(json.dumps({'queue':'succeeded','face_frames':face['frames_with_faces'],'coefficients':len(face['peak_blendshapes']),'action_model':'not loaded; explicit warning verified'},ensure_ascii=False))
            finally:
                for process in processes:process.terminate()
                for process in processes:
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':main()
