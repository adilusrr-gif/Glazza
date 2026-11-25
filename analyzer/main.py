import asyncio
from contextlib import asynccontextmanager
import json
import logging
import math
import os
import re
from pathlib import Path
import secrets
import subprocess
import threading
import uuid
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .models import FaceModel, VadClipModel, MAX_SAMPLE_GAP_SECONDS, VADCLIP_NAME
from .fight import FightModel
from .identity import IdentityModel, register as register_identity

log = logging.getLogger('glazza.analyzer')


def read_video(path, profile='default', *, include_times=False):
    # Disable network protocols while probing a local uploaded container.
    probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height,avg_frame_rate,duration:format=duration', '-of', 'json', str(path)],
                           capture_output=True, timeout=15, check=True)
    metadata = json.loads(probe.stdout)
    stream = metadata['streams'][0]
    duration = float(stream.get('duration') or metadata.get('format', {}).get('duration', 0))
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
    if profile == 'fight':
        sampling, limit = 'fps=8,scale=172:172:flags=area', 960
    else:
        resize = 'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2'
        if profile == 'retail':
            # About one frame per 16 frames at 30 fps. Select originals without inventing
            # duplicate observations on low-FPS streams. Account for PTS rounding.
            select = "select='isnan(prev_selected_t)+gte(t-prev_selected_t,16/30-0.000001)'"
            limit = 226
        else:
            select, limit = "select='not(mod(n,16))'", 900
        sampling = f'setpts=PTS-STARTPTS,{select},{resize}'
    if include_times:
        sampling += ',showinfo=checksum=0'
    decoded = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-v', 'info' if include_times else 'error',
                              '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0',
                              '-t', '120', '-vf', sampling, '-vsync', 'vfr', '-frames:v', str(limit),
                              '-threads:v', '1', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
                             capture_output=True, timeout=90, check=True)
    import numpy as np
    frame_size = width * height * 3
    if not decoded.stdout or len(decoded.stdout) % frame_size:
        raise ValueError('No decodable video frames')
    frames = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, height, width, 3)
    if include_times:
        sample_times = [float(pts) for pts in re.findall(rb'\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([^\s]+)', decoded.stderr)]
        if (len(sample_times) != len(frames) or any(not math.isfinite(t) or not 0 <= t < duration for t in sample_times)
                or any(b <= a for a, b in zip(sample_times, sample_times[1:]))):
            raise ValueError('Cannot align decoded frames with video timestamps')
        return list(frames), duration, sample_times
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
        modules: list[Literal['fight','theft','face']] | None = None

    @app.post('/analyze')
    def analyze(body: AnalysisRequest, x_worker_key: str | None = Header(default=None)):
        authorize(x_worker_key)
        requested = set(body.modules) if body.modules is not None else {'fight','theft','face'}
        if body.mode == 'fight': requested &= {'fight'}
        if not requested:
            raise HTTPException(422, 'No analysis modules enabled')
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
            if 'fight' in models and 'fight' in requested:
                fight_frames, duration = read_video(path, 'fight')
                result['fight'] = models['fight'].analyze(fight_frames)
                result['detections'].extend(result['fight']['detections'])
                result['checked'].append('fight')
                names.append('VD-MIL / MoViNet-A0')
                result['warnings'].append('Сигнал возможной драки/насилия требует просмотра оператором; модель также обучена на стрельбе и не разделяет эти действия. Порог не откалиброван для колледжа.')
                if result['fight']['windows_analyzed'] < models['fight'].consecutive:
                    result['warnings'].append('Недостаточно окон для подтверждения сигнала драки: загрузите более длинное видео.')
            if 'vadclip' in models and (('theft' in requested) or ('fight' in requested and 'fight' not in models)):
                profile = 'retail' if 'theft' in requested else 'default'
                frames, duration, times = read_video(path, profile, include_times=True)
                model = models['vadclip']
                detections = model.analyze(frames, times, duration)
                result['detections'].extend(d for d in detections if d['kind'] in requested and (d['kind'] != 'fight' or 'fight' not in models))
                if 'fight' in requested:
                    result['checked'].append('fight')
                if 'theft' in requested:
                    gaps = [b - a for a, b in zip(times, times[1:] + [duration])]
                    if len(times) < model.theft_samples:
                        status = 'insufficient_samples'
                    elif max(gaps) > MAX_SAMPLE_GAP_SECONDS:
                        status = 'incomplete_coverage'
                    else:
                        status = 'checked'
                    result['theft'] = {'status': status, 'threshold': model.theft_threshold,
                                       'required_samples': model.theft_samples, 'samples_analyzed': len(times)}
                    if status == 'checked':
                        result['checked'].append('theft')
                    else:
                        result['warnings'].append('Для полной проверки возможной кражи недостаточно последовательных кадров.')
                names.append(VADCLIP_NAME)
                result['warnings'].append('Сигнал модели требует просмотра оператором. Порог настраивается по размеченным записям объекта.')
            elif body.mode == 'all' and 'fight' not in models and requested & {'fight','theft'}:
                result['warnings'].append('Модель действий не загружена: драки и кражи в этом видео не проверялись.')
            elif body.mode == 'all' and 'theft' in requested:
                result['warnings'].append('VadCLIP не загружен: возможная кража в этом видео не проверялась.')
            if 'theft' in requested and 'vadclip' not in models:
                result['theft'] = {'status': 'model_missing'}
            if 'face' in models and 'face' in requested:
                if frames is None:
                    frames, duration = read_video(path)
                result['face'] = models['face'].analyze(frames)
                result['checked'].append('face')
                names.append('MediaPipe Face Landmarker')
            elif 'face' in requested:
                result['warnings'].append('Модель мимики не загружена.')
            if not result['checked'] and result.get('theft', {}).get('status') not in ('insufficient_samples', 'incomplete_coverage'):
                raise HTTPException(503, 'Requested models are not loaded')
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
