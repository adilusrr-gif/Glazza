import contextlib
import hashlib
import json
import secrets
import sqlite3
import time


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
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, event_id TEXT, source_id TEXT, camera TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, error TEXT, result TEXT, file TEXT);
                CREATE INDEX IF NOT EXISTS job_state ON jobs(state, created);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for name in ('clip_start', 'clip_end'):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} REAL')
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

    def event(self, *, source_id, source, camera, kind, score, started, ended=None, details=None):
        identifier = hashlib.sha256(f'{source}:{source_id}:{kind}'.encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute('''INSERT INTO events(id,source_id,source,camera,kind,score,started,ended,details) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,source_id,kind) DO UPDATE SET ended=excluded.ended, score=excluded.score, details=excluded.details''',
                (identifier, source_id, source, camera, kind, score, started, ended, json.dumps(details or {}, ensure_ascii=False)))
        return identifier

    def cameras(self, private=False):
        items = self.rows('SELECT * FROM cameras ORDER BY name')
        for item in items:
            item['zones'] = json.loads(item['zones'])
            item['analyze'] = bool(item['analyze'])
            if not private:
                item.pop('stream')
        return items

    def events(self, status=None, kind=None, camera=None, limit=100):
        filters, args = [], []
        for name, value in [('status', status), ('kind', kind), ('camera', camera)]:
            if value:
                filters.append(f'{name}=?')
                args.append(value)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        items = self.rows('SELECT * FROM events' + where + ' ORDER BY started DESC LIMIT ?', (*args, limit))
        for item in items:
            item['details'] = json.loads(item['details'])
        return items
