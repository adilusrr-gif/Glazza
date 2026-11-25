"""Conservative, camera-local observations and cross-camera review hypotheses."""
import hashlib
import json

from .retail import MAX_GAP_SECONDS, MAX_POINTS, MIN_ZONE_SECONDS, _inside, _intersect, _edges, _finite
from .retail_appearance import compare_clothing
from .retail_multicam_config import initialize as initialize_config

ROLES = ('shelf', 'checkout', 'exit')
MAX_HEALTH_GAP = 15
SETTLE_SECONDS = 10
# Observation retention exceeds the two 600-second search horizons, even after retries.
STATE_TTL = 7200


def initialize(store):
    initialize_config(store)
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_multicam_tracks(
                site TEXT NOT NULL, generation REAL NOT NULL, camera TEXT NOT NULL, role TEXT NOT NULL,
                zone TEXT NOT NULL, source_id TEXT NOT NULL, origin_event TEXT, start REAL NOT NULL, end REAL,
                quality TEXT NOT NULL, first_at REAL, last_at REAL, has_snapshot INTEGER NOT NULL DEFAULT 0,
                snapshot_at REAL, box TEXT, signature TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(site,generation,camera,source_id)
            );
            CREATE INDEX IF NOT EXISTS multicam_time ON retail_multicam_tracks(site,generation,role,start);
            CREATE INDEX IF NOT EXISTS multicam_exits ON retail_multicam_tracks(role,quality,checked);
            CREATE TABLE IF NOT EXISTS retail_multicam_health(
                camera TEXT NOT NULL, at REAL NOT NULL, healthy INTEGER NOT NULL, PRIMARY KEY(camera,at)
            );
            CREATE TABLE IF NOT EXISTS retail_multicam_links(
                event TEXT PRIMARY KEY,site TEXT NOT NULL,generation REAL NOT NULL,exit_source TEXT NOT NULL,
                lo REAL NOT NULL,hi REAL NOT NULL,dirty INTEGER NOT NULL DEFAULT 0,withdrawn INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS multicam_link_time ON retail_multicam_links(site,generation,hi);
        """)


def configured_zones(store):
    return {r[role+'_camera']: r[role+'_zone']
            for r in store.rows("SELECT * FROM retail_multicam_routes WHERE state='active'") for role in ROLES}


def configured_cameras(store):
    return set(configured_zones(store))


def _classify(event, polygon, zone):
    """Use zone membership and actual positions; retain unknown and incomplete tracks as blockers."""
    start, end = event['start_time'], event.get('end_time')
    if end is None:
        return 'active', None, None
    data = event.get('data')
    path = data.get('path_data') if isinstance(data, dict) else None
    zones = event.get('zones', event.get('entered_zones'))
    if not isinstance(zones, list) or not isinstance(path, list) or not 2 <= len(path) <= MAX_POINTS:
        return 'uncertain', None, None
    previous = None
    runs = []
    touched = False
    last_inside = False
    for step in path:
        if not isinstance(step, (list, tuple)) or len(step) != 2:
            return 'uncertain', None, None
        point, at = step
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(_finite(n) and 0 <= n <= 1 for n in point) or not _finite(at) or not start <= at <= end):
            return 'uncertain', None, None
        if previous is not None:
            if previous == step:
                continue
            if not 0 < at - previous[1] <= MAX_GAP_SECONDS:
                return 'uncertain', None, None
            if not touched and any(_intersect(previous[0], point, a, b) for a,b in _edges(polygon)):
                touched = True
        elif at - start > MAX_GAP_SECONDS:
            return 'uncertain', None, None
        inside = _inside(point, polygon)
        if inside:
            touched = True
            if last_inside:
                runs[-1][1] = at
            else:
                runs.append([at, at])
        last_inside, previous = inside, step
    if end - previous[1] > MAX_GAP_SECONDS:
        return 'uncertain', None, None
    if zone not in zones and not touched:
        return 'absent', None, None
    if zone not in zones or not event.get('has_clip') or len(runs) != 1:
        return 'uncertain', None, None
    first, last = runs[0]
    if last-first < MIN_ZONE_SECONDS:
        return 'uncertain', None, None
    return ('terminal' if last_inside else 'visit'), first, last


def _dirty(db, site, generation, start, end):
    # Re-evaluate negative decisions only when evidence in their horizon changes.
    # A missing snapshot may become available after an exit was first evaluated.
    rule = db.execute("SELECT max_seconds FROM retail_multicam_routes WHERE site_id=? AND activated_at=?",
                      (site,generation)).fetchone()
    if rule:
        radius = 2 * rule['max_seconds']
        db.execute("""UPDATE retail_multicam_tracks SET checked=0 WHERE site=? AND generation=? AND role='exit'
            AND checked!=0 AND first_at>=? AND (first_at<=? OR ? IS NULL)""",
            (site,generation,start-radius,end+radius if end is not None else None,end))
    db.execute("""UPDATE retail_multicam_links SET dirty=1 WHERE site=? AND generation=? AND withdrawn=0
        AND hi>=? AND (lo<=? OR ? IS NULL)""", (site,generation,start,end,end))


def observe(store, camera, event, origin_event_id, site_id):
    if event.get('label') != 'person' or event.get('false_positive'):
        return
    camera_id, start, end = camera['id'], event.get('start_time'), event.get('end_time')
    if not _finite(start) or (end is not None and (not _finite(end) or end < start)):
        return
    rows = store.rows("""SELECT * FROM retail_multicam_routes WHERE site_id=? AND state='active'
        AND ? IN (shelf_camera,checkout_camera,exit_camera)""", (site_id,camera_id))
    if not rows:
        return
    rule = rows[0]
    if end is not None and end < rule['activated_at']:
        return
    role = next(role for role in ROLES if rule[role+'_camera']==camera_id)
    zone = rule[role+'_zone']
    polygon = next((z['points'] for z in camera['zones'] if z['name']==zone and not z['restricted']), None)
    quality, first, last = _classify(event, polygon, zone) if polygon else ('uncertain',None,None)
    if start < rule['activated_at'] or (quality=='visit' and role!='checkout'):
        quality, first, last = 'uncertain', None, None
    has_snapshot = int(bool(event.get('has_snapshot')))
    data = event.get('data')
    snapshot_at = data.get('snapshot_frame_time') if isinstance(data,dict) else None
    if not _finite(snapshot_at) or snapshot_at < start or (end is not None and snapshot_at > end):
        snapshot_at = None
    box = data.get('box') if isinstance(data,dict) else None
    if (not isinstance(box,(tuple,list)) or len(box)!=4 or not all(_finite(v) and 0 <= v <= 1 for v in box)
            or min(box[2:]) <= 0 or box[0]+box[2] > 1.0001 or box[1]+box[3] > 1.0001):
        box = None
    packed_box = json.dumps(box) if box is not None else None
    generation = rule['activated_at']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute("SELECT 1 FROM retail_multicam_routes WHERE site_id=? AND activated_at=? AND state='active'",
                             (site_id,generation)).fetchone()
        if not current:
            return
        changed = db.execute("""INSERT INTO retail_multicam_tracks(
            site,generation,camera,role,zone,source_id,origin_event,start,end,quality,first_at,last_at,has_snapshot,snapshot_at,box)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(site,generation,camera,source_id) DO UPDATE SET
            end=excluded.end,origin_event=excluded.origin_event,quality=excluded.quality,
            first_at=excluded.first_at,last_at=excluded.last_at,has_snapshot=excluded.has_snapshot,
            signature=CASE WHEN snapshot_at IS excluded.snapshot_at AND box IS excluded.box AND excluded.has_snapshot=1 THEN signature END,
            snapshot_at=excluded.snapshot_at,box=excluded.box,retry_at=0,attempts=0,checked=0
            WHERE quality!='lost' AND (end IS NULL OR excluded.end IS NOT NULL)
            AND (end,origin_event,quality,first_at,last_at,has_snapshot,snapshot_at,box) IS NOT
                (excluded.end,excluded.origin_event,excluded.quality,excluded.first_at,excluded.last_at,excluded.has_snapshot,excluded.snapshot_at,excluded.box)""",
            (site_id,generation,camera_id,role,zone,str(event['id']),origin_event_id,start,end,
             quality,first,last,has_snapshot,snapshot_at,packed_box)).rowcount
        if changed:
            _dirty(db,site_id,generation,start,end)


def invalidate_track(store, camera, source_id, start, end, site_id):
    if not _finite(start) or not _finite(end) or end < start:
        return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        r = db.execute("""SELECT * FROM retail_multicam_routes WHERE site_id=? AND state='active'
            AND ? IN (shelf_camera,checkout_camera,exit_camera) AND activated_at<=?""", (site_id,camera,end)).fetchone()
        if not r:
            return
        role = next(k for k in ROLES if r[k+'_camera']==camera)
        db.execute("""INSERT INTO retail_multicam_tracks(site,generation,camera,role,zone,source_id,start,end,quality)
            VALUES(?,?,?,?,?,?,?,?,'lost') ON CONFLICT(site,generation,camera,source_id) DO UPDATE SET
            quality='lost',end=MAX(COALESCE(end,excluded.end),excluded.end),signature=NULL,checked=0""",
            (site_id,r['activated_at'],camera,role,r[role+'_zone'],source_id,start,end))
        _dirty(db,site_id,r['activated_at'],start,end)


def record_health(store, camera_id, timestamp, healthy):
    if not _finite(timestamp):
        return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""INSERT INTO retail_multicam_health VALUES(?,?,?) ON CONFLICT(camera,at) DO UPDATE SET
            healthy=MIN(healthy,excluded.healthy)""", (camera_id,timestamp,int(bool(healthy))))
        if not healthy:
            db.execute("""UPDATE retail_multicam_links SET dirty=1 WHERE withdrawn=0 AND lo<=? AND hi>=?
                AND site IN (SELECT site_id FROM retail_multicam_routes WHERE ? IN(shelf_camera,checkout_camera,exit_camera))""",
                (timestamp,timestamp,camera_id))


def snapshot_jobs(store, now, limit=8):
    jobs = store.rows("""SELECT t.site AS site_id,t.generation AS activated_at,t.camera,t.source_id,t.origin_event,t.snapshot_at,t.box,t.end AS ended
        FROM retail_multicam_tracks t JOIN retail_multicam_routes r ON r.site_id=t.site AND r.activated_at=t.generation
        WHERE r.state='active' AND t.quality IN ('terminal','visit') AND t.signature IS NULL AND t.has_snapshot=1
        AND t.box IS NOT NULL AND t.attempts<3 AND t.retry_at<=? AND t.end>=? ORDER BY t.retry_at,t.end DESC LIMIT ?""",
        (now,now-STATE_TTL,max(1,min(128,limit))))
    for job in jobs:
        job['box'] = json.loads(job['box'])
    return jobs


def snapshot_result(store, job, descriptor, now):
    signature = json.dumps(descriptor) if descriptor is not None else None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        changed = db.execute("""UPDATE retail_multicam_tracks SET signature=?, retry_at=?,attempts=attempts+1
            WHERE site=? AND generation=? AND camera=? AND source_id=? AND snapshot_at IS ?
            AND box IS ? AND has_snapshot=1 AND quality IN ('terminal','visit') AND signature IS NULL""",
            (signature,now+60,job['site_id'],job['activated_at'],job['camera'],job['source_id'],job['snapshot_at'],json.dumps(job['box']))).rowcount
        if changed and signature:
            _dirty(db,job['site_id'],job['activated_at'],job['ended'],job['ended'])


def _covered(db, route, lo, hi):
    if route['activated_at'] > lo:
        return False
    for role in ROLES:
        samples = db.execute("""SELECT at,healthy FROM retail_multicam_health WHERE camera=? AND at BETWEEN ? AND ? ORDER BY at""",
                             (route[role+'_camera'],lo-MAX_HEALTH_GAP,hi+MAX_HEALTH_GAP)).fetchall()
        before = [s for s in samples if s['at'] <= lo]
        after = [s for s in samples if s['at'] >= hi]
        if not before or not after:
            return False
        relevant = [s for s in samples if before[-1]['at'] <= s['at'] <= after[0]['at']]
        if any(not s['healthy'] for s in relevant) or any(b['at']-a['at']>MAX_HEALTH_GAP for a,b in zip(relevant,relevant[1:])):
            return False
    return True


def _possible(db, route, role, lo, hi, boundary=None):
    # Unknown/active persons cover the entire observed track. Known visits use
    # the measured handoff timestamp/interval, never a clip upload or polling time.
    rows = db.execute("""SELECT * FROM retail_multicam_tracks WHERE site=? AND generation=? AND role=?
        AND start<=? AND (end>=? OR end IS NULL) AND quality!='absent' LIMIT 101""",
        (route['site_id'],route['activated_at'],role,hi,lo)).fetchall()
    if len(rows)>100:
        return None
    result = []
    for row in rows:
        if row['quality'] in ('terminal','visit'):
            first = row[boundary] if boundary else row['first_at']
            last = row[boundary] if boundary else row['last_at']
            if last < lo or first > hi:
                continue
        result.append(row)
    return result


def _match(db, route, ex):
    if not ex or ex['quality'] != 'terminal':
        return None
    arrive = ex['first_at']
    minimum, maximum = route['min_seconds'], route['max_seconds']
    lo, hi = arrive-maximum, arrive+maximum
    if not _covered(db,route,lo,hi):
        return None
    shelves = _possible(db,route,'shelf',lo,arrive-minimum,'last_at')
    if not shelves or len(shelves)!=1 or shelves[0]['quality']!='terminal':
        return None
    shelf = shelves[0]
    depart = shelf['last_at']
    exits = _possible(db,route,'exit',depart+minimum,depart+maximum,'first_at')
    if not exits or len(exits)!=1 or exits[0]['source_id']!=ex['source_id'] or shelf['end'] > ex['start']:
        return None
    checkouts = _possible(db,route,'checkout',depart,arrive)
    if checkouts is None or len(checkouts)>1:
        return None
    journey = [shelf,ex]
    if checkouts:
        checkout = checkouts[0]
        if (checkout['quality'] not in ('terminal','visit') or shelf['end'] > checkout['start']
                or checkout['end'] > ex['start']):
            return None
        # A checkout track must also belong uniquely to this ordered pair.
        # Competing compatible journeys outside the immediate shelf/exit window
        # may otherwise both reuse the same evidence.
        if not _covered(db,route,checkout['first_at']-maximum,hi):
            return None
        other_shelves = _possible(db,route,'shelf',checkout['first_at']-maximum,checkout['first_at'],'last_at')
        other_exits = _possible(db,route,'exit',checkout['last_at'],checkout['last_at']+maximum,'first_at')
        if not other_shelves or len(other_shelves)!=1 or not other_exits or len(other_exits)!=1:
            return None
        journey.insert(1,checkout)
    if any(not r['signature'] for r in journey):
        return None
    signatures = [json.loads(r['signature']) for r in journey]
    if any(compare_clothing(a,b)!='compatible' for idx,a in enumerate(signatures) for b in signatures[idx+1:]):
        return None
    return journey


def _origins(journey):
    return [r['origin_event'] for r in journey]


def _save(db, route, journey):
    shelf, ex = journey[0],journey[-1]
    bypass = len(journey)==2
    kind = 'theft' if bypass else 'retail_journey'
    source_id = f"{route['site_id']}:{route['activated_at']}:{ex['camera']}:{ex['source_id']}"
    identifier = hashlib.sha256(f'retail_multicam:{source_id}:{kind}'.encode()).hexdigest()[:32]
    evidence = [{'role':r['role'],'camera':r['camera'],'zone':r['zone'],'origin_event':r['origin_event'],
                 'start_seconds':r['first_at']-r['start'],'end_seconds':r['last_at']-r['start']} for r in journey]
    details = {'signal':'multicam_shelf_to_exit' if bypass else 'multicam_via_checkout',
               'association_state':'linked','review_required':bypass,'evidence':evidence,
               'origin_event':ex['origin_event'],'start_seconds':evidence[-1]['start_seconds'],'end_seconds':evidence[-1]['end_seconds'],
               'transition_seconds':ex['first_at']-shelf['last_at'],
               'checkout_camera':route['checkout_camera'],'checkout_zone':route['checkout_zone']}
    db.execute("""INSERT OR IGNORE INTO events(id,source,source_id,camera,kind,score,started,ended,status,details,site_id)
        VALUES(?,'retail_multicam',?,?,?,NULL,?,?,?,?,?)""", (
        identifier,source_id,ex['camera'],kind,shelf['last_at'],ex['last_at'],'new' if bypass else 'confirmed',
        json.dumps(details),route['site_id']))
    db.execute("""INSERT OR IGNORE INTO retail_multicam_links(event,site,generation,exit_source,lo,hi)
        VALUES(?,?,?,?,?,?)""",(identifier,route['site_id'],route['activated_at'],ex['source_id'],
        journey[1]['first_at']-route['max_seconds'],ex['first_at']+route['max_seconds']))


def _withdraw(db, link, now):
    reason = 'Дополнительные наблюдения изменили связь между треками. Требуется сверка исходных записей.'
    db.execute("""UPDATE events SET details=json_set(details,'$.association_state','withdrawn','$.withdrawal_reason',?,
        '$.review_required',json('false')),status=CASE WHEN status='new' THEN 'dismissed' ELSE status END WHERE id=?""",
        (reason,link['event']))
    db.execute("UPDATE retail_multicam_links SET dirty=0,withdrawn=1 WHERE event=?",(link['event'],))
    db.execute("INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,'system','retail.multicam.withdraw',?,?)",
               (now,link['event'],reason))


def advance(store, watermark):
    """Run after a complete Frigate poll including open tracks. Absence during downtime cannot link people."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for rule in db.execute("SELECT * FROM retail_multicam_routes WHERE state='active'").fetchall():
            scope = rule['site_id'],rule['activated_at']
            dirty = db.execute("""SELECT l.*,e.details FROM retail_multicam_links l JOIN events e ON e.id=l.event
                WHERE l.site=? AND l.generation=? AND l.dirty=1 AND l.withdrawn=0 LIMIT 100""",scope).fetchall()
            for link in dirty:
                ex = db.execute("SELECT * FROM retail_multicam_tracks WHERE site=? AND generation=? AND role='exit' AND source_id=?",
                                (*scope,link['exit_source'])).fetchone()
                journey = _match(db,rule,ex)
                prior = [e['origin_event'] for e in json.loads(link['details'])['evidence']]
                if journey and _origins(journey)==prior:
                    db.execute("UPDATE retail_multicam_links SET dirty=0 WHERE event=?", (link['event'],))
                else:
                    _withdraw(db,link,watermark)
            exits = db.execute("""SELECT t.* FROM retail_multicam_tracks t WHERE site=? AND generation=? AND role='exit'
                AND quality='terminal' AND first_at<=? AND first_at>=?
                AND (checked=0 OR (first_at>=? AND checked<=?))
                AND NOT EXISTS (SELECT 1 FROM retail_multicam_links l WHERE l.site=t.site AND l.generation=t.generation
                    AND l.exit_source=t.source_id)
                ORDER BY checked,first_at DESC LIMIT 100""", (*scope,watermark-rule['max_seconds']-SETTLE_SECONDS,
                watermark-STATE_TTL,watermark-rule['max_seconds']-60,watermark-15)).fetchall()
            for ex in exits:
                journey = _match(db,rule,ex)
                if journey:
                    _save(db,rule,journey)
                db.execute("UPDATE retail_multicam_tracks SET checked=? WHERE site=? AND generation=? AND camera=? AND source_id=?",
                           (watermark,*scope,ex['camera'],ex['source_id']))


def cleanup(store, now):
    with store.connect() as db:
        db.execute("DELETE FROM retail_multicam_health WHERE at<?",(now-STATE_TTL-1200,))
        db.execute("""DELETE FROM retail_multicam_tracks WHERE end<? OR NOT EXISTS(
            SELECT 1 FROM retail_multicam_routes r WHERE r.site_id=site AND r.activated_at=generation AND r.state='active')""",
            (now-STATE_TTL-1200,))
        db.execute("DELETE FROM retail_multicam_links WHERE NOT EXISTS(SELECT 1 FROM events WHERE id=event)")
