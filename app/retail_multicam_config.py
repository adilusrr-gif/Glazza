"""Site-scoped commissioning of the three-camera retail review rule."""
import itertools
import json
import math
import time
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .retail import _edges, _intersect
from .schemas import Zone


Identifier = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{1,39}$')]
ROLES = ('shelf', 'checkout', 'exit')


class MulticamRoute(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shelf_camera: Identifier
    shelf_zone: Identifier
    checkout_camera: Identifier
    checkout_zone: Identifier
    exit_camera: Identifier
    exit_zone: Identifier
    min_seconds: int = Field(default=2, ge=0, le=599, strict=True)
    max_seconds: int = Field(default=60, ge=1, le=600, strict=True)

    @model_validator(mode='after')
    def validate_route(self):
        if len({self.shelf_camera, self.checkout_camera, self.exit_camera}) != 3:
            raise ValueError('Выберите три разные камеры для полки, кассы и выхода')
        if self.min_seconds >= self.max_seconds:
            raise ValueError('Максимальное время перехода должно быть больше минимального')
        return self


def initialize(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_multicam_routes (
                site_id TEXT PRIMARY KEY,
                shelf_camera TEXT NOT NULL, shelf_zone TEXT NOT NULL,
                checkout_camera TEXT NOT NULL, checkout_zone TEXT NOT NULL,
                exit_camera TEXT NOT NULL, exit_zone TEXT NOT NULL,
                min_seconds INTEGER NOT NULL, max_seconds INTEGER NOT NULL,
                activated_at REAL NOT NULL, state TEXT NOT NULL, reason TEXT
            );
            CREATE TRIGGER IF NOT EXISTS retail_multicam_change_camera
            AFTER UPDATE OF zones,stream,site_id ON cameras
            WHEN OLD.zones IS NOT NEW.zones OR OLD.stream IS NOT NEW.stream OR OLD.site_id IS NOT NEW.site_id
            BEGIN
                UPDATE retail_multicam_routes SET state='invalidated',reason='camera_changed'
                WHERE OLD.id IN (shelf_camera,checkout_camera,exit_camera);
            END;
            CREATE TRIGGER IF NOT EXISTS retail_multicam_remove_camera AFTER DELETE ON cameras BEGIN
                UPDATE retail_multicam_routes SET state='invalidated',reason='camera_deleted'
                WHERE OLD.id IN (shelf_camera,checkout_camera,exit_camera);
            END;
            CREATE TRIGGER IF NOT EXISTS retail_multicam_change_site AFTER UPDATE OF profile ON sites
            WHEN OLD.profile IS NOT NEW.profile BEGIN
                UPDATE retail_multicam_routes SET state='invalidated',reason='profile_changed' WHERE site_id=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS retail_multicam_remove_site AFTER DELETE ON sites BEGIN
                DELETE FROM retail_multicam_routes WHERE site_id=OLD.id;
            END;
        """)


def site_profile(db, site_id):
    site = db.execute('SELECT profile FROM sites WHERE id=?', (site_id,)).fetchone()
    if site is None:
        raise HTTPException(404, 'Объект не найден')
    return site['profile']


def next_activation(db, site_id, action, previous):
    # A disable removes the configuration row. Reusing its last generation would
    # attach observations from an earlier commissioning after a clock adjustment.
    last = previous['activated_at'] if previous else db.execute(
        'SELECT max(at) FROM audit WHERE subject=? AND action=?', (site_id, action)).fetchone()[0]
    now = time.time()
    return now if last is None else max(now, math.nextafter(last, math.inf))


def validate_camera_zone(db, site_id, camera_id, zone_name):
    camera = db.execute('SELECT zones FROM cameras WHERE id=? AND site_id=?', (camera_id, site_id)).fetchone()
    if camera is None:
        raise HTTPException(404, 'Камера на выбранном объекте не найдена')
    try:
        matches = [z for z in json.loads(camera['zones']) if z['name'] == zone_name]
        if len(matches) != 1:
            raise ValueError()
        zone = Zone.model_validate(matches[0])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, 'Выберите существующую зону камеры с корректным полигоном')
    if zone.restricted:
        raise HTTPException(422, 'Для маршрута нужны зоны со снятой отметкой «Запретная»')
    points = zone.points
    if len(set(points)) != len(points):
        raise HTTPException(422, 'Уберите повтор вершины из границы зоны')
    edges = list(_edges(points))
    for (i, (a, b)), (j, (c, d)) in itertools.combinations(enumerate(edges), 2):
        if i + 1 != j and (i, j) != (0, len(edges)-1) and _intersect(a, b, c, d):
            raise HTTPException(422, 'Граница зоны не должна пересекать саму себя')
    return points


def register(app, store, auth, writable):
    initialize(store)

    @app.get('/api/retail/multicam/{site_id}')
    def get_rule(site_id: str, user=Depends(auth)):
        with store.connect() as db:
            db.execute('BEGIN')
            site_profile(db, site_id)
            row = db.execute('SELECT * FROM retail_multicam_routes WHERE site_id=?', (site_id,)).fetchone()
            return {'rule': dict(row) if row else None}

    @app.put('/api/retail/multicam/{site_id}')
    def configure(site_id: str, body: MulticamRoute, user=Depends(auth)):
        writable()
        config = body.model_dump()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if site_profile(db, site_id) != 'retail':
                raise HTTPException(422, 'Настройка доступна на объекте с профилем магазина')
            for role in ROLES:
                validate_camera_zone(db, site_id, config[role + '_camera'], config[role + '_zone'])
            previous = db.execute('SELECT * FROM retail_multicam_routes WHERE site_id=?', (site_id,)).fetchone()
            if previous and previous['state'] == 'active' and all(previous[k] == v for k, v in config.items()):
                return {'rule': dict(previous)}
            activated = next_activation(db, site_id, 'retail.multicam.configure', previous)
            db.execute("""INSERT INTO retail_multicam_routes
                (site_id,shelf_camera,shelf_zone,checkout_camera,checkout_zone,exit_camera,exit_zone,
                 min_seconds,max_seconds,activated_at,state) VALUES
                (:site_id,:shelf_camera,:shelf_zone,:checkout_camera,:checkout_zone,:exit_camera,:exit_zone,
                 :min_seconds,:max_seconds,:activated_at,'active')
                ON CONFLICT(site_id) DO UPDATE SET
                shelf_camera=excluded.shelf_camera,shelf_zone=excluded.shelf_zone,
                checkout_camera=excluded.checkout_camera,checkout_zone=excluded.checkout_zone,
                exit_camera=excluded.exit_camera,exit_zone=excluded.exit_zone,
                min_seconds=excluded.min_seconds,max_seconds=excluded.max_seconds,
                activated_at=excluded.activated_at,state='active',reason=NULL""",
                dict(config, site_id=site_id, activated_at=activated))
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                       (activated, user['username'], 'retail.multicam.configure', site_id, json.dumps(config)))
            return {'rule': dict(config, site_id=site_id, activated_at=activated, state='active', reason=None)}

    @app.delete('/api/retail/multicam/{site_id}')
    def disable(site_id: str, user=Depends(auth)):
        writable()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site_profile(db, site_id)
            if db.execute('DELETE FROM retail_multicam_routes WHERE site_id=?', (site_id,)).rowcount:
                db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                           (time.time(), user['username'], 'retail.multicam.disable', site_id, ''))
        return {'rule': None}
