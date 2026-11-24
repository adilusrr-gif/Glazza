"""Commission an entrance and the directed paths between a shop's cameras."""
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .retail import _edges, _inside, _intersect
from .retail_multicam_config import Identifier, next_activation, site_profile, validate_camera_zone


class Transition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_camera: Identifier
    to_camera: Identifier
    min_seconds: int = Field(default=2, ge=0, le=599, strict=True)
    max_seconds: int = Field(default=60, ge=1, le=600, strict=True)

    @model_validator(mode='after')
    def validate_window(self):
        if self.from_camera == self.to_camera or self.min_seconds >= self.max_seconds:
            raise ValueError('Выберите разные камеры и возрастающие границы времени')
        return self


class VisitConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entrance_camera: Identifier
    entrance_zone: Identifier
    inside_zone: Identifier | None = None
    camera_ids: list[Identifier] = Field(min_length=2, max_length=64)
    transitions: list[Transition] = Field(min_length=1, max_length=4032)

    @model_validator(mode='after')
    def validate_graph(self):
        cameras = set(self.camera_ids)
        if len(cameras) != len(self.camera_ids) or cameras & {'upload','webcam'}:
            raise ValueError('Выберите разные зарегистрированные камеры')
        if self.entrance_camera not in cameras or self.entrance_zone in cameras:
            raise ValueError('Включите входную камеру в список и выберите отдельную зону входа')
        if self.inside_zone is not None and (self.inside_zone in cameras or self.inside_zone == self.entrance_zone):
            raise ValueError('Укажите отдельную зону внутри магазина')
        edges = {camera: set() for camera in cameras}
        for step in self.transitions:
            if step.from_camera not in cameras or step.to_camera not in cameras:
                raise ValueError('Обе камеры каждого перехода должны входить в выбранный список')
            if step.to_camera in edges[step.from_camera]:
                raise ValueError('Прямой переход между этими камерами уже указан')
            edges[step.from_camera].add(step.to_camera)
        seen = {self.entrance_camera}
        pending = [self.entrance_camera]
        while pending:
            camera = pending.pop()
            following = edges[camera] - seen
            pending.extend(following)
            seen.update(following)
        if seen != cameras:
            raise ValueError('У каждой камеры должен быть направленный маршрут от входа')
        return self


def initialize(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_visit_configs (
                site_id TEXT PRIMARY KEY, entrance_camera TEXT NOT NULL, entrance_zone TEXT NOT NULL, inside_zone TEXT,
                camera_ids TEXT NOT NULL, transitions TEXT NOT NULL,
                activated_at REAL NOT NULL, state TEXT NOT NULL, reason TEXT
            );
            CREATE TRIGGER IF NOT EXISTS visit_config_camera_update AFTER UPDATE OF stream,zones,site_id ON cameras
            WHEN OLD.stream IS NOT NEW.stream OR OLD.zones IS NOT NEW.zones OR OLD.site_id IS NOT NEW.site_id BEGIN
                UPDATE retail_visit_configs SET state='invalidated',reason='camera_changed'
                WHERE OLD.id IN (SELECT value FROM json_each(camera_ids));
            END;
            CREATE TRIGGER IF NOT EXISTS visit_config_camera_delete AFTER DELETE ON cameras BEGIN
                UPDATE retail_visit_configs SET state='invalidated',reason='camera_deleted'
                WHERE OLD.id IN (SELECT value FROM json_each(camera_ids));
            END;
            CREATE TRIGGER IF NOT EXISTS visit_config_profile_update AFTER UPDATE OF profile ON sites
            WHEN OLD.profile IS NOT NEW.profile BEGIN
                UPDATE retail_visit_configs SET state='invalidated',reason='profile_changed' WHERE site_id=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS visit_config_site_delete AFTER DELETE ON sites BEGIN
                DELETE FROM retail_visit_configs WHERE site_id=OLD.id;
            END;
        """)
        db.execute('BEGIN IMMEDIATE')
        if 'inside_zone' not in {row['name'] for row in db.execute('PRAGMA table_info(retail_visit_configs)')}:
            db.execute('ALTER TABLE retail_visit_configs ADD COLUMN inside_zone TEXT')


def decode(row):
    if row is None:
        return None
    return {**dict(row), 'camera_ids':json.loads(row['camera_ids']), 'transitions':json.loads(row['transitions'])}


def register(app, store, auth, writable):
    initialize(store)

    @app.get('/api/retail/visits/{site_id}/config')
    def get_config(site_id: str, user=Depends(auth)):
        with store.connect() as db:
            db.execute('BEGIN')
            site_profile(db, site_id)
            row = db.execute('SELECT * FROM retail_visit_configs WHERE site_id=?', (site_id,)).fetchone()
            return {'config': decode(row)}

    @app.put('/api/retail/visits/{site_id}/config')
    def configure(site_id: str, body: VisitConfig, user=Depends(auth)):
        writable()
        # Order in a form does not change the graph. Retries and reordered lists retain the generation.
        cameras = sorted(body.camera_ids)
        edges = sorted(body.model_dump()['transitions'], key=lambda step: (step['from_camera'], step['to_camera']))
        config = dict(entrance_camera=body.entrance_camera, entrance_zone=body.entrance_zone, inside_zone=body.inside_zone,
                      camera_ids=json.dumps(cameras), transitions=json.dumps(edges))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if site_profile(db, site_id) != 'retail':
                raise HTTPException(422, 'Настройка доступна на объекте с профилем магазина')
            registered = {row[0] for row in db.execute('SELECT id FROM cameras WHERE site_id=?', (site_id,))}
            if not set(cameras) <= registered:
                raise HTTPException(404, 'Камера на выбранном объекте не найдена')
            entrance = validate_camera_zone(db, site_id, body.entrance_camera, body.entrance_zone)
            if body.inside_zone:
                inside = validate_camera_zone(db, site_id, body.entrance_camera, body.inside_zone)
                if (_inside(entrance[0], inside) or _inside(inside[0], entrance)
                        or any(_intersect(a,b,c,d) for a,b in _edges(entrance) for c,d in _edges(inside))):
                    raise HTTPException(422, 'Оставьте промежуток между зоной входа и внутренней зоной')
            previous = db.execute('SELECT * FROM retail_visit_configs WHERE site_id=?', (site_id,)).fetchone()
            if previous and previous['state'] == 'active' and all(previous[k] == v for k,v in config.items()):
                return {'config': decode(previous)}
            activated = next_activation(db, site_id, 'retail.visits.configure', previous)
            db.execute("""INSERT INTO retail_visit_configs
                (site_id,entrance_camera,entrance_zone,inside_zone,camera_ids,transitions,activated_at,state)
                VALUES (:site_id,:entrance_camera,:entrance_zone,:inside_zone,:camera_ids,:transitions,:activated_at,'active')
                ON CONFLICT(site_id) DO UPDATE SET entrance_camera=excluded.entrance_camera,
                entrance_zone=excluded.entrance_zone,inside_zone=excluded.inside_zone,camera_ids=excluded.camera_ids,transitions=excluded.transitions,
                activated_at=excluded.activated_at,state='active',reason=NULL""",
                dict(config, site_id=site_id, activated_at=activated))
            row = db.execute('SELECT * FROM retail_visit_configs WHERE site_id=?', (site_id,)).fetchone()
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                       (activated, user['username'], 'retail.visits.configure', site_id,
                        json.dumps(dict(entrance_camera=body.entrance_camera, entrance_zone=body.entrance_zone,inside_zone=body.inside_zone,
                                        camera_ids=cameras, transitions=edges))))
            return {'config': decode(row)}

    @app.delete('/api/retail/visits/{site_id}/config')
    def disable(site_id: str, user=Depends(auth)):
        writable()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site_profile(db, site_id)
            if db.execute('DELETE FROM retail_visit_configs WHERE site_id=?', (site_id,)).rowcount:
                db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                           (time.time(), user['username'], 'retail.visits.disable', site_id, ''))
        return {'config': None}
