import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import threading
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .models import FaceModel, VadClipModel

log = logging.getLogger('glazza.analyzer')


def read_video(path):
    import cv2
    # Disable network protocols while probing a local uploaded container.
    probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height,avg_frame_rate:format=duration', '-of', 'json', str(path)],
                           capture_output=True, timeout=15, check=True)
    metadata = json.loads(probe.stdout)
    duration = float(metadata['format']['duration'])
    stream = metadata['streams'][0]
    if not 0.1 <= duration <= 120 or stream['width'] * stream['height'] > 3840 * 2160:
        raise ValueError('Use video up to 120 seconds and 3840x2160 pixels')
    numerator, denominator = stream['avg_frame_rate'].split('/')
    fps = float(numerator) / float(denominator)
    if not 1 <= fps <= 120:
        raise ValueError('Unsupported frame rate')
    # Decode in ffmpeg with local-only protocols, bounded output and duration.
    # Scale before crossing the pipe to bound memory even for 4K sources.
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(path),
                              '-t', '120', '-vf', "select=not(mod(n\\,16)),scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2", '-vsync', 'vfr',
                              '-frames:v', '900', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
                             capture_output=True, timeout=90, check=True)
    import numpy as np
    frame_size = 640 * 360 * 3
    if not decoded.stdout or len(decoded.stdout) % frame_size:
        raise ValueError('No decodable video frames')
    frames = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, 360, 640, 3)
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
        for name, factory in [('vadclip', VadClipModel), ('face', FaceModel)]:
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

    @app.get('/health')
    def health(x_worker_key: str | None = Header(default=None)):
        authorize(x_worker_key)
        return {'state': 'ready' if models else 'unavailable', 'models': statuses}

    class AnalysisRequest(BaseModel):
        job_id: uuid.UUID

    @app.post('/analyze')
    def analyze(body: AnalysisRequest, x_worker_key: str | None = Header(default=None)):
        authorize(x_worker_key)
        if not models:
            raise HTTPException(503, 'No models loaded')
        if not lock.acquire(blocking=False):
            raise HTTPException(429, 'Analyzer busy')
        try:
            path = jobs / (str(body.job_id) + '.mp4')
            if not path.is_file() or path.stat().st_size > 200 * 1024 * 1024:
                raise HTTPException(404, 'Video missing or too large')
            frames, duration = read_video(path)
            result = {'duration_seconds': duration, 'detections': [], 'face': {}, 'warnings': [], 'model': 'VadCLIP UCF-Crime' if 'vadclip' in models else 'MediaPipe Face Landmarker'}
            if 'vadclip' in models:
                result['detections'] = models['vadclip'].analyze(frames)
                result['warnings'].append('Порог требует настройки на вашем объекте. Качество этого видеоадаптера не принято на независимом корпусе.')
            else:
                result['warnings'].append('Модель действий не загружена: драки и кражи в этом видео не проверялись.')
            if 'face' in models:
                result['face'] = models['face'].analyze(frames)
            else:
                result['warnings'].append('Модель мимики не загружена.')
            return result
        except HTTPException:
            raise
        except Exception as error:
            log.warning('Analysis failed: %s', type(error).__name__)
            raise HTTPException(422, 'Cannot analyze video; use MP4/MOV, <=120s, <=4K')
        finally:
            lock.release()

    return app
