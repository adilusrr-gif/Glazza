"""Local UI fixture with synthetic NVR telemetry/media; never uses real cameras."""
import argparse
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time

import httpx
import uvicorn

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18082)
    parser.add_argument('--populated', action='store_true')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='glazza-ui-fixture-') as directory:
        root = Path(directory)
        settings = Settings(data=root/'data', frigate_config=root/'frigate.yml', frigate_url='http://nvr.test', analyzer_url='http://analyzer.test',
                            worker_key='ui-test-worker-key-'*3, username='admin', password='map-check-password-123', demo=False, secure_cookie=False)
        app = create_app(settings)
        store = app.state.store
        if args.populated:
            cameras = [(f'online_{i}', f'Тест · Астана {i+1}', 51.1694 + max(0, i-1)*.0003, 71.4491 + max(0, i-1)*.0002) for i in range(6)]
            cameras += [('alert_camera', 'Тест · Тревога', 51.1696, 71.4493), ('offline_camera', 'Тест · Нет сигнала', 47.0945, 51.9238),
                        ('unknown_camera', 'Тест · Нет данных', 43.2389, 76.8897), ('missing_camera', 'Тест · Не подключена', 42.3155, 69.5869),
                        ('offline_unlocated', 'Тест · Без координат', None, None)]
            for identifier, name, latitude, longitude in cameras:
                store.execute('INSERT INTO cameras(id,name,stream,latitude,longitude) VALUES(?,?,?,?,?)', (identifier, name, 'rtsp://127.0.0.1:9/test', latitude, longitude))
            now = time.time()
            store.event(source='frigate', source_id='fixture-fight', camera='alert_camera', kind='fight', score=.9, started=now-600, ended=now-595)
            for i in range(205):
                store.event(source='frigate', source_id=f'fixture-person-{i}', camera='offline_camera', kind='person', score=.8, started=now-210+i, ended=now-209+i)
        clip = root/'fixture.mp4'
        frame = root/'fixture.jpg'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=0x275cab:s=320x180:r=10','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart','-y',str(clip)], check=True)
        subprocess.run(['ffmpeg','-v','error','-i',str(clip),'-frames:v','1','-y',str(frame)], check=True)
        clip_data, frame_data = clip.read_bytes(), frame.read_bytes()

        def nvr(request):
            path = request.url.path
            if path == '/api/stats':
                return httpx.Response(200, json={'service': {'last_updated': time.time()}, 'cameras': {
                    camera['id']: {'camera_fps': 0 if camera['id'].startswith('offline_') else None if camera['id'].startswith('unknown_') else 5}
                    for camera in store.cameras() if camera['id'] != 'missing_camera'}})
            if path == '/api/events': return httpx.Response(200, json=[])
            if path == '/health': return httpx.Response(200, json={'state':'ready', 'models':[{'id':'fight','state':'ready'},{'id':'face','state':'ready'},{'id':'vadclip','state':'missing'}]})
            if path.endswith('/latest.jpg'): return httpx.Response(200, content=frame_data, headers={'Content-Type':'image/jpeg'})
            if path.endswith('/clip.mp4'): return httpx.Response(200, content=clip_data, headers={'Content-Type':'video/mp4'})
            return httpx.Response(404)

        app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(nvr))
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
