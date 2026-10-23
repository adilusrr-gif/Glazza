"""Opt-in: real VD-MIL weights, public RWF author demos, and end-to-end HTTP queue.

Requires analyzer deps, FFmpeg and `python scripts/models.py fight`.
Downloads are temporary; source clips are not republished. This is NOT an accuracy test.
"""
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]


def main():
    processes = []
    report = []
    with tempfile.TemporaryDirectory(prefix='glazza-fight-') as temporary:
        root = Path(temporary)
        for n in (1, 2):
            url = f'https://raw.githubusercontent.com/mchengny/RWF2000-Video-Database-for-Violence-Detection/master/Images/result_{n}.gif'
            with urllib.request.urlopen(url, timeout=45) as response:
                (root / f'{n}.gif').write_bytes(response.read())
            # Published GIF has video on the left, the author's prediction graph on the right.
            # Remove the graph BEFORE inference to avoid passing label information to the model.
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(root / f'{n}.gif'), '-vf', 'crop=iw/2:ih:0:0',
                            '-pix_fmt', 'yuv420p', '-y', str(root / f'{n}.mp4')], check=True)
        cap = cv2.VideoCapture(str(root / '1.mp4'))
        ok, frame = cap.read()
        cap.release()
        assert ok
        writer = cv2.VideoWriter(str(root / 'static.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), 8, (frame.shape[1], frame.shape[0]))
        for _ in range(40):
            writer.write(frame)
        writer.release()
        (root / 'models').mkdir()
        (root / 'models/vdmil').symlink_to(ROOT / 'models/vdmil', target_is_directory=True)
        env = {**os.environ, 'GLAZZA_PASSWORD': 'test-password-12345', 'GLAZZA_WORKER_KEY': 'fight-smoke-worker-key-1234567890123456',
               'GLAZZA_DATA': str(root / 'data'), 'GLAZZA_MODELS': str(root / 'models'), 'GLAZZA_DEMO': 'false',
               'GLAZZA_DEVICE': 'cpu', 'GLAZZA_FIGHT_THRESHOLD': '0.8', 'GLAZZA_FIGHT_WINDOWS': '3',
               'FRIGATE_CONFIG': str(root / 'config.yml'), 'FRIGATE_URL': 'http://127.0.0.1:59999', 'ANALYZER_URL': 'http://127.0.0.1:18190'}
        with (root / 'servers.log').open('w') as logs:
            try:
                for app, port in [('analyzer.main:create_app', 18190), ('app.main:create_app', 18191)]:
                    processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', app, '--factory', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=ROOT, env=env, stdout=logs, stderr=logs))
                with httpx.Client(trust_env=False, timeout=15) as client:
                    for _ in range(90):
                        try:
                            a = client.get('http://127.0.0.1:18190/health', headers={'X-Worker-Key': env['GLAZZA_WORKER_KEY']})
                            b = client.get('http://127.0.0.1:18191/healthz')
                            if a.status_code == b.status_code == 200:
                                assert {'id': 'fight', 'state': 'ready'} in a.json()['models'], a.text
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(1)
                    else:
                        raise RuntimeError('Services did not start')
                    base = 'http://127.0.0.1:18191/api'
                    login = client.post(base + '/login', json={'username': 'admin', 'password': env['GLAZZA_PASSWORD']})
                    login.raise_for_status()
                    client.headers['X-CSRF-Token'] = login.json()['csrf']
                    for filename, positive in [('1.mp4', True), ('2.mp4', True), ('static.mp4', False)]:
                        started = time.monotonic()
                        upload = client.post(base + '/jobs/upload', content=(root / filename).read_bytes(), headers={'Content-Type': 'video/mp4'})
                        upload.raise_for_status()
                        for _ in range(90):
                            job = next(j for j in client.get(base + '/jobs').json() if j['id'] == upload.json()['id'])
                            if job['state'] in ('succeeded', 'failed'):
                                break
                            time.sleep(1)
                        assert job['state'] == 'succeeded', job
                        result = job['result']
                        assert result['checked'] == ['fight'], result
                        assert bool(result['detections']) == positive, result
                        assert any('кража' in w and 'не проверялась' in w for w in result['warnings'])
                        if positive:
                            event = next(e for e in client.get(base + '/events?kind=fight').json() if e['details']['job_id'] == job['id'])
                            assert event['status'] == 'new' and event['details']['review_required']
                            assert client.get(base + f'/events/{event["id"]}/media/clip').status_code == 200
                            assert client.patch(base + f'/events/{event["id"]}', json={'status': 'confirmed', 'note': 'Public demo integration test'}).status_code == 200
                        report.append({'sample': filename, 'expected_fight': positive, 'detections': result['detections'],
                                       'peak_score': result['fight']['peak_score'], 'windows': result['fight']['windows_analyzed'],
                                       'pipeline_seconds': round(time.monotonic() - started, 2),
                                       'input_sha256': hashlib.sha256((root / filename).read_bytes()).hexdigest()})
                    assert client.get(base + '/overview').json()['pending_fights'] == 0
                    print(json.dumps({'result': 'PASS', 'samples': report, 'scope': 'Integration only; not a benchmark or college acceptance'}, ensure_ascii=False, indent=2))
            except Exception:
                logs.flush()
                print((root / 'servers.log').read_text()[-5000:])
                raise
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()


if __name__ == '__main__':
    main()
