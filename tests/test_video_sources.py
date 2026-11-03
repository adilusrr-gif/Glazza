import asyncio
import time

import httpx
import pytest

from app.frigate import make_config


@pytest.mark.parametrize('stream', [
    'rtsp://user:secret@192.168.1.2/live', 'rtsps://camera.example/live',
    'http://192.168.1.3/mjpeg', 'https://camera.example/live.m3u8',
    'http://camera.example/live.flv', 'rtmp://camera.example/live/camera',
    'rtmps://camera.example/live/camera',
])
def test_camera_protocol_roundtrip_and_config(logged, app, camera, stream):
    camera['stream'] = stream
    assert logged.post('/api/cameras', json=camera).status_code == 201
    public = logged.get('/api/cameras')
    assert stream not in public.text
    config = make_config(app.state.store.cameras(private=True))
    source = config['cameras']['entrance']['ffmpeg']['inputs'][0]
    assert set(source['roles']) == {'detect', 'record'}
    if stream.startswith(('rtsp:', 'rtsps:')):
        assert source['path'] == stream
    else:
        assert source['path'] == 'rtsp://127.0.0.1:8554/entrance'
        assert config['go2rtc']['streams']['entrance'] == [f'ffmpeg:{stream}#video=h264']


@pytest.mark.parametrize('stream', ['ffmpeg:device?video=0', 'exec:whoami', 'http://host/live#raw=-i/etc/passwd', 'http://host/\nvideo'])
def test_camera_rejects_executable_sources_and_fragments(logged, camera, stream):
    assert logged.post('/api/cameras', json={**camera, 'stream': stream}).status_code == 422


@pytest.mark.parametrize('header,mime,suffix', [
    (b'\x1a\x45\xdf\xa3webm' + b'0'*24, 'video/webm;codecs=vp8', '.webm'),
    (b'\x1a\x45\xdf\xa3matroska' + b'0'*24, 'video/x-matroska', '.mkv'),
    (b'RIFF0000AVI ' + b'0'*24, 'video/x-msvideo', '.avi'),
])
def test_new_upload_formats_queue_playback_and_retention(logged, app, settings, header, mime, suffix):
    r = logged.post('/api/jobs/upload?source=webcam', content=header, headers={'Content-Type': mime})
    assert r.status_code == 202
    job_id = r.json()['id']
    path = settings.data / 'jobs' / (job_id + suffix)
    assert path.read_bytes() == header
    playback = logged.get(f'/api/jobs/{job_id}/video')
    assert playback.headers['content-type'] == mime.split(';')[0]
    assert playback.content == header
    job = app.state.store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
    calls = []
    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'detections': [], 'model': 'test', 'warnings': []})
    app.state.engine.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    asyncio.run(app.state.engine.run_job(job))
    assert calls == ['/analyze']  # No attempt to download this upload from Frigate.
    assert logged.get('/api/jobs').json()[0]['state'] == 'succeeded'
    assert logged.get('/api/jobs').json()[0]['camera'] == 'webcam'
    app.state.store.execute('UPDATE jobs SET updated=? WHERE id=?', (time.time()-90000, job_id))
    app.state.engine.cleanup()
    assert not path.exists()


def test_upload_rejects_forged_container_and_source(logged, settings):
    assert logged.post('/api/jobs/upload', content=b'not a webm video'*3, headers={'Content-Type':'video/webm'}).status_code == 415
    assert logged.post('/api/jobs/upload?source=entrance', content=b'0000ftyp'+b'0'*24, headers={'Content-Type':'video/mp4'}).status_code == 422
    assert not list((settings.data/'jobs').iterdir())
