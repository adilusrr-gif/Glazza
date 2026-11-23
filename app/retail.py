"""Opt-in, single-camera review of a recorded route from a shelf to an exit.

Frigate 0.18.2 supplies cumulative zone names and data.path_data, ordered
([normalized bottom-center x, y], frame_time) samples. Neither signal observes
merchandise or a transaction. Only a completed track with a clip is reviewable.
"""
import hashlib
import itertools
import json
import math
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

# Frigate samples movement, not every frame. Stay silent when evidence is sparse.
MAX_GAP_SECONDS = 8
MAX_ROUTE_SECONDS = 60
MIN_ZONE_SECONDS = 0.4
MAX_POINTS = 4096


class Route(BaseModel):
    shelf_zone: str = Field(min_length=1, max_length=40)
    checkout_zone: str = Field(min_length=1, max_length=40)
    exit_zone: str = Field(min_length=1, max_length=40)


def initialize(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_routes(
                camera TEXT PRIMARY KEY, site_id TEXT NOT NULL,
                shelf_zone TEXT NOT NULL, checkout_zone TEXT NOT NULL, exit_zone TEXT NOT NULL,
                activated_at REAL NOT NULL, state TEXT NOT NULL, reason TEXT
            );
            CREATE TRIGGER IF NOT EXISTS retail_camera_delete AFTER DELETE ON cameras BEGIN
                DELETE FROM retail_routes WHERE camera=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS retail_camera_change AFTER UPDATE OF stream,zones,site_id ON cameras
                WHEN OLD.stream!=NEW.stream OR OLD.zones!=NEW.zones OR OLD.site_id!=NEW.site_id BEGIN
                UPDATE retail_routes SET state='invalidated',reason='camera_changed' WHERE camera=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS retail_profile_change AFTER UPDATE OF profile ON sites
                WHEN OLD.profile!=NEW.profile BEGIN
                UPDATE retail_routes SET state='invalidated',reason='profile_changed' WHERE site_id=OLD.id;
            END;
        """)


def _edges(poly):
    return zip(poly, poly[1:] + poly[:1])


def _side(a, b, p):
    value = (b[0]-a[0])*(p[1]-a[1]) - (b[1]-a[1])*(p[0]-a[0])
    return 0 if abs(value) < 1e-10 else (1 if value > 0 else -1)


def _on_segment(a, b, p):
    return _side(a, b, p) == 0 and all(min(a[i], b[i])-1e-10 <= p[i] <= max(a[i], b[i])+1e-10 for i in (0, 1))


def _intersect(a, b, c, d):
    if _side(a, b, c)*_side(a, b, d) < 0 and _side(c, d, a)*_side(c, d, b) < 0:
        return True
    return any(_on_segment(*s) for s in ((a,b,c), (a,b,d), (c,d,a), (c,d,b)))


def _inside(p, poly):
    """Ray casting with the boundary included (conservative for cash registers)."""
    inside = False
    x, y = p
    for a, b in _edges(poly):
        if _on_segment(a, b, p):
            return True
        if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1]) + a[0]:
            inside = not inside
    return inside


def _validate(zones, route):
    names = list(route.model_dump().values())
    if len(set(names)) != 3:
        raise HTTPException(422, 'Назначьте три разные зоны для полки, кассы и выхода')
    available = {z['name']:z for z in zones if not z['restricted']}
    if any(name not in available for name in names):
        raise HTTPException(422, 'Выберите существующие зоны камеры со снятой отметкой «Запретная»')
    polygons = [available[name]['points'] for name in names]
    for polygon in polygons:
        edges = list(_edges(polygon))
        for (i, (a, b)), (j, (c, d)) in itertools.combinations(enumerate(edges), 2):
            if i + 1 != j and (i, j) != (0, len(edges)-1) and _intersect(a, b, c, d):
                raise HTTPException(422, 'Границы каждой зоны должны образовывать простой многоугольник')
    for first, second in itertools.combinations(polygons, 2):
        if (_inside(first[0], second) or _inside(second[0], first)
                or any(_intersect(a,b,c,d) for a,b in _edges(first) for c,d in _edges(second))):
            raise HTTPException(422, 'Оставьте промежуток между зонами полки, кассы и выхода')


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def _route_interval(path, start, end, shelf, checkout, exit_zone):
    if not isinstance(path, list) or not 4 <= len(path) <= MAX_POINTS:
        return None
    previous_point = previous_at = shelf_since = departure = exit_since = finish = None
    for sample in path:
        if not isinstance(sample, (tuple, list)) or len(sample) != 2:
            return None
        point, at = sample
        if (not isinstance(point, (tuple, list)) or len(point) != 2
                or not all(_finite(n) and 0 <= n <= 1 for n in point)
                or not _finite(at) or not start <= at <= end):
            return None
        if previous_at is not None:
            if at == previous_at and point == previous_point:
                continue
            if at <= previous_at:
                return None
            if at-previous_at > MAX_GAP_SECONDS:
                # A missing section could include a visit to the register.
                return None
            if any(_intersect(previous_point, point, a, b) for a,b in _edges(checkout)):
                return None
        if _inside(point, checkout):
            return None
        if _inside(point, shelf):
            shelf_since = at if shelf_since is None else shelf_since
            if at-shelf_since >= MIN_ZONE_SECONDS:
                departure = at
        else:
            shelf_since = None
        if _inside(point, exit_zone):
            exit_since = at if exit_since is None else exit_since
            if departure is not None and at-exit_since >= MIN_ZONE_SECONDS and at-departure <= MAX_ROUTE_SECONDS:
                finish = at if finish is None else finish
        else:
            if finish is not None:
                return None  # Left the confirmed exit area before tracking ended.
            exit_since = None
        previous_point, previous_at = point, at
    if finish is None or end-previous_at > MAX_GAP_SECONDS:
        return None
    return departure, finish


def observe(store, camera, event, origin_event_id, site_id):
    """Called by Frigate ingestion. Replaying an event preserves the operator verdict."""
    start, end = event.get('start_time'), event.get('end_time')
    if (event.get('label') != 'person' or event.get('false_positive') or not event.get('has_clip')
            or not _finite(start) or not _finite(end) or not end > start):
        return None
    rows = store.rows("""SELECT r.*, c.zones FROM retail_routes r JOIN cameras c ON c.id=r.camera
        JOIN sites s ON s.id=c.site_id WHERE r.camera=? AND r.site_id=? AND r.state='active'
        AND r.site_id=c.site_id AND s.profile='retail' AND r.activated_at<=?""", (camera['id'], site_id, start))
    if not rows:
        return None
    rule = rows[0]
    entered = event.get('zones', event.get('entered_zones'))
    if (not isinstance(entered, list) or rule['shelf_zone'] not in entered or rule['exit_zone'] not in entered
            or rule['checkout_zone'] in entered):
        return None
    polygons = {z['name']: z['points'] for z in json.loads(rule['zones'])}
    data = event.get('data')
    interval = _route_interval(data.get('path_data') if isinstance(data, dict) else None, start, end,
                               polygons[rule['shelf_zone']], polygons[rule['checkout_zone']], polygons[rule['exit_zone']])
    if interval is None:
        return None
    left, arrived = interval
    details = {'signal':'shelf_to_exit', 'origin_event':origin_event_id, 'review_required':True,
               'start_seconds':left-start, 'end_seconds':arrived-start,
               **{k:rule[k] for k in ('shelf_zone','checkout_zone','exit_zone')}}
    source_id = f"{camera['id']}:{event['id']}"
    identifier = hashlib.sha256(f'retail_route:{source_id}:theft'.encode()).hexdigest()[:32]
    # Recheck activation in the write transaction in case the operator edited a
    # camera while its path was being evaluated. Keep the first evidence immutable.
    written = store.execute("""INSERT INTO events(id,source_id,source,camera,kind,score,started,ended,details,site_id)
        SELECT ?,?,'retail_route',?,'theft',NULL,?,?,?,site_id FROM retail_routes
        WHERE camera=? AND state='active' AND activated_at=? AND site_id=?
        ON CONFLICT(source,source_id,kind) DO NOTHING""",
        (identifier, source_id, camera['id'], left, arrived, json.dumps(details), camera['id'], rule['activated_at'], site_id))
    return identifier if written else None


def _camera(db, camera_id, site_id):
    row = db.execute('SELECT c.site_id,c.zones,s.profile FROM cameras c JOIN sites s ON s.id=c.site_id WHERE c.id=?',
                     (camera_id,)).fetchone()
    if not row or site_id is not None and row['site_id'] != site_id:
        raise HTTPException(404, 'Камера на этом объекте не найдена')
    return row


def register(app, store, auth, writable):
    @app.get('/api/retail/routes/{camera_id}')
    def get_route(camera_id: str, site_id: str | None = None, user=Depends(auth)):
        with store.connect() as db:
            db.execute('BEGIN')
            _camera(db, camera_id, site_id)
            row = db.execute('SELECT * FROM retail_routes WHERE camera=?', (camera_id,)).fetchone()
        return {'rule':dict(row) if row else None}

    @app.put('/api/retail/routes/{camera_id}')
    def configure(camera_id: str, body: Route, site_id: str | None = None, user=Depends(auth)):
        writable()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cam = _camera(db, camera_id, site_id)
            if cam['profile'] != 'retail':
                raise HTTPException(422, 'Правило маршрута доступно на объекте с профилем магазина')
            _validate(json.loads(cam['zones']), body)
            now = time.time()
            record = body.model_dump()
            db.execute("""INSERT INTO retail_routes(camera,site_id,shelf_zone,checkout_zone,exit_zone,activated_at,state)
                VALUES(?,?,?,?,?,?,'active') ON CONFLICT(camera) DO UPDATE SET site_id=excluded.site_id,
                shelf_zone=excluded.shelf_zone,checkout_zone=excluded.checkout_zone,exit_zone=excluded.exit_zone,
                activated_at=excluded.activated_at,state='active',reason=NULL
                WHERE state!='active' OR site_id!=excluded.site_id OR shelf_zone!=excluded.shelf_zone
                OR checkout_zone!=excluded.checkout_zone OR exit_zone!=excluded.exit_zone""",
                (camera_id, cam['site_id'], body.shelf_zone, body.checkout_zone, body.exit_zone, now))
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                       (now, user['username'], 'retail.route.configure', camera_id, json.dumps(record)))
            row = db.execute('SELECT * FROM retail_routes WHERE camera=?', (camera_id,)).fetchone()
        return {'rule':dict(row)}

    @app.delete('/api/retail/routes/{camera_id}')
    def disable(camera_id: str, site_id: str | None = None, user=Depends(auth)):
        writable()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _camera(db, camera_id, site_id)
            if db.execute('DELETE FROM retail_routes WHERE camera=?', (camera_id,)).rowcount:
                db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                           (time.time(), user['username'], 'retail.route.disable', camera_id, ''))
        return {'rule':None}
