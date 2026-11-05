import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from .platform import migrate, camera_site


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'{salt}:{digest}'


def password_matches(password, encoded):
    return secrets.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'glazza.db'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, stream TEXT NOT NULL, zones TEXT NOT NULL DEFAULT '[]', analyze INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source TEXT NOT NULL, camera TEXT NOT NULL, kind TEXT NOT NULL, score REAL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}', UNIQUE(source, source_id, kind));
                CREATE INDEX IF NOT EXISTS event_time ON events(started DESC);
                CREATE INDEX IF NOT EXISTS event_camera_time ON events(camera,started DESC,id DESC);
                CREATE INDEX IF NOT EXISTS event_camera_alert ON events(camera) WHERE status='new' AND kind IN ('fight','intrusion','theft');
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, event_id TEXT, source_id TEXT, camera TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, error TEXT, result TEXT, file TEXT);
                CREATE INDEX IF NOT EXISTS job_state ON jobs(state, created);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            job_columns = {'clip_start':'REAL', 'clip_end':'REAL', 'event_id':'TEXT', 'source_id':'TEXT',
                           'attempts':'INTEGER NOT NULL DEFAULT 0', 'error':'TEXT', 'result':'TEXT'}
            for name, definition in job_columns.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS job_source ON jobs(source_id)')
            db.execute('CREATE INDEX IF NOT EXISTS job_origin_state ON jobs(event_id,state)')
            camera_columns = {row['name'] for row in db.execute('PRAGMA table_info(cameras)')}
            for name in ('latitude', 'longitude'):
                if name not in camera_columns:
                    db.execute(f'ALTER TABLE cameras ADD COLUMN {name} REAL')
            migrate(db)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, query, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, args).fetchall()]

    def execute(self, query, args=()):
        with self.connect() as db:
            return db.execute(query, args).rowcount

    def audit(self, actor, action, subject, detail=''):
        self.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)', (time.time(), actor, action, subject, detail))

    def enqueue(self, *, id, camera, site_id, event_id=None, source_id=None,
                file=None, clip_start=None, clip_end=None, now=None):
        """Serialize all producers, including uploads, against one pending-job limit."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM jobs WHERE id=?', (id,)).fetchone():
                return True  # Idempotent ingestion, even while the queue is full.
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 100:
                return False
            db.execute('''INSERT INTO jobs(id,camera,site_id,event_id,source_id,file,clip_start,clip_end,state,created,updated)
                VALUES(?,?,?,?,?,?,?,?, 'queued',?,?)''',
                (id,camera,site_id,event_id,source_id,file,clip_start,clip_end,now,now))
            return True

    def retry_job(self, identifier):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 100:
                return 'full'
            changed = db.execute("UPDATE jobs SET state='queued',attempts=0,updated=?,error=NULL WHERE id=? AND state='failed' AND file IS NOT NULL",
                                 (time.time(),identifier)).rowcount
            return 'queued' if changed else 'unavailable'

    def event(self, *, source_id, source, camera, kind, score, started, ended=None, details=None, site_id=None):
        identifier = hashlib.sha256(f'{source}:{source_id}:{kind}'.encode()).hexdigest()[:32]
        site_id = site_id or camera_site(self, camera)
        with self.connect() as db:
            db.execute('''INSERT INTO events(id,source_id,source,camera,kind,score,started,ended,details,site_id) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,source_id,kind) DO UPDATE SET ended=excluded.ended, score=excluded.score, details=excluded.details''',
                (identifier, source_id, source, camera, kind, score, started, ended, json.dumps(details or {}, ensure_ascii=False), site_id))
        return identifier

    def cameras(self, private=False, site_id=None):
        items = self.rows('SELECT * FROM cameras' + (' WHERE site_id=?' if site_id else '') + ' ORDER BY name', (site_id,) if site_id else ())
        for item in items:
            item['zones'] = json.loads(item['zones'])
            item['analyze'] = bool(item['analyze'])
            if not private:
                item.pop('stream')
        return items

    def events(self, status=None, kind=None, camera=None, limit=100, site_id=None):
        filters, args = [], []
        for name, value in [('status', status), ('kind', kind), ('camera', camera), ('site_id', site_id)]:
            if value:
                filters.append(f'{name}=?')
                args.append(value)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        items = self.rows('SELECT * FROM events' + where + ' ORDER BY started DESC LIMIT ?', (*args, limit))
        for item in items:
            item['details'] = json.loads(item['details'])
        return items

    def camera_activity(self):
        latest = self.rows('''SELECT e.id,e.camera,e.kind,e.started,e.status,e.score
            FROM cameras c JOIN events e ON e.id=(
                SELECT id FROM events WHERE camera=c.id ORDER BY started DESC,id DESC LIMIT 1)''')
        alerts = self.rows("SELECT camera,count(*) AS n FROM events WHERE status='new' AND kind IN ('fight','intrusion','theft') GROUP BY camera")
        return {event['camera']: event for event in latest}, {row['camera']: row['n'] for row in alerts}
