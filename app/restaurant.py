"""Restaurant table lifecycle and staff tasks driven by current, local NVR counts.

Presence is an observation; greeting and cleaning are explicit staff confirmations.
The database owns visits, deadlines and alerts, independently of any open browser.
"""
import hashlib
import json
import math
import secrets
import time
import uuid
from typing import Literal

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .platform import check_site
from .schemas import Zone


class StaffInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    site_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    role: Literal['waiter', 'manager', 'cleaner']
    telegram_chat_id: str = Field(default='', pattern=r'^(-?[0-9]{1,20})?$')
    enabled: bool = True


class TableInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    site_id: str = Field(min_length=1, max_length=80)
    camera: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    points: list[tuple[float, float]] = Field(min_length=3, max_length=20)
    waiter_id: str | None = None
    cleaner_id: str | None = None
    wait_seconds: int = Field(default=120, ge=30, le=1800, strict=True)
    arrival_seconds: int = Field(default=5, ge=1, le=60, strict=True)
    vacancy_seconds: int = Field(default=60, ge=10, le=600, strict=True)

    @field_validator('points')
    @classmethod
    def valid_points(cls, value):
        return Zone(name='restaurant_seats', points=value).points

    @field_validator('waiter_id', 'cleaner_id')
    @classmethod
    def optional_id(cls, value):
        return value or None


class TableAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['greeted', 'cleaned', 'hold', 'resume']
    # Required even when null: an old task must not act on a new party.
    visit_id: str | None
    revision: int | None = Field(default=None, ge=0)


def _at(now=None):
    value = time.time() if now is None else now
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid observation time')
    return float(value)


def _audit(db, actor, action, subject, now, detail=''):
    db.execute('INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)',
               (now, actor, action, subject, detail))


def _module(db, site_id):
    row = db.execute('SELECT modules FROM sites WHERE id=?', (site_id,)).fetchone()
    return bool(row and 'restaurant' in json.loads(row['modules']))


def _require_module(db, site_id):
    if not db.execute('SELECT id FROM sites WHERE id=?', (site_id,)).fetchone():
        raise HTTPException(404, 'Объект не найден')
    if not _module(db, site_id):
        raise HTTPException(409, 'Включите модуль «Ресторан» в настройках объекта')


class RestaurantService:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.stale_seconds = max(15.0, float(getattr(settings, 'poll_seconds', 5)) * 3)
        self.health = {'state': 'waiting', 'detail': 'Ожидание текущих данных о столах'}
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS restaurant_staff(
                    id TEXT PRIMARY KEY, site_id TEXT NOT NULL, name TEXT NOT NULL,
                    role TEXT NOT NULL, telegram_chat_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1, token_hash TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS restaurant_staff_site ON restaurant_staff(site_id,role);
                CREATE TABLE IF NOT EXISTS restaurant_tables(
                    id TEXT PRIMARY KEY, site_id TEXT NOT NULL, camera TEXT NOT NULL,
                    name TEXT NOT NULL, zone TEXT NOT NULL UNIQUE, points TEXT NOT NULL,
                    waiter_id TEXT, cleaner_id TEXT,
                    wait_seconds INTEGER NOT NULL DEFAULT 120,
                    arrival_seconds INTEGER NOT NULL DEFAULT 5,
                    vacancy_seconds INTEGER NOT NULL DEFAULT 60,
                    state TEXT NOT NULL DEFAULT 'needs_cleaning',
                    occupancy TEXT NOT NULL DEFAULT 'unknown', people INTEGER,
                    visit_id TEXT, candidate_since REAL, absent_since REAL,
                    hold INTEGER NOT NULL DEFAULT 0, last_observed_at REAL,
                    revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    UNIQUE(site_id,name));
                CREATE INDEX IF NOT EXISTS restaurant_table_camera ON restaurant_tables(camera);
                CREATE TABLE IF NOT EXISTS restaurant_visits(
                    id TEXT PRIMARY KEY, table_id TEXT NOT NULL, site_id TEXT NOT NULL,
                    arrived_at REAL NOT NULL, deadline_at REAL NOT NULL,
                    greeted_at REAL, greeted_by TEXT, ended_at REAL, escalated_at REAL);
                CREATE INDEX IF NOT EXISTS restaurant_visit_table ON restaurant_visits(table_id,arrived_at);
                CREATE TABLE IF NOT EXISTS restaurant_alerts(
                    id TEXT PRIMARY KEY, site_id TEXT NOT NULL, table_id TEXT NOT NULL,
                    table_name TEXT NOT NULL, visit_id TEXT NOT NULL, kind TEXT NOT NULL,
                    recipient_id TEXT NOT NULL DEFAULT '', recipient_name TEXT NOT NULL DEFAULT '',
                    at REAL NOT NULL, message TEXT NOT NULL,
                    ack_at REAL, ack_by TEXT, resolved_at REAL,
                    delivery_state TEXT NOT NULL DEFAULT 'in_app',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    delivery_started REAL, delivered_at REAL, delivery_error TEXT,
                    UNIQUE(visit_id,kind,recipient_id));
                CREATE INDEX IF NOT EXISTS restaurant_alert_site ON restaurant_alerts(site_id,at DESC);
                CREATE INDEX IF NOT EXISTS restaurant_alert_delivery ON restaurant_alerts(delivery_state,retry_at);
            ''')
            # After a process restart, retained DB observations do not establish a live feed.
            # Visits and deadlines remain intact; debounce windows start with fresh evidence.
            db.execute("UPDATE restaurant_tables SET occupancy='unknown', people=NULL, candidate_since=NULL, absent_since=NULL, last_observed_at=NULL")

    def set_health(self, value):
        self.health = dict(value)

    def camera_in_use(self, camera_id):
        return bool(self.store.rows('SELECT id FROM restaurant_tables WHERE camera=? LIMIT 1', (camera_id,)))

    def configuration_tables(self):
        with self.store.connect() as db:
            rows = db.execute('''SELECT t.* FROM restaurant_tables t JOIN cameras c
                ON c.id=t.camera AND c.site_id=t.site_id ORDER BY t.id''').fetchall()
            return [{**dict(t), 'points': json.loads(t['points'])} for t in rows if _module(db, t['site_id'])]

    def authenticate_staff(self, token):
        if not token or len(token) > 200:
            raise HTTPException(401, 'Откройте персональную ссылку сотрудника')
        rows = self.store.rows('SELECT * FROM restaurant_staff WHERE token_hash=? AND enabled=1',
                               (hashlib.sha256(token.encode()).hexdigest(),))
        if not rows:
            raise HTTPException(401, 'Ссылка сотрудника недействительна или отозвана')
        return rows[0]

    @staticmethod
    def _table(db, identifier):
        row = db.execute('SELECT * FROM restaurant_tables WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, 'Стол не найден')
        return dict(row)

    @staticmethod
    def _staff(db, identifier):
        row = db.execute('SELECT * FROM restaurant_staff WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, 'Сотрудник не найден')
        return dict(row)

    def _current_staff(self, db, staff):
        if not staff:
            return None
        current = self._staff(db, staff['id'])
        if not current['enabled'] or not secrets.compare_digest(current['token_hash'], staff['token_hash']):
            raise HTTPException(401, 'Ссылка сотрудника отключена или обновлена')
        return current

    @staticmethod
    def _public_staff(staff, private=False):
        keys = ['id', 'site_id', 'name', 'role', 'enabled']
        if private:
            keys.append('telegram_chat_id')
        return {k: bool(staff[k]) if k == 'enabled' else staff[k] for k in keys}

    def save_staff(self, body, actor, identifier=None, now=None):
        now = _at(now)
        token = secrets.token_urlsafe(32) if identifier is None else None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_module(db, body.site_id)
            if identifier:
                old = self._staff(db, identifier)
                assigned = db.execute('SELECT id FROM restaurant_tables WHERE waiter_id=? OR cleaner_id=?',
                                      (identifier, identifier)).fetchone()
                if assigned and (old['site_id'] != body.site_id or old['role'] != body.role):
                    raise HTTPException(409, 'Сначала измените назначения столов сотрудника')
                db.execute('''UPDATE restaurant_staff SET site_id=?,name=?,role=?,telegram_chat_id=?,enabled=? WHERE id=?''',
                           (body.site_id, body.name, body.role, body.telegram_chat_id, int(body.enabled), identifier))
                if not body.enabled or old['site_id'] != body.site_id or old['role'] != body.role:
                    db.execute("UPDATE restaurant_alerts SET resolved_at=?,delivery_state=CASE WHEN delivery_state IN ('pending','sending') THEN 'cancelled' ELSE delivery_state END WHERE recipient_id=? AND resolved_at IS NULL", (now, identifier))
            else:
                identifier = uuid.uuid4().hex
                db.execute('''INSERT INTO restaurant_staff(id,site_id,name,role,telegram_chat_id,enabled,token_hash,created)
                    VALUES(?,?,?,?,?,?,?,?)''', (identifier, body.site_id, body.name, body.role, body.telegram_chat_id,
                                              int(body.enabled), hashlib.sha256(token.encode()).hexdigest(), now))
            # A new manager must inherit unresolved escalations, including earlier
            # unassigned diagnostics. A removed/moved manager must not strand them.
            for row in db.execute("SELECT * FROM restaurant_tables WHERE visit_id IS NOT NULL").fetchall():
                table = dict(row)
                if not _module(db, table['site_id']):
                    continue
                if table['state'] == 'waiting':
                    self._sync_routes(db, table, 'arrival', now)
                    visit = db.execute('SELECT escalated_at FROM restaurant_visits WHERE id=?', (table['visit_id'],)).fetchone()
                    if visit and visit['escalated_at'] is not None:
                        self._sync_routes(db, table, 'escalation', now)
                elif table['state'] == 'needs_cleaning':
                    self._sync_routes(db, table, 'cleaning', now)
            _audit(db, actor, 'restaurant.staff.update' if token is None else 'restaurant.staff.create', identifier, now)
        result = {'id': identifier}
        if token:
            result.update(token=token, portal_url='/staff#token=' + token)
        return result

    def rotate_token(self, identifier, actor, now=None):
        now = _at(now)
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._staff(db, identifier)
            db.execute('UPDATE restaurant_staff SET token_hash=? WHERE id=?',
                       (hashlib.sha256(token.encode()).hexdigest(), identifier))
            _audit(db, actor, 'restaurant.staff.rotate_link', identifier, now)
        return {'id': identifier, 'token': token, 'portal_url': '/staff#token=' + token}

    def save_table(self, body, actor, identifier=None, now=None):
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_module(db, body.site_id)
            camera = db.execute('SELECT site_id FROM cameras WHERE id=?', (body.camera,)).fetchone()
            if not camera or camera['site_id'] != body.site_id:
                raise HTTPException(422, 'Выберите камеру этого объекта')
            for key, role in [('waiter_id', 'waiter'), ('cleaner_id', 'cleaner')]:
                employee_id = getattr(body, key)
                if employee_id:
                    staff = self._staff(db, employee_id)
                    if staff['site_id'] != body.site_id or staff['role'] != role or not staff['enabled']:
                        raise HTTPException(422, 'Назначьте активного сотрудника соответствующей роли этого объекта')
            duplicate = db.execute('SELECT id FROM restaurant_tables WHERE site_id=? AND name=? AND id!=?',
                                   (body.site_id, body.name, identifier or '')).fetchone()
            if duplicate:
                raise HTTPException(409, 'На объекте уже есть стол с таким названием')
            points = json.dumps(body.points)
            if identifier:
                old = self._table(db, identifier)
                geometry_changed = old['camera'] != body.camera or json.loads(old['points']) != json.loads(points) or old['site_id'] != body.site_id
                if geometry_changed and old['state'] in ('waiting', 'served'):
                    raise HTTPException(409, 'Изменить зону или объект можно после завершения визита')
                zone = 'rt_' + uuid.uuid4().hex[:12] if geometry_changed else old['zone']
                db.execute('''UPDATE restaurant_tables SET site_id=?,camera=?,name=?,zone=?,points=?,waiter_id=?,cleaner_id=?,
                    wait_seconds=?,arrival_seconds=?,vacancy_seconds=?,revision=revision+1 WHERE id=?''',
                           (body.site_id, body.camera, body.name, zone, points, body.waiter_id, body.cleaner_id,
                            body.wait_seconds, body.arrival_seconds, body.vacancy_seconds, identifier))
                if geometry_changed:
                    if old['visit_id']:
                        self._resolve(db, old['visit_id'], now)
                    db.execute("UPDATE restaurant_tables SET state='needs_cleaning',occupancy='unknown',people=NULL,visit_id=NULL,candidate_since=NULL,absent_since=NULL,last_observed_at=NULL,hold=0 WHERE id=?", (identifier,))
                elif old['visit_id'] and (old['waiter_id'] != body.waiter_id or old['cleaner_id'] != body.cleaner_id):
                    kind = 'arrival' if old['state'] == 'waiting' else 'cleaning' if old['state'] == 'needs_cleaning' else None
                    if kind:
                        self._resolve(db, old['visit_id'], now, kinds=(kind,))
                        self._notify(db, self._table(db, identifier), kind, now, reopen=True)
            else:
                identifier = uuid.uuid4().hex
                zone = 'rt_' + uuid.uuid4().hex[:12]
                db.execute('''INSERT INTO restaurant_tables(id,site_id,camera,name,zone,points,waiter_id,cleaner_id,
                    wait_seconds,arrival_seconds,vacancy_seconds,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (identifier, body.site_id, body.camera, body.name, zone, points, body.waiter_id, body.cleaner_id,
                            body.wait_seconds, body.arrival_seconds, body.vacancy_seconds, now))
                geometry_changed = True
            _audit(db, actor, 'restaurant.table.save', identifier, now)
        return {'id': identifier, 'zone': zone, 'apply_required': geometry_changed}

    def delete_table(self, identifier, actor, now=None):
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            table = self._table(db, identifier)
            if table['state'] in ('waiting', 'served') or (table['people'] or 0) > 0:
                raise HTTPException(409, 'Нельзя удалить стол с текущим визитом')
            if table['visit_id']:
                self._resolve(db, table['visit_id'], now)
            db.execute('DELETE FROM restaurant_tables WHERE id=?', (identifier,))
            _audit(db, actor, 'restaurant.table.delete', identifier, now)
        return {'ok': True, 'apply_required': True}

    def _recipients(self, db, table, kind):
        preferred = table['waiter_id'] if kind == 'arrival' else (table['cleaner_id'] or table['waiter_id']) if kind == 'cleaning' else None
        if preferred:
            row = db.execute('SELECT * FROM restaurant_staff WHERE id=? AND site_id=? AND enabled=1',
                             (preferred, table['site_id'])).fetchone()
            if row:
                return [dict(row)], False
        managers = [dict(row) for row in db.execute("SELECT * FROM restaurant_staff WHERE site_id=? AND role='manager' AND enabled=1", (table['site_id'],))]
        return managers or [None], kind != 'escalation'

    def _sync_routes(self, db, table, kind, now):
        recipients, _ = self._recipients(db, table, kind)
        expected = {recipient['id'] if recipient else '' for recipient in recipients}
        existing = {row['recipient_id']: dict(row) for row in db.execute(
            'SELECT * FROM restaurant_alerts WHERE visit_id=? AND kind=?', (table['visit_id'], kind))}
        for identifier, alert in existing.items():
            if identifier not in expected and alert['resolved_at'] is None:
                db.execute("UPDATE restaurant_alerts SET resolved_at=?,delivery_state=CASE WHEN delivery_state IN ('pending','sending') THEN 'cancelled' ELSE delivery_state END WHERE id=?", (now, alert['id']))
        missing = {identifier for identifier in expected if identifier not in existing or existing[identifier]['resolved_at'] is not None}
        if missing:
            self._notify(db, table, kind, now, reopen=True, recipient_ids=missing)

    def _notify(self, db, table, kind, now, reopen=False, recipient_ids=None):
        recipients, fallback = self._recipients(db, table, kind)
        prefix = f"Стол «{table['name']}»: "
        if kind == 'arrival':
            message = prefix + 'гости сели. Подойдите к столу и подтвердите подход.'
        elif kind == 'cleaning':
            message = prefix + 'гости отсутствуют дольше заданного интервала. Проверьте уход и подготовьте стол.'
        else:
            visit = db.execute('SELECT arrived_at FROM restaurant_visits WHERE id=?', (table['visit_id'],)).fetchone()
            elapsed = max(0, int(now - visit['arrived_at'])) if visit else table['wait_seconds']
            message = prefix + f'подход официанта не подтверждён уже {elapsed // 60} мин {elapsed % 60:02d} с. Проверьте обслуживание.'
            if not self._fresh(table, now):
                message += ' Актуальные данные камеры недоступны.'
        if fallback:
            message += ' Ответственный сотрудник не назначен или отключён.'
        for recipient in recipients:
            recipient_id = recipient['id'] if recipient else ''
            if recipient_ids is not None and recipient_id not in recipient_ids:
                continue
            delivery = 'pending' if getattr(self.settings, 'telegram_bot_token', '') and recipient and recipient['telegram_chat_id'] else 'in_app'
            alert_message = message + ('' if recipient else ' Назначьте менеджера для получения уведомлений.')
            db.execute('''INSERT OR IGNORE INTO restaurant_alerts(id,site_id,table_id,table_name,visit_id,kind,
                recipient_id,recipient_name,at,message,delivery_state,retry_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (uuid.uuid4().hex, table['site_id'], table['id'], table['name'], table['visit_id'], kind,
                        recipient_id, recipient['name'] if recipient else 'Не назначен', now, alert_message, delivery, now))
            if reopen:
                db.execute('''UPDATE restaurant_alerts SET ack_at=NULL,ack_by=NULL,resolved_at=NULL,at=?,message=?,
                    delivery_state=?,delivery_attempts=0,retry_at=?,delivery_started=NULL,delivery_error=NULL,delivered_at=NULL
                    WHERE visit_id=? AND kind=? AND recipient_id=?''',
                           (now, alert_message, delivery, now, table['visit_id'], kind, recipient_id))

    @staticmethod
    def _resolve(db, visit_id, now, kinds=('arrival', 'escalation', 'cleaning')):
        marks = ','.join('?' for _ in kinds)
        db.execute(f'''UPDATE restaurant_alerts SET resolved_at=?,delivery_state=CASE
            WHEN delivery_state IN ('pending','sending') THEN 'cancelled' ELSE delivery_state END
            WHERE visit_id=? AND kind IN ({marks}) AND resolved_at IS NULL''', (now, visit_id, *kinds))

    def _fresh(self, table, now):
        observed = table['last_observed_at']
        return observed is not None and 0 <= now - observed <= self.stale_seconds and table['occupancy'] != 'unknown'

    def _advance(self, db, table, now):
        if not self._fresh(table, now):
            return
        if table['occupancy'] == 'occupied':
            table['absent_since'] = None
            if table['state'] in ('ready', 'needs_cleaning'):
                if table['candidate_since'] is None:
                    table['candidate_since'] = now
                if now - table['candidate_since'] >= table['arrival_seconds']:
                    if table['visit_id']:
                        self._resolve(db, table['visit_id'], now)
                    arrival = table['candidate_since']
                    visit_id = uuid.uuid4().hex
                    db.execute('''INSERT INTO restaurant_visits(id,table_id,site_id,arrived_at,deadline_at)
                        VALUES(?,?,?,?,?)''', (visit_id, table['id'], table['site_id'], arrival, arrival + table['wait_seconds']))
                    db.execute("UPDATE restaurant_tables SET state='waiting',visit_id=?,hold=0,revision=revision+1 WHERE id=?", (visit_id, table['id']))
                    table.update(state='waiting', visit_id=visit_id, hold=0, candidate_since=None)
                    self._notify(db, table, 'arrival', now)
                    _audit(db, 'restaurant', 'restaurant.seated', table['id'], now, visit_id)
            else:
                table['candidate_since'] = None
        else:
            table['candidate_since'] = None
            if table['state'] in ('waiting', 'served') and not table['hold']:
                if table['absent_since'] is None:
                    table['absent_since'] = now
                if now - table['absent_since'] >= table['vacancy_seconds']:
                    db.execute('UPDATE restaurant_visits SET ended_at=? WHERE id=? AND ended_at IS NULL', (table['absent_since'], table['visit_id']))
                    db.execute("UPDATE restaurant_tables SET state='needs_cleaning',hold=0,revision=revision+1 WHERE id=?", (table['id'],))
                    table['state'] = 'needs_cleaning'
                    self._resolve(db, table['visit_id'], now, kinds=('arrival', 'escalation'))
                    self._notify(db, table, 'cleaning', now)
                    _audit(db, 'restaurant', 'restaurant.vacated', table['id'], now, table['visit_id'])
            else:
                table['absent_since'] = None
        db.execute('UPDATE restaurant_tables SET candidate_since=?,absent_since=? WHERE id=?',
                   (table['candidate_since'], table['absent_since'], table['id']))

    def observe(self, camera_id, counts, *, available, now=None):
        """Apply a *current* zone snapshot in reception order, never historical entry zones.

        The transport must establish camera health and current Frigate availability.
        Missing or malformed zone counts are unknown, not zero.
        """
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM restaurant_tables WHERE camera=?', (camera_id,)).fetchall()
            camera = db.execute('SELECT site_id FROM cameras WHERE id=?', (camera_id,)).fetchone()
            for row in rows:
                table = dict(row)
                previous_time = table['last_observed_at']
                if previous_time is not None and now < previous_time:
                    continue
                count = counts.get(table['zone']) if isinstance(counts, dict) else None
                valid = (available and camera and camera['site_id'] == table['site_id'] and _module(db, table['site_id'])
                         and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000)
                if not valid:
                    db.execute("UPDATE restaurant_tables SET occupancy='unknown',people=NULL,candidate_since=NULL,absent_since=NULL,last_observed_at=? WHERE id=?", (now, table['id']))
                    continue
                # A long observation gap breaks debounce; elapsed darkness is not evidence.
                if previous_time is None or now - previous_time > self.stale_seconds:
                    table['candidate_since'] = table['absent_since'] = None
                table.update(people=count, occupancy='occupied' if count else 'empty', last_observed_at=now)
                db.execute('UPDATE restaurant_tables SET occupancy=?,people=?,last_observed_at=? WHERE id=?',
                           (table['occupancy'], count, now, table['id']))
                self._advance(db, table, now)

    def tick(self, now=None):
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT * FROM restaurant_tables').fetchall():
                table = dict(row)
                if not _module(db, table['site_id']):
                    continue
                if not self._fresh(table, now):
                    db.execute("UPDATE restaurant_tables SET occupancy='unknown',people=NULL,candidate_since=NULL,absent_since=NULL WHERE id=?", (table['id'],))
                    table.update(occupancy='unknown', people=None, candidate_since=None, absent_since=None)
                self._advance(db, table, now)
                if table['state'] != 'waiting' or not table['visit_id']:
                    continue
                visit = db.execute('SELECT * FROM restaurant_visits WHERE id=?', (table['visit_id'],)).fetchone()
                if visit and visit['greeted_at'] is None and visit['escalated_at'] is None and now >= visit['deadline_at']:
                    db.execute('UPDATE restaurant_visits SET escalated_at=? WHERE id=? AND escalated_at IS NULL', (now, table['visit_id']))
                    self._notify(db, table, 'escalation', now)
                    _audit(db, 'restaurant', 'restaurant.escalated', table['id'], now, table['visit_id'])

    @staticmethod
    def _can_view(table, staff):
        if not staff or table['site_id'] != staff['site_id']:
            return False
        if staff['role'] == 'manager':
            return True
        return table['waiter_id'] == staff['id'] if staff['role'] == 'waiter' else table['cleaner_id'] == staff['id']

    def _allowed_actions(self, table, staff, now):
        if staff and not self._can_view(table, staff):
            return []
        manager = not staff or staff['role'] == 'manager'
        waiter = manager or staff['role'] == 'waiter'
        cleaner = manager or (staff['role'] == 'cleaner' and table['cleaner_id'] == staff['id']) or (staff['role'] == 'waiter' and not table['cleaner_id'])
        actions = []
        if table['state'] in ('waiting', 'served') and waiter:
            if table['state'] == 'waiting':
                actions.append('greeted')
            actions.append('resume' if table['hold'] else 'hold')
        if table['state'] == 'needs_cleaning' and cleaner and self._fresh(table, now) and table['occupancy'] == 'empty':
            actions.append('cleaned')
        return actions

    def action(self, identifier, body, actor, staff=None, now=None):
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            staff = self._current_staff(db, staff)
            table = self._table(db, identifier)
            _require_module(db, table['site_id'])
            if staff and not self._can_view(table, staff):
                raise HTTPException(403, 'Нет доступа к этому столу')
            if body.visit_id != table['visit_id']:
                raise HTTPException(409, 'Визит изменился. Обновите стол перед действием')
            if body.revision is not None and body.revision != table['revision']:
                raise HTTPException(409, 'Состояние стола изменилось. Обновите страницу')
            # Repeated same greeting/hold is harmless; it does not change the original timestamp.
            if body.action == 'greeted' and table['state'] == 'served' and (not staff or staff['role'] in ('manager', 'waiter')):
                return {'ok': True, 'already_done': True}
            allowed = self._allowed_actions(table, staff, now)
            if body.action not in allowed:
                raise HTTPException(409 if not staff or self._can_view(table, staff) else 403,
                                    'Действие недоступно в текущем состоянии стола')
            if body.action == 'greeted':
                db.execute("UPDATE restaurant_tables SET state='served',revision=revision+1 WHERE id=?", (identifier,))
                db.execute('UPDATE restaurant_visits SET greeted_at=?,greeted_by=? WHERE id=? AND greeted_at IS NULL',
                           (now, actor, table['visit_id']))
                self._resolve(db, table['visit_id'], now, kinds=('arrival', 'escalation'))
            elif body.action == 'cleaned':
                if table['visit_id']:
                    self._resolve(db, table['visit_id'], now)
                db.execute("UPDATE restaurant_tables SET state='ready',visit_id=NULL,hold=0,candidate_since=NULL,absent_since=NULL,revision=revision+1 WHERE id=?", (identifier,))
            else:
                db.execute('UPDATE restaurant_tables SET hold=?,absent_since=NULL,revision=revision+1 WHERE id=?',
                           (int(body.action == 'hold'), identifier))
            _audit(db, actor, 'restaurant.' + body.action, identifier, now, table['visit_id'] or '')
        return {'ok': True}

    def acknowledge(self, identifier, actor, staff=None, now=None):
        now = _at(now)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            staff = self._current_staff(db, staff)
            alert = db.execute('SELECT * FROM restaurant_alerts WHERE id=?', (identifier,)).fetchone()
            if not alert:
                raise HTTPException(404, 'Уведомление не найдено')
            if staff and (alert['recipient_id'] != staff['id'] or alert['site_id'] != staff['site_id']):
                raise HTTPException(403, 'Это уведомление адресовано другому сотруднику')
            if alert['ack_at'] is None:
                db.execute("UPDATE restaurant_alerts SET ack_at=?,ack_by=?,delivery_state=CASE WHEN delivery_state IN ('pending','sending') THEN 'cancelled' ELSE delivery_state END WHERE id=?", (now, actor, identifier))
                _audit(db, actor, 'restaurant.alert.ack', identifier, now)
        return {'ok': True}

    def snapshot(self, site_id=None, staff_id=None, now=None):
        now = _at(now)
        with self.store.connect() as db:
            staff = self._staff(db, staff_id) if staff_id else None
            if staff and not staff['enabled']:
                raise HTTPException(401, 'Ссылка сотрудника отключена')
            if staff:
                site_id = staff['site_id']
            if site_id and not db.execute('SELECT id FROM sites WHERE id=?', (site_id,)).fetchone():
                raise HTTPException(404, 'Объект не найден')
            where, args = (' WHERE t.site_id=?', (site_id,)) if site_id else ('', ())
            rows = db.execute('''SELECT t.*,c.name AS camera_name,c.site_id AS camera_site,
                v.arrived_at,v.deadline_at,v.greeted_at,v.ended_at,v.escalated_at,
                w.name AS waiter_name,k.name AS cleaner_name
                FROM restaurant_tables t LEFT JOIN cameras c ON c.id=t.camera
                LEFT JOIN restaurant_visits v ON v.id=t.visit_id
                LEFT JOIN restaurant_staff w ON w.id=t.waiter_id
                LEFT JOIN restaurant_staff k ON k.id=t.cleaner_id''' + where + ' ORDER BY t.name', args).fetchall()
            tables, diagnostics = [], []
            for row in rows:
                table = dict(row)
                if staff and not self._can_view(table, staff):
                    continue
                enabled = _module(db, table['site_id']) and table['camera_site'] == table['site_id']
                if not enabled or not self._fresh(table, now):
                    table.update(occupancy='unknown', people=None)
                table['color'] = ('gray' if table['occupancy'] == 'unknown' or
                                  (table['occupancy'] == 'occupied' and table['state'] in ('ready', 'needs_cleaning'))
                                  else {'ready': 'green', 'waiting': 'red', 'served': 'red', 'needs_cleaning': 'yellow'}[table['state']])
                table['points'] = json.loads(table['points'])
                table['hold'] = bool(table['hold'])
                table['enabled'] = enabled
                table['overdue'] = table['state'] == 'waiting' and table['deadline_at'] is not None and now >= table['deadline_at']
                table['last_seen_at'] = table['last_observed_at']
                table['allowed_actions'] = self._allowed_actions(table, staff, now) if enabled else []
                table.pop('camera_site', None)
                tables.append(table)
                assigned = db.execute('SELECT id FROM restaurant_staff WHERE id=? AND enabled=1', (table['waiter_id'],)).fetchone()
                if not assigned:
                    diagnostics.append({'table_id': table['id'], 'kind': 'waiter_missing', 'message': f"Стол «{table['name']}»: назначьте официанта"})
            staff_rows = db.execute('SELECT * FROM restaurant_staff' + (' WHERE site_id=?' if site_id else '') + ' ORDER BY name', args).fetchall()
            employees = [self._public_staff(dict(s), private=not staff) for s in staff_rows if not staff or s['id'] == staff['id']]
            clause, params = (' WHERE site_id=?', [site_id]) if site_id else ('', [])
            if staff:
                clause += ' AND recipient_id=?'
                params.append(staff['id'])
            alerts = [dict(a) for a in db.execute('SELECT * FROM restaurant_alerts' + clause + ' ORDER BY at DESC,id DESC LIMIT 200', params)]
            for a in alerts:
                a.pop('delivery_started', None)
            active_sites = {table['site_id'] for table in tables}
            for identifier in active_sites:
                if not db.execute("SELECT id FROM restaurant_staff WHERE site_id=? AND role='manager' AND enabled=1 LIMIT 1", (identifier,)).fetchone():
                    diagnostics.append({'site_id': identifier, 'kind': 'manager_missing', 'message': 'Назначьте менеджера для просроченных вызовов'})
        return {'tables': tables, 'staff': employees, 'alerts': alerts, 'server_time': now,
                'health': dict(self.health), 'diagnostics': diagnostics,
                'telegram_configured': bool(getattr(self.settings, 'telegram_bot_token', '')),
                'current_staff': self._public_staff(staff) if staff else None}

    async def deliver(self, client, now=None):
        """Deliver a bounded durable outbox. Telegram has no exactly-once send guarantee."""
        if not getattr(self.settings, 'telegram_bot_token', ''):
            return
        now = _at(now)
        for _ in range(10):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('''SELECT a.*,s.telegram_chat_id,s.enabled AS recipient_enabled,s.site_id AS recipient_site
                    FROM restaurant_alerts a LEFT JOIN restaurant_staff s ON s.id=a.recipient_id
                    WHERE a.ack_at IS NULL AND a.resolved_at IS NULL AND
                    ((a.delivery_state='pending' AND a.delivery_attempts<8 AND a.retry_at<=?) OR
                     (a.delivery_state='sending' AND a.delivery_started<?))
                    ORDER BY a.at LIMIT 1''', (now, now - 60)).fetchone()
                if not row:
                    return
                alert = dict(row)
                if alert['delivery_attempts'] >= 8:
                    # A crash during the final send leaves an uncertain result.
                    # Make that uncertainty visible instead of claiming a delivery
                    # or leaving the task in "sending" forever.
                    db.execute("UPDATE restaurant_alerts SET delivery_state='failed',delivery_error=? WHERE id=?", ('Доставка не подтверждена после повторных попыток. Проверьте уведомление вручную.', alert['id']))
                    continue
                if not alert['recipient_enabled'] or alert['recipient_site'] != alert['site_id'] or not alert['telegram_chat_id'] or not _module(db, alert['site_id']):
                    db.execute("UPDATE restaurant_alerts SET delivery_state='in_app' WHERE id=?", (alert['id'],))
                    continue
                db.execute("UPDATE restaurant_alerts SET delivery_state='sending',delivery_started=?,delivery_attempts=delivery_attempts+1 WHERE id=?", (now, alert['id']))
            error = None
            try:
                response = await client.post('https://api.telegram.org/bot' + self.settings.telegram_bot_token + '/sendMessage',
                                             json={'chat_id': alert['telegram_chat_id'], 'text': alert['message']}, timeout=10)
                response.raise_for_status()
                if response.json().get('ok') is not True:
                    raise ValueError('Delivery rejected')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                error = 'Не удалось доставить сообщение. Проверьте бота и chat ID.'
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if error:
                    attempts = alert['delivery_attempts'] + 1
                    db.execute("UPDATE restaurant_alerts SET delivery_state=?,retry_at=?,delivery_error=? WHERE id=? AND delivery_state='sending'",
                               ('failed' if attempts >= 8 else 'pending', now + min(300, 5 * 2 ** attempts), error, alert['id']))
                else:
                    db.execute("UPDATE restaurant_alerts SET delivery_state='sent',delivered_at=?,delivery_error=NULL WHERE id=? AND delivery_state='sending'", (now, alert['id']))

    def cleanup(self, now=None):
        """Retain the current visit and open tasks, bound completed history."""
        now = _at(now)
        cutoff = now - int(getattr(self.settings, 'retention_days', 30)) * 86400
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''DELETE FROM restaurant_alerts WHERE at<? AND (resolved_at IS NOT NULL OR ack_at IS NOT NULL)
                AND NOT EXISTS(SELECT 1 FROM restaurant_tables WHERE visit_id=restaurant_alerts.visit_id)''', (cutoff,))
            db.execute('''DELETE FROM restaurant_visits WHERE ended_at IS NOT NULL AND ended_at<?
                AND NOT EXISTS(SELECT 1 FROM restaurant_tables WHERE visit_id=restaurant_visits.id)
                AND NOT EXISTS(SELECT 1 FROM restaurant_alerts WHERE visit_id=restaurant_visits.id)''', (cutoff,))


def register(app, store, engine, auth, writable):
    service = engine.restaurant

    def employee(request: Request):
        value = request.headers.get('Authorization', '')
        if not value.startswith('Bearer '):
            raise HTTPException(401, 'Откройте персональную ссылку сотрудника')
        return service.authenticate_staff(value[7:])

    @app.get('/api/restaurant')
    def overview(site_id: str | None = None, user=Depends(auth)):
        check_site(store, site_id)
        return service.snapshot(site_id=site_id)

    @app.post('/api/restaurant/staff', status_code=201)
    def create_staff(body: StaffInput, user=Depends(auth)):
        writable()
        return service.save_staff(body, user['username'])

    @app.put('/api/restaurant/staff/{identifier}')
    def update_staff(identifier: str, body: StaffInput, user=Depends(auth)):
        writable()
        return service.save_staff(body, user['username'], identifier=identifier)

    @app.post('/api/restaurant/staff/{identifier}/token')
    def rotate_staff_token(identifier: str, user=Depends(auth)):
        writable()
        return service.rotate_token(identifier, user['username'])

    @app.post('/api/restaurant/tables', status_code=201)
    def create_table(body: TableInput, user=Depends(auth)):
        writable()
        return service.save_table(body, user['username'])

    @app.put('/api/restaurant/tables/{identifier}')
    def update_table(identifier: str, body: TableInput, user=Depends(auth)):
        writable()
        return service.save_table(body, user['username'], identifier=identifier)

    @app.delete('/api/restaurant/tables/{identifier}')
    def remove_table(identifier: str, user=Depends(auth)):
        writable()
        return service.delete_table(identifier, user['username'])

    @app.post('/api/restaurant/tables/{identifier}/actions')
    def act_on_table(identifier: str, body: TableAction, user=Depends(auth)):
        writable()
        return service.action(identifier, body, user['username'])

    @app.post('/api/restaurant/alerts/{identifier}/ack')
    def acknowledge_alert(identifier: str, user=Depends(auth)):
        writable()
        return service.acknowledge(identifier, user['username'])

    @app.get('/api/restaurant/portal')
    def staff_portal(staff=Depends(employee)):
        return service.snapshot(staff_id=staff['id'])

    @app.post('/api/restaurant/portal/tables/{identifier}/actions')
    def staff_table_action(identifier: str, body: TableAction, staff=Depends(employee)):
        writable()
        return service.action(identifier, body, 'staff:' + staff['id'], staff=staff)

    @app.post('/api/restaurant/portal/alerts/{identifier}/ack')
    def staff_acknowledge(identifier: str, staff=Depends(employee)):
        writable()
        return service.acknowledge(identifier, 'staff:' + staff['id'], staff=staff)
