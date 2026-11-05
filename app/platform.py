"""Organizations and sites within one trusted administrator installation, not SaaS tenancy."""
import json
import time
import uuid
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

MODULES = {
 'person': {'name':'Люди в кадре','state':'implemented','model':None},
 'intrusion': {'name':'Запретные зоны','state':'implemented','model':None},
 'fight': {'name':'Возможная драка','state':'implemented','model':'fight'},
 'admission': {'name':'Проверка посетителей','state':'implemented','model':'identity'},
 'theft': {'name':'Экспериментальный анализ возможной кражи','state':'experimental','model':'vadclip'},
 'face': {'name':'Движения лица','state':'implemented','model':'face'},
 'concealment': {'name':'Возможное сокрытие товара','state':'planned','model':None},
 'queue': {'name':'Очереди у касс','state':'planned','model':None},
 'ppe': {'name':'Средства защиты','state':'planned','model':None},
 'vehicle': {'name':'Въезд и выезд автомобилей','state':'planned','model':None},
 'pos': {'name':'Сверка с кассовыми операциями','state':'planned','model':None},
}
PROFILES = {
 'general': {'name':'Универсальный объект','defaults':['person','intrusion','fight']},
 'education': {'name':'Образование','defaults':['person','intrusion','fight','admission']},
 'retail': {'name':'Магазин / супермаркет','defaults':['person','intrusion','fight']},
 'warehouse': {'name':'Склад / производство','defaults':['person','intrusion','fight','admission']},
 'office': {'name':'Офис / жилой комплекс','defaults':['person','intrusion','admission']},
 'parking': {'name':'Парковка / территория','defaults':['person','intrusion']},
}


class Organization(BaseModel):
    name: str = Field(min_length=2,max_length=120,pattern=r'.*\S.*')


class CameraSite(BaseModel):
    site_id: str


class Site(Organization):
    organization_id: str
    profile: Literal['general','education','retail','warehouse','office','parking']
    modules: list[str] | None = None


def migrate(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS organizations(id TEXT PRIMARY KEY,name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sites(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,name TEXT NOT NULL,profile TEXT NOT NULL,modules TEXT NOT NULL);
      INSERT OR IGNORE INTO organizations VALUES('default','Моя организация');
      INSERT OR IGNORE INTO sites VALUES('default','default','Первый объект','education','["person","intrusion","fight","admission","theft","face"]');
    ''')
    for table in ('cameras','events','jobs'):
        columns={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'site_id' not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN site_id TEXT NOT NULL DEFAULT 'default'")
            if table!='cameras':
                db.execute(f"UPDATE {table} SET site_id=COALESCE((SELECT site_id FROM cameras WHERE id={table}.camera),'default')")
        db.execute(f'CREATE INDEX IF NOT EXISTS {table}_site ON {table}(site_id)')
        if table=='jobs':
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_assign_site AFTER INSERT ON {table}
                WHEN EXISTS(SELECT 1 FROM cameras WHERE id=NEW.camera)
                BEGIN UPDATE {table} SET site_id=(SELECT site_id FROM cameras WHERE id=NEW.camera) WHERE id=NEW.id; END''')


    db.execute('DROP TRIGGER IF EXISTS events_assign_site')

def check_site(store, identifier):
    if identifier is not None and not store.rows('SELECT id FROM sites WHERE id=?',(identifier,)):
        raise HTTPException(404,'Объект не найден')
    return identifier


def modules_for(store, identifier):
    rows=store.rows('SELECT modules FROM sites WHERE id=?',(identifier,))
    return set(json.loads(rows[0]['modules'])) if rows else set()


def camera_site(store,camera):
    rows=store.rows('SELECT site_id FROM cameras WHERE id=?',(camera,))
    return rows[0]['site_id'] if rows else 'default'


def camera_enabled(store,camera,module):
    return module in modules_for(store,camera_site(store,camera))


def register(app,store,engine,auth,writable):
    @app.get('/api/platform')
    def platform(user=Depends(auth)):
        states={m['id']:m['state'] for m in engine.health['analyzer'].get('models',[])}
        return {'access_model':'single_trusted_administrator','organizations':store.rows('SELECT * FROM organizations ORDER BY name'),
                'sites':[{**s,'modules':json.loads(s['modules'])} for s in store.rows('SELECT * FROM sites ORDER BY name')],
                'profiles':PROFILES,'modules':{k:{**v,'availability':states.get(v['model'],'missing') if v['model'] else ('available' if v['state']!='planned' else 'planned')} for k,v in MODULES.items()}}

    @app.post('/api/platform/organizations',status_code=201)
    def create_organization(body:Organization,user=Depends(auth)):
        writable();identifier=str(uuid.uuid4())
        store.execute('INSERT INTO organizations VALUES(?,?)',(identifier,body.name.strip()))
        store.audit(user['username'],'organization.create',identifier)
        return {'id':identifier}

    @app.put('/api/platform/organizations/{identifier}')
    def rename_organization(identifier:str,body:Organization,user=Depends(auth)):
        writable()
        if not store.execute('UPDATE organizations SET name=? WHERE id=?',(body.name.strip(),identifier)):raise HTTPException(404)
        store.audit(user['username'],'organization.rename',identifier)
        return {'ok':True}

    def values(body):
        if not store.rows('SELECT id FROM organizations WHERE id=?',(body.organization_id,)):raise HTTPException(422,'Организация не найдена')
        modules=body.modules if body.modules is not None else PROFILES[body.profile]['defaults']
        if any(m not in MODULES or MODULES[m]['state']=='planned' for m in modules):raise HTTPException(422,'Этот модуль ещё не реализован')
        return sorted(set(modules)|{'person'})

    @app.post('/api/platform/sites',status_code=201)
    def create_site(body:Site,user=Depends(auth)):
        writable();modules=values(body);identifier=str(uuid.uuid4())
        store.execute('INSERT INTO sites VALUES(?,?,?,?,?)',(identifier,body.organization_id,body.name.strip(),body.profile,json.dumps(modules)))
        store.audit(user['username'],'site.create',identifier)
        return {'id':identifier}

    @app.put('/api/platform/sites/{identifier}')
    def update_site(identifier:str,body:Site,user=Depends(auth)):
        writable();modules=values(body)
        if not store.execute('UPDATE sites SET organization_id=?,name=?,profile=?,modules=? WHERE id=?',(body.organization_id,body.name.strip(),body.profile,json.dumps(modules),identifier)):raise HTTPException(404)
        store.audit(user['username'],'site.update',identifier,json.dumps({'profile':body.profile,'modules':modules}))
        return {'ok':True}

    @app.put('/api/platform/cameras/{camera}/site')
    def assign_camera(camera:str,body:CameraSite,user=Depends(auth)):
        writable();check_site(store,body.site_id)
        if not store.execute('UPDATE cameras SET site_id=? WHERE id=?',(body.site_id,camera)):raise HTTPException(404)
        store.audit(user['username'],'camera.site',camera,body.site_id)
        return {'ok':True}
