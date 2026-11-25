"""Commission entrances and the directed paths between a shop's cameras."""
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


class Entrance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    camera: Identifier
    entrance_zone: Identifier
    inside_zone: Identifier | None = None


class VisitConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entrances: list[Entrance] | None = Field(default=None, min_length=1, max_length=64)
    entrance_camera: Identifier | None = None
    entrance_zone: Identifier | None = None
    inside_zone: Identifier | None = None
    camera_ids: list[Identifier] = Field(min_length=2, max_length=64)
    transitions: list[Transition] = Field(max_length=4032)

    @model_validator(mode='after')
    def validate_input(self):
        if self.entrances is None:
            if 'entrances' in self.model_fields_set or not (self.entrance_camera and self.entrance_zone):
                raise ValueError('Укажите хотя бы один вход с камерой и зоной')
        else:
            first = self.entrances[0]
            aliases = {'entrance_camera': first.camera, 'entrance_zone': first.entrance_zone,
                       'inside_zone': first.inside_zone}
            if any(k in self.model_fields_set and getattr(self, k) != v for k, v in aliases.items()):
                raise ValueError('Поля основного входа должны совпадать с первым входом в списке')
        return self

    def effective_entrances(self, previous):
        if self.entrances is not None:
            return [door.model_dump() for door in self.entrances]
        # An older editor can only change the primary entrance.
        first = dict(camera=self.entrance_camera, entrance_zone=self.entrance_zone, inside_zone=self.inside_zone)
        return [first, *configured_entrances(previous)[1:]] if previous else [first]

    def validate_graph(self, entrances):
        cameras = set(self.camera_ids)
        if len(cameras) != len(self.camera_ids) or cameras & {'upload','webcam'}:
            raise ValueError('Выберите разные зарегистрированные камеры')
        roots = {door['camera'] for door in entrances}
        if len(roots) != len(entrances):
            raise ValueError('Для каждого входа выберите отдельную камеру')
        if not roots <= cameras or any(d['entrance_zone'] in cameras for d in entrances):
            raise ValueError('Включите все входные камеры в список и выберите их зоны')
        if any(d['inside_zone'] and (d['inside_zone'] in cameras or d['inside_zone'] == d['entrance_zone'])
               for d in entrances):
            raise ValueError('Укажите отдельную зону внутри магазина')
        edges = {camera: set() for camera in cameras}
        for step in self.transitions:
            if step.from_camera not in cameras or step.to_camera not in cameras:
                raise ValueError('Обе камеры каждого перехода должны входить в выбранный список')
            if step.to_camera in edges[step.from_camera]:
                raise ValueError('Прямой переход между этими камерами уже указан')
            edges[step.from_camera].add(step.to_camera)
        seen = set(roots)
        pending = list(roots)
        while pending:
            camera = pending.pop()
            following = edges[camera] - seen
            pending.extend(following)
            seen.update(following)
        if seen != cameras:
            raise ValueError('У каждой камеры должен быть направленный маршрут хотя бы от одного входа')


def initialize(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_visit_configs (
                site_id TEXT PRIMARY KEY, entrance_camera TEXT NOT NULL, entrance_zone TEXT NOT NULL, inside_zone TEXT, entrances TEXT,
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
        columns = {row['name'] for row in db.execute('PRAGMA table_info(retail_visit_configs)')}
        if 'inside_zone' not in columns:
            db.execute('ALTER TABLE retail_visit_configs ADD COLUMN inside_zone TEXT')
        if 'entrances' not in columns:
            db.execute('ALTER TABLE retail_visit_configs ADD COLUMN entrances TEXT')
        db.execute("""UPDATE retail_visit_configs SET entrances=json_array(json_object(
            'camera',entrance_camera,'entrance_zone',entrance_zone,'inside_zone',inside_zone))
            WHERE entrances IS NULL""")


def configured_entrances(row):
    """Normalize SQLite rows and decoded configs; legacy rows describe a single entrance."""
    if 'entrances' in row.keys() and row['entrances'] is not None:
        value = row['entrances']
        return json.loads(value) if isinstance(value, str) else value
    return [dict(camera=row['entrance_camera'], entrance_zone=row['entrance_zone'],
                 inside_zone=row['inside_zone'] if 'inside_zone' in row.keys() else None)]


def decode(row):
    if row is None:
        return None
    return {**dict(row), 'entrances':configured_entrances(row), 'camera_ids':json.loads(row['camera_ids']),
            'transitions':json.loads(row['transitions'])}


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
        # Camera and edge order do not change the graph. The first entrance is the legacy primary.
        cameras = sorted(body.camera_ids)
        edges = sorted(body.model_dump()['transitions'], key=lambda step: (step['from_camera'], step['to_camera']))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if site_profile(db, site_id) != 'retail':
                raise HTTPException(422, 'Настройка доступна на объекте с профилем магазина')
            previous = db.execute('SELECT * FROM retail_visit_configs WHERE site_id=?', (site_id,)).fetchone()
            entrances = body.effective_entrances(previous)
            try:
                body.validate_graph(entrances)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            registered = {row[0] for row in db.execute('SELECT id FROM cameras WHERE site_id=?', (site_id,))}
            if not set(cameras) <= registered:
                raise HTTPException(404, 'Камера на выбранном объекте не найдена')
            for door in entrances:
                outer = validate_camera_zone(db, site_id, door['camera'], door['entrance_zone'])
                if door['inside_zone']:
                    inner = validate_camera_zone(db, site_id, door['camera'], door['inside_zone'])
                    if (_inside(outer[0], inner) or _inside(inner[0], outer)
                            or any(_intersect(a,b,c,d) for a,b in _edges(outer) for c,d in _edges(inner))):
                        raise HTTPException(422, 'Оставьте промежуток между зоной входа и внутренней зоной')
            first = entrances[0]
            config = dict(entrance_camera=first['camera'], entrance_zone=first['entrance_zone'],
                          inside_zone=first['inside_zone'], entrances=entrances, camera_ids=cameras, transitions=edges)
            saved = decode(previous)
            active = saved is not None and saved['state'] == 'active'
            if active and all(saved[k] == v for k,v in config.items()):
                return {'config': saved}
            order_only = (active and saved['camera_ids'] == cameras and saved['transitions'] == edges
                          and {d['camera']: d for d in saved['entrances']} == {d['camera']: d for d in entrances})
            # Display order also selects the legacy primary entrance. It does not change tracking.
            activated = saved['activated_at'] if order_only else next_activation(db, site_id, 'retail.visits.configure', previous)
            db.execute("""INSERT INTO retail_visit_configs
                (site_id,entrance_camera,entrance_zone,inside_zone,entrances,camera_ids,transitions,activated_at,state)
                VALUES (:site_id,:entrance_camera,:entrance_zone,:inside_zone,:entrances,:camera_ids,:transitions,:activated_at,'active')
                ON CONFLICT(site_id) DO UPDATE SET entrance_camera=excluded.entrance_camera,
                entrance_zone=excluded.entrance_zone,inside_zone=excluded.inside_zone,entrances=excluded.entrances,
                camera_ids=excluded.camera_ids,transitions=excluded.transitions,
                activated_at=excluded.activated_at,state='active',reason=NULL""",
                dict(config, site_id=site_id, activated_at=activated, entrances=json.dumps(entrances),
                     camera_ids=json.dumps(cameras), transitions=json.dumps(edges)))
            row = db.execute('SELECT * FROM retail_visit_configs WHERE site_id=?', (site_id,)).fetchone()
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
                       (time.time() if order_only else activated, user['username'],
                        'retail.visits.reorder' if order_only else 'retail.visits.configure',
                        site_id, json.dumps(config)))
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
