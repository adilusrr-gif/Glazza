"""Local admission registry and human-reviewed entrance events; no criminal labels."""
import base64
import json
import math
import time
import uuid
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from .platform import check_site, camera_site, modules_for

MODEL = 'opencv-sface-2021dec'


class Person(BaseModel):
    site_id: str = 'default'
    name: str = Field(min_length=2, max_length=120)
    category: Literal['student', 'staff', 'guest']
    valid_from: float = Field(ge=0, le=4102444800)
    valid_until: float = Field(ge=0, le=4102444800)
    host: str = Field(default='', max_length=120)
    enabled: bool = True
    basis: str = Field(min_length=3, max_length=300)

    @model_validator(mode='after')
    def dates(self):
        if not all(math.isfinite(x) for x in (self.valid_from, self.valid_until)) or self.valid_until <= self.valid_from:
            raise ValueError('Укажите корректный срок допуска')
        if self.category == 'guest' and not self.host.strip():
            raise ValueError('Для гостя нужен принимающий сотрудник')
        return self


class Entrance(BaseModel):
    camera: str
    zone: str
    direction: Literal['entry', 'exit'] = 'entry'
    enabled: bool = True


class Decision(BaseModel):
    decision: Literal['allowed', 'guest', 'denied', 'exit', 'dismissed']
    person_id: str | None = None
    note: str = Field(min_length=2, max_length=1000)


def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS admission_people(id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,
          valid_from REAL NOT NULL,valid_until REAL NOT NULL,host TEXT NOT NULL,enabled INTEGER NOT NULL,basis TEXT NOT NULL,
          photo BLOB,embedding TEXT,model TEXT,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS admission_entrances(camera TEXT PRIMARY KEY,zone TEXT NOT NULL,direction TEXT NOT NULL,enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS admission_entries(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,source_id TEXT NOT NULL,
          camera TEXT NOT NULL,direction TEXT NOT NULL,at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          reason TEXT NOT NULL DEFAULT 'awaiting_face',candidate_id TEXT,score REAL,person_id TEXT,decision TEXT,
          note TEXT NOT NULL DEFAULT '',reviewer TEXT,reviewed_at REAL,attempts INTEGER NOT NULL DEFAULT 0,
          UNIQUE(camera,source_id,direction));
        CREATE INDEX IF NOT EXISTS admission_pending ON admission_entries(state,at);
        ''')
        for table in ('admission_people','admission_entries'):
            columns={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            if 'site_id' not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN site_id TEXT NOT NULL DEFAULT 'default'")
                if table=='admission_entries':
                    db.execute("UPDATE admission_entries SET site_id=COALESCE((SELECT site_id FROM cameras WHERE id=admission_entries.camera),'default')")


def candidate(store, vector, at, site_id='default'):
    if len(vector) != 128 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
        raise ValueError('Invalid face descriptor')
    norm = math.sqrt(sum(x*x for x in vector))
    if norm < .5:
        raise ValueError('Empty descriptor')
    matches = []
    for p in store.rows('SELECT id,embedding FROM admission_people WHERE enabled=1 AND valid_from<=? AND valid_until>=? AND model=? AND embedding IS NOT NULL AND site_id=?', (at, at, MODEL, site_id)):
        other = json.loads(p['embedding'])
        score = sum(a*b for a,b in zip(vector, other)) / norm
        matches.append((score, p['id']))
    matches.sort(reverse=True)
    if not matches:
        return None, None, 'no_active_enrollment'
    score, identifier = matches[0]
    # Deliberately conservative pilot gate, not a calibrated identity probability.
    if score < .5 or (len(matches) > 1 and score - matches[1][0] < .08):
        return None, score, 'unknown_or_ambiguous'
    return identifier, score, 'candidate_requires_review'


def ingest(store, event_id, source_id, camera, zones, at, site_id=None):
    rules = store.rows('SELECT * FROM admission_entrances WHERE camera=? AND enabled=1', (camera,))
    site_id = site_id or camera_site(store,camera)
    if 'admission' not in modules_for(store,site_id) or not rules or rules[0]['zone'] not in zones:
        return
    r = rules[0]
    store.execute('INSERT OR IGNORE INTO admission_entries(id,event_id,source_id,camera,direction,at,site_id) VALUES(?,?,?,?,?,?,?)',
                  (str(uuid.uuid4()),event_id,source_id,camera,r['direction'],at,site_id))


async def process(engine):
    store = engine.store
    for entry in store.rows("SELECT * FROM admission_entries WHERE state='pending' ORDER BY at LIMIT 5"):
        try:
            async with engine.client.stream('GET', engine.settings.frigate_url + f"/api/events/{entry['source_id']}/snapshot.jpg?crop=1") as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2*1024*1024:
                        raise ValueError('Snapshot too large')
                    chunks.append(chunk)
            response = await engine.client.post(engine.settings.analyzer_url + '/identity/encode', content=b''.join(chunks),
                headers={'X-Worker-Key':engine.settings.worker_key,'Content-Type':'image/jpeg'}, timeout=15)
            response.raise_for_status()
            data = response.json()
            person, score, reason = candidate(store, data['embedding'], entry['at'], entry['site_id'])
            store.execute("UPDATE admission_entries SET state='review',reason=?,candidate_id=?,score=? WHERE id=? AND state='pending'", (reason, person, score, entry['id']))
        except Exception:
            attempts = entry['attempts'] + 1
            store.execute("UPDATE admission_entries SET attempts=?,state=?,reason='recognition_unavailable' WHERE id=? AND state='pending'",
                          (attempts, 'review' if attempts >= 3 else 'pending', entry['id']))


def register(app, store, engine, auth, writable):
    initialize(store)

    @app.get('/api/admission')
    def summary(site_id: str | None = None,user=Depends(auth)):
        check_site(store,site_id)
        people = store.rows('SELECT id,name,category,valid_from,valid_until,host,enabled,basis,site_id,photo IS NOT NULL AS has_photo FROM admission_people ORDER BY name')
        entries = store.rows('SELECT e.*,p.name AS candidate_name,r.name AS person_name FROM admission_entries e LEFT JOIN admission_people p ON p.id=e.candidate_id LEFT JOIN admission_people r ON r.id=e.person_id WHERE (? IS NULL OR e.site_id=?) ORDER BY (e.decision IS NULL) DESC,e.at DESC LIMIT 200', (site_id,site_id))
        if site_id:
            people=[p for p in people if p['site_id']==site_id]
            entries=[e for e in entries if e['site_id']==site_id]
        return {'people':people,'entrances':store.rows('SELECT r.* FROM admission_entrances r JOIN cameras c ON c.id=r.camera WHERE (? IS NULL OR c.site_id=?)',(site_id,site_id)), 'entries':entries,
                'pending':store.rows("SELECT count(*) AS n FROM admission_entries WHERE decision IS NULL AND (? IS NULL OR site_id=?)", (site_id,site_id))[0]['n'],
                'recognition':next((m['state'] for m in engine.health['analyzer'].get('models',[]) if m['id']=='identity'),'unavailable')}

    @app.post('/api/admission/people', status_code=201)
    def create(body: Person, user=Depends(auth)):
        writable()
        check_site(store,body.site_id)
        identifier = str(uuid.uuid4())
        with store.connect() as db:
            db.execute('INSERT INTO admission_people(id,name,category,valid_from,valid_until,host,enabled,basis,created,site_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifier,body.name,body.category,body.valid_from,body.valid_until,body.host,body.enabled,body.basis,time.time(),body.site_id))
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',(time.time(),user['username'],'admission.create',identifier,''))
        return {'id':identifier}

    @app.put('/api/admission/people/{identifier}')
    def update(identifier: str, body: Person, user=Depends(auth)):
        writable()
        existing=store.rows('SELECT site_id FROM admission_people WHERE id=?',(identifier,))
        if not existing: raise HTTPException(404)
        if 'site_id' in body.model_fields_set and body.site_id!=existing[0]['site_id']: raise HTTPException(422,'Создайте отдельный допуск для другого объекта')
        if not store.execute('UPDATE admission_people SET name=?,category=?,valid_from=?,valid_until=?,host=?,enabled=?,basis=? WHERE id=?',
                (body.name,body.category,body.valid_from,body.valid_until,body.host,body.enabled,body.basis,identifier)):
            raise HTTPException(404)
        store.audit(user['username'],'admission.update',identifier)
        return {'ok':True}

    @app.delete('/api/admission/people/{identifier}')
    def delete(identifier: str,user=Depends(auth)):
        writable()
        with store.connect() as db:
            db.execute('DELETE FROM admission_people WHERE id=?',(identifier,))
            db.execute('''UPDATE admission_entries SET
                candidate_id=CASE WHEN candidate_id=? THEN NULL ELSE candidate_id END,
                person_id=CASE WHEN person_id=? THEN NULL ELSE person_id END
                WHERE candidate_id=? OR person_id=?''',(identifier,identifier,identifier,identifier))
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',(time.time(),user['username'],'admission.delete',identifier,''))
        return {'ok':True}

    @app.post('/api/admission/people/{identifier}/photo')
    async def photo(identifier: str, request: Request,user=Depends(auth)):
        writable()
        if not store.rows('SELECT id FROM admission_people WHERE id=?',(identifier,)):
            raise HTTPException(404)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>2*1024*1024:
                raise HTTPException(413,'Фото до 2 МБ')
        try:
            response=await engine.client.post(engine.settings.analyzer_url+'/identity/encode',content=bytes(data),headers={'X-Worker-Key':engine.settings.worker_key},timeout=20)
            response.raise_for_status()
            result=response.json()
            candidate(store,result['embedding'],time.time())
            image=base64.b64decode(result['jpeg'],validate=True)
            if result['model'] != MODEL or len(image)>2*1024*1024:
                raise ValueError()
        except Exception:
            raise HTTPException(422,'Нужна модель identity и чёткое фото одного лица без сильного поворота')
        if not store.execute('UPDATE admission_people SET photo=?,embedding=?,model=? WHERE id=?',(image,json.dumps(result['embedding']),MODEL,identifier)):
            raise HTTPException(404)
        store.audit(user['username'],'admission.enroll',identifier)
        return {'ok':True}

    @app.get('/api/admission/people/{identifier}/photo')
    def get_photo(identifier: str,user=Depends(auth)):
        rows=store.rows('SELECT photo FROM admission_people WHERE id=?',(identifier,))
        if not rows or rows[0]['photo'] is None:
            raise HTTPException(404)
        return Response(rows[0]['photo'],media_type='image/jpeg')

    @app.put('/api/admission/entrances')
    def entrance(body: Entrance,user=Depends(auth)):
        writable()
        cameras={c['id']:c for c in store.cameras()}
        if body.camera not in cameras or body.zone not in {z['name'] for z in cameras[body.camera]['zones']}:
            raise HTTPException(422,'Выберите существующую камеру и зону')
        store.execute('INSERT INTO admission_entrances VALUES(?,?,?,?) ON CONFLICT(camera) DO UPDATE SET zone=excluded.zone,direction=excluded.direction,enabled=excluded.enabled',
                      (body.camera,body.zone,body.direction,body.enabled))
        store.audit(user['username'],'admission.entrance',body.camera)
        return {'ok':True}

    @app.post('/api/admission/entries/{identifier}/decision')
    def decision(identifier: str, body: Decision,user=Depends(auth)):
        writable()
        now=time.time()
        with store.connect() as db:
            entry=db.execute('SELECT * FROM admission_entries WHERE id=?',(identifier,)).fetchone()
            if not entry: raise HTTPException(404)
            if body.person_id is not None:
                p=db.execute('SELECT * FROM admission_people WHERE id=?',(body.person_id,)).fetchone()
                if not p or p['site_id'] != entry['site_id']:
                    raise HTTPException(422,'Выберите человека этого объекта')
            if body.decision in ('allowed','guest','exit'):
                p=db.execute('SELECT * FROM admission_people WHERE id=?',(body.person_id,)).fetchone()
                if not p or p['site_id'] != entry['site_id'] or (body.decision != 'exit' and (not p['enabled'] or not p['valid_from']<=now<=p['valid_until'])):
                    raise HTTPException(422,'Выберите человека с действующим допуском')
                if body.decision=='guest' and p['category']!='guest': raise HTTPException(422,'Нужен допуск гостя')
            if body.decision=='exit' and entry['direction']!='exit': raise HTTPException(422,'Это зона входа')
            db.execute("UPDATE admission_entries SET decision=?,person_id=?,note=?,reviewer=?,reviewed_at=?,state='reviewed' WHERE id=?",
                       (body.decision,body.person_id,body.note,user['username'],now,identifier))
            db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',(now,user['username'],'admission.decision',identifier,json.dumps({'decision':body.decision,'previous':entry['decision']})))
        return {'ok':True}

    @app.post('/api/admission/entries/{identifier}/retry')
    def retry(identifier: str,user=Depends(auth)):
        writable()
        if not store.execute("UPDATE admission_entries SET state='pending',attempts=0,reason='awaiting_face',candidate_id=NULL,score=NULL WHERE id=? AND decision IS NULL", (identifier,)):
            raise HTTPException(409,'Нет непроверенного прохода')
        store.audit(user['username'],'admission.retry',identifier)
        return {'ok':True}
