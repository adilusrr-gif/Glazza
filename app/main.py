import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .engine import Engine, seed_demo
from .frigate import atomic_config, validate_names
from .schemas import Camera, Login, Review, ZoneUpdate
from .store import Store, password_hash, password_matches

STATIC = Path(__file__).parent / 'static'


def create_app(settings=None, background=True):
    settings = settings or Settings.from_env()
    if len(settings.password) < 12:
        raise RuntimeError('Set GLAZZA_PASSWORD to at least 12 characters; run python scripts/setup.py')
    if len(settings.worker_key) < 32:
        raise RuntimeError('GLAZZA_WORKER_KEY must contain at least 32 characters')
    if not 1 <= settings.retention_days <= 3650:
        raise RuntimeError('GLAZZA_RETENTION_DAYS must be 1..3650')
    store = Store(settings.data)
    (settings.data / 'jobs').mkdir(exist_ok=True, mode=0o700)
    mode = 'demo' if settings.demo else 'live'
    previous = store.rows("SELECT value FROM meta WHERE key='mode'")
    if previous and previous[0]['value'] != mode:
        raise RuntimeError('Demo and live require separate GLAZZA_DATA directories')
    store.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('mode',?)", (mode,))
    store.execute('INSERT OR IGNORE INTO users(name,password) VALUES(?,?)', (settings.username, password_hash(settings.password)))
    if settings.demo:
        seed_demo(store)
    engine = Engine(store, settings)
    attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        if background:
            await engine.start()
        yield
        await engine.close()

    app = FastAPI(title='Glazza', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine, app.state.settings = store, engine, settings

    @app.middleware('http')
    async def secure_headers(request, call_next):
        # Reject cross-origin writes even before parsing multipart/form bodies.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo RTSP passwords or login input in Pydantic error responses.
        return JSONResponse({'detail': [{'loc': list(e['loc']), 'msg': e['msg']} for e in exc.errors()]}, status_code=422)

    def authenticated(request: Request):
        token = request.cookies.get('glazza_session', '')
        digest = hashlib.sha256(token.encode()).hexdigest()
        sessions = store.rows('SELECT * FROM sessions WHERE token=? AND expires>?', (digest, time.time()))
        if not sessions:
            raise HTTPException(401, 'Войдите в Glazza')
        session = sessions[0]
        if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            raise HTTPException(403, 'Сессия устарела. Обновите страницу')
        return session

    def writable():
        if settings.demo:
            raise HTTPException(409, 'Настройка оборудования недоступна в демонстрации')

    def check_camera_names(candidate):
        cameras = [camera for camera in store.cameras() if camera['id'] != candidate['id']]
        try:
            validate_names([*cameras, candidate])
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.get('/healthz')
    def health():
        return {'status': 'ok', 'version': '0.1.0'}

    @app.post('/api/login')
    def login(body: Login, request: Request, response: Response):
        now = time.time()
        ip = request.client.host if request.client else 'unknown'
        for key in list(attempts):
            if attempts[key][1] < now - 300:
                attempts.pop(key, None)
        count, last = attempts.get(ip, (0, now))
        if count >= 10:
            raise HTTPException(429, 'Слишком много попыток. Подождите 5 минут')
        attempts[ip] = (count + 1, now)
        users = store.rows('SELECT password FROM users WHERE name=?', (body.username,))
        # A constant-cost hash is also performed for unknown users.
        encoded = users[0]['password'] if users else password_hash('invalid-user-placeholder')
        if not password_matches(body.password, encoded) or not users:
            raise HTTPException(401, 'Неверный логин или пароль')
        attempts.pop(ip, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        store.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), body.username, csrf, now + 43200))
        response.set_cookie('glazza_session', token, httponly=True, secure=settings.secure_cookie, samesite='strict', max_age=43200)
        store.audit(body.username, 'login', 'session')
        return {'username': body.username, 'csrf': csrf, 'demo': settings.demo}

    @app.get('/api/session')
    def session(user=Depends(authenticated)):
        return {'username': user['username'], 'csrf': user['csrf'], 'demo': settings.demo}

    @app.post('/api/logout')
    def logout(response: Response, user=Depends(authenticated)):
        store.execute('DELETE FROM sessions WHERE token=?', (user['token'],))
        response.delete_cookie('glazza_session')
        return {'ok': True}

    @app.get('/api/overview')
    def overview(user=Depends(authenticated)):
        counts = {r['kind']: r['n'] for r in store.rows('SELECT kind,count(*) AS n FROM events WHERE started>? GROUP BY kind', (time.time() - 86400,))}
        return {'mode': mode, 'cameras': len(store.cameras()), 'counts': counts,
                'pending_fights': store.rows("SELECT count(*) AS n FROM events WHERE status='new' AND kind='fight'")[0]['n'],
                'pending': store.rows("SELECT count(*) AS n FROM events WHERE status='new' AND kind!='person'")[0]['n'],
                'health': engine.health, 'jobs': store.rows('SELECT state,count(*) AS n FROM jobs GROUP BY state'),
                'retention_days': settings.retention_days}

    @app.get('/api/cameras')
    def cameras(user=Depends(authenticated)):
        return store.cameras()

    @app.post('/api/cameras', status_code=201)
    def add_camera(body: Camera, user=Depends(authenticated)):
        writable()
        if store.rows('SELECT id FROM cameras WHERE id=?', (body.id,)):
            raise HTTPException(409, 'Такой идентификатор камеры уже существует')
        check_camera_names(body.model_dump())
        store.execute('INSERT INTO cameras(id,name,stream,zones,analyze) VALUES(?,?,?,?,?)',
                      (body.id, body.name, body.stream, json.dumps([z.model_dump() for z in body.zones]), int(body.analyze)))
        store.audit(user['username'], 'camera.create', body.id)
        return {'id': body.id, 'apply_required': True}

    @app.put('/api/cameras/{camera_id}')
    def update_camera(camera_id: str, body: Camera, user=Depends(authenticated)):
        writable()
        if body.id != camera_id:
            raise HTTPException(422, 'Идентификатор нельзя изменить')
        check_camera_names(body.model_dump())
        changed = store.execute('UPDATE cameras SET name=?,stream=?,zones=?,analyze=? WHERE id=?',
                                (body.name, body.stream, json.dumps([z.model_dump() for z in body.zones]), int(body.analyze), camera_id))
        if not changed:
            raise HTTPException(404, 'Камера не найдена')
        store.audit(user['username'], 'camera.update', camera_id)
        return {'apply_required': True}

    @app.put('/api/cameras/{camera_id}/zones')
    def update_zones(camera_id: str, body: ZoneUpdate, user=Depends(authenticated)):
        writable()
        names = [z.name for z in body.zones]
        if len(names) != len(set(names)) or camera_id in names:
            raise HTTPException(422, 'Имена зон должны быть уникальны и отличаться от камеры')
        check_camera_names({'id': camera_id, 'zones': [zone.model_dump() for zone in body.zones]})
        if not store.execute('UPDATE cameras SET zones=? WHERE id=?', (json.dumps([z.model_dump() for z in body.zones]), camera_id)):
            raise HTTPException(404, 'Камера не найдена')
        store.audit(user['username'], 'camera.zones', camera_id)
        return {'apply_required': True}

    @app.delete('/api/cameras/{camera_id}')
    def delete_camera(camera_id: str, user=Depends(authenticated)):
        writable()
        if not store.execute('DELETE FROM cameras WHERE id=?', (camera_id,)):
            raise HTTPException(404, 'Камера не найдена')
        store.execute('UPDATE events SET ended=? WHERE camera=? AND ended IS NULL', (time.time(), camera_id))
        store.audit(user['username'], 'camera.delete', camera_id)
        return {'apply_required': True}

    @app.post('/api/config/apply')
    async def apply_config(user=Depends(authenticated)):
        writable()
        atomic_config(settings.frigate_config, store.cameras(private=True))
        store.audit(user['username'], 'config.apply', 'frigate')
        try:
            response = await engine.client.post(settings.frigate_url + '/api/restart')
            response.raise_for_status()
        except httpx.HTTPError:
            return JSONResponse({'saved': True, 'restarted': False, 'detail': 'Конфигурация сохранена. Запустите или перезапустите контейнер Frigate.'}, status_code=202)
        return {'saved': True, 'restarted': True}

    @app.get('/api/events')
    def events(status: str | None = None, kind: str | None = None, camera: str | None = None, limit: int = 100, user=Depends(authenticated)):
        return store.events(status, kind, camera, max(1, min(limit, 500)))

    @app.patch('/api/events/{event_id}')
    def review(event_id: str, body: Review, user=Depends(authenticated)):
        with store.connect() as db:
            changed = db.execute('UPDATE events SET status=?,note=? WHERE id=?', (body.status, body.note, event_id)).rowcount
            if not changed:
                raise HTTPException(404, 'Событие не найдено')
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                       (time.time(), user['username'], 'event.review', event_id, json.dumps(body.model_dump(), ensure_ascii=False)))
        return {'ok': True}

    @app.get('/api/audit')
    def audit(user=Depends(authenticated)):
        return store.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 200')

    @app.get('/api/jobs')
    def jobs(user=Depends(authenticated)):
        items = store.rows('SELECT id,camera,state,created,updated,error,result,file,clip_start,clip_end FROM jobs ORDER BY created DESC LIMIT 100')
        for item in items:
            item['result'] = json.loads(item['result']) if item['result'] else None
            item['has_file'] = bool(item.pop('file'))
        return items

    @app.post('/api/jobs/upload', status_code=202)
    async def upload(request: Request, user=Depends(authenticated)):
        writable()
        if store.rows("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','running')")[0]['n'] >= 100:
            raise HTTPException(429, 'Очередь заполнена')
        # Raw request streaming avoids buffering multipart uploads before enforcing the limit.
        if request.headers.get('content-type', '').split(';')[0] not in ('video/mp4', 'video/quicktime', 'application/octet-stream'):
            raise HTTPException(415, 'Загрузите MP4 или MOV')
        job_id = str(uuid.uuid4())
        path = settings.data / 'jobs' / (job_id + '.mp4')
        size = 0
        try:
            with path.open('wb') as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload:
                        raise HTTPException(413, 'Максимальный размер — 200 МБ')
                    handle.write(chunk)
            if size < 16:
                raise HTTPException(422, 'Видео пустое или повреждено')
            with path.open('rb') as handle:
                header = handle.read(32)
            if b'ftyp' not in header:
                raise HTTPException(415, 'Поддерживаются контейнеры MP4 и MOV')
            store.execute('INSERT INTO jobs(id,camera,state,created,updated,file) VALUES(?,?,?,?,?,?)',
                          (job_id, 'upload', 'queued', time.time(), time.time(), path.name))
            store.audit(user['username'], 'video.upload', job_id)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {'id': job_id, 'state': 'queued'}

    @app.post('/api/jobs/{job_id}/retry')
    def retry(job_id: str, user=Depends(authenticated)):
        if not store.execute("UPDATE jobs SET state='queued',attempts=0,updated=?,error=NULL WHERE id=? AND state='failed' AND file IS NOT NULL", (time.time(), job_id)):
            raise HTTPException(409, 'Повтор доступен для неудачного задания с сохранённым видео')
        store.audit(user['username'], 'analysis.retry', job_id)
        return {'ok': True}

    @app.get('/api/jobs/{job_id}/video')
    def job_video(job_id: str, user=Depends(authenticated)):
        rows = store.rows('SELECT file FROM jobs WHERE id=?', (job_id,))
        if not rows or not rows[0]['file']:
            raise HTTPException(404, 'Видео удалено по сроку хранения')
        path = settings.data / 'jobs' / rows[0]['file']
        if not path.is_file():
            raise HTTPException(404, 'Видео недоступно')
        return FileResponse(path, media_type='video/mp4')

    @app.get('/api/cameras/{camera_id}/snapshot')
    async def snapshot(camera_id: str, user=Depends(authenticated)):
        if settings.demo:
            raise HTTPException(404, 'В демонстрации нет живых камер')
        if not store.rows('SELECT id FROM cameras WHERE id=?', (camera_id,)):
            raise HTTPException(404, 'Камера не найдена')
        try:
            response = await engine.client.get(settings.frigate_url + f'/api/{camera_id}/latest.jpg', params={'h': 640})
            response.raise_for_status()
            return Response(response.content, media_type='image/jpeg')
        except httpx.HTTPError:
            raise HTTPException(503, 'Камера недоступна')

    @app.get('/api/events/{event_id}/media/{kind}')
    async def event_media(event_id: str, kind: str, request: Request, user=Depends(authenticated)):
        if kind not in ('snapshot', 'clip'):
            raise HTTPException(404)
        rows = store.rows('SELECT * FROM events WHERE id=?', (event_id,))
        if not rows:
            raise HTTPException(404)
        event = rows[0]
        if event['source'] == 'analysis':
            details = json.loads(event['details'])
            if kind == 'clip':
                return job_video(details['job_id'], user)
            origin = store.rows('SELECT * FROM events WHERE id=?', (details.get('origin_event'),))
            if not origin:
                raise HTTPException(404)
            event = origin[0]
        if event['source'] != 'frigate':
            raise HTTPException(404, 'В демонстрации запись не создаётся')
        suffix = 'snapshot.jpg' if kind == 'snapshot' else 'clip.mp4'
        headers = {}
        if request.headers.get('range'):
            headers['Range'] = request.headers['range']
        upstream_request = engine.client.build_request('GET', settings.frigate_url + f"/api/events/{event['source_id']}/{suffix}", headers=headers, timeout=60)
        try:
            upstream = await engine.client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            raise HTTPException(503, 'Архив недоступен')
        if upstream.status_code not in (200, 206):
            await upstream.aclose()
            raise HTTPException(404, 'Запись ещё не готова или срок хранения истёк')
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
        headers = {h: upstream.headers[h] for h in ('content-range', 'accept-ranges', 'content-length') if h in upstream.headers}
        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers, media_type='image/jpeg' if kind == 'snapshot' else 'video/mp4')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    return app
