import asyncio
from contextlib import asynccontextmanager
import json
import logging
import math
import os
from pathlib import Path
import secrets
import subprocess
import threading
import uuid
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .models import FaceModel, VadClipModel
from .fight import FightModel
from .identity import IdentityModel, register as register_identity

log = logging.getLogger('glazza.analyzer')


def read_video(path, profile='default'):
    # Disable network protocols while probing a local uploaded container.
    probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height,avg_frame_rate:format=duration', '-of', 'json', str(path)],
                           capture_output=True, timeout=15, check=True)
    metadata = json.loads(probe.stdout)
    stream = metadata['streams'][0]
    duration = float(metadata.get('format', {}).get('duration', 0))
    # MediaRecorder WebM commonly has no duration or average frame rate.
    # Inspect bounded packet timestamps, without decoding or trusting client metadata.
    packet_fps = None
    if not duration:
        packets = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                  '-select_streams', 'v:0', '-read_intervals', '%+121',
                                  '-show_entries', 'packet=pts_time,duration_time', '-of', 'json', str(path)],
                                 capture_output=True, timeout=15, check=True)
        stamps = [(float(p['pts_time']), float(p.get('duration_time', 0)))
                  for p in json.loads(packets.stdout).get('packets', []) if 'pts_time' in p]
        if len(stamps) < 2:
            raise ValueError('Video contains too few timed frames')
        start = min(p[0] for p in stamps)
        duration = max(p[0] + p[1] for p in stamps) - start
        packet_fps = len(stamps) / duration if duration > 0 else 0
    if not 0.1 <= duration <= 120 or stream['width'] * stream['height'] > 3840 * 2160:
        raise ValueError('Use video up to 120 seconds and 3840x2160 pixels')
    numerator, denominator = stream['avg_frame_rate'].split('/')
    fps = float(numerator) / float(denominator) if float(denominator) and float(numerator) else (packet_fps or 0)
    if not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError('Unsupported frame rate')
    # Decode in ffmpeg with local-only protocols, bounded output and duration.
    # Scale before crossing the pipe to bound memory even for 4K sources.
    width, height = (172, 172) if profile == 'fight' else (640, 360)
    sampling = 'fps=8,scale=172:172:flags=area' if profile == 'fight' else "select=not(mod(n\\,16)),scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2"
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(path),
                              '-t', '120', '-vf', sampling, '-vsync', 'vfr',
                              '-frames:v', '960' if profile == 'fight' else '900', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
                             capture_output=True, timeout=90, check=True)
    import numpy as np
    frame_size = width * height * 3
    if not decoded.stdout or len(decoded.stdout) % frame_size:
        raise ValueError('No decodable video frames')
    frames = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, height, width, 3)
    return list(frames), duration


def create_app():
    key = os.getenv('GLAZZA_WORKER_KEY', '')
    if len(key) < 32:
        raise RuntimeError('Set a strong GLAZZA_WORKER_KEY')
    root = Path(os.getenv('GLAZZA_MODELS', '/models'))
    jobs = Path(os.getenv('GLAZZA_DATA', '/data')) / 'jobs'
    models, statuses = {}, []
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        for name, factory in [('fight', FightModel), ('vadclip', VadClipModel), ('face', FaceModel), ('identity', IdentityModel)]:
            try:
                models[name] = await asyncio.to_thread(factory, root)
                statuses.append({'id': name, 'state': 'ready'})
            except FileNotFoundError:
                statuses.append({'id': name, 'state': 'missing'})
            except Exception as error:
                statuses.append({'id': name, 'state': 'error'})
                log.warning('%s initialization failed: %s', name, type(error).__name__)
        yield
        if 'face' in models:
            models['face'].model.close()

    app = FastAPI(title='Glazza analyzer', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(value):
        if not value or not secrets.compare_digest(value, key):
            raise HTTPException(401)

    register_identity(app, models, authorize)

    @app.get('/health')
    def health(x_worker_key: str | None = Header(default=None)):
        authorize(x_worker_key)
        return {'state': 'ready' if models else 'unavailable', 'models': statuses}

    class AnalysisRequest(BaseModel):
        job_id: uuid.UUID
        mode: Literal['all', 'fight'] = 'all'

    @app.post('/analyze')
    def analyze(body: AnalysisRequest, x_worker_key: str | None = Header(default=None)):
        authorize(x_worker_key)
        if not ({'fight','vadclip','face'} & models.keys()):
            raise HTTPException(503, 'No models loaded')
        if body.mode == 'fight' and not ({'fight', 'vadclip'} & models.keys()):
            raise HTTPException(503, 'Fight model is not loaded')
        if not lock.acquire(blocking=False):
            raise HTTPException(429, 'Analyzer busy')
        try:
            path = next((jobs / (str(body.job_id) + suffix) for suffix in ('.mp4', '.webm', '.mkv', '.avi')
                         if (jobs / (str(body.job_id) + suffix)).is_file()), None)
            if path is None or path.stat().st_size > 200 * 1024 * 1024:
                raise HTTPException(404, 'Video missing or too large')
            result = {'detections': [], 'face': {}, 'warnings': [], 'model': '', 'checked': []}
            names = []
            frames = None
            if 'fight' in models:
                fight_frames, duration = read_video(path, 'fight')
                result['fight'] = models['fight'].analyze(fight_frames)
                result['detections'].extend(result['fight']['detections'])
                result['checked'].append('fight')
                names.append('VD-MIL / MoViNet-A0')
                result['warnings'].append('Сигнал возможной драки/насилия требует просмотра оператором; модель также обучена на стрельбе и не разделяет эти действия. Порог не откалиброван для колледжа.')
                if result['fight']['windows_analyzed'] < models['fight'].consecutive:
                    result['warnings'].append('Недостаточно окон для подтверждения сигнала драки: загрузите более длинное видео.')
            if 'vadclip' in models and (body.mode == 'all' or 'fight' not in models):
                frames, duration = read_video(path)
                detections = models['vadclip'].analyze(frames)
                result['detections'].extend(d for d in detections if (body.mode == 'all' or d['kind'] == 'fight') and (d['kind'] != 'fight' or 'fight' not in models))
                result['checked'].extend(['fight', 'theft'] if body.mode == 'all' else ['fight'])
                names.append('VadCLIP UCF-Crime')
                result['warnings'].append('Порог требует настройки на вашем объекте. Качество этого видеоадаптера не принято на независимом корпусе.')
            elif body.mode == 'all' and 'fight' not in models:
                result['warnings'].append('Модель действий не загружена: драки и кражи в этом видео не проверялись.')
            elif body.mode == 'all':
                result['warnings'].append('VadCLIP не загружен: возможная кража в этом видео не проверялась.')
            if 'face' in models and body.mode == 'all':
                if frames is None:
                    frames, duration = read_video(path)
                result['face'] = models['face'].analyze(frames)
                result['checked'].append('face')
                names.append('MediaPipe Face Landmarker')
            elif body.mode == 'all':
                result['warnings'].append('Модель мимики не загружена.')
            result.update(duration_seconds=duration, model=' + '.join(names), checked=sorted(set(result['checked'])))
            return result
        except HTTPException:
            raise
        except Exception as error:
            log.warning('Analysis failed: %s', type(error).__name__)
            raise HTTPException(422, 'Cannot analyze video; use MP4/MOV/WebM/MKV/AVI, <=120s, <=4K')
        finally:
            lock.release()

    return app
