"""Temporary, site-scoped store visits built from positive Frigate observations."""

import json
import math
import time
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict

from . import visits_config
from .retail import _inside
from .retail_appearance import compare_clothing

IDLE_SECONDS = 30 * 60
HISTORY_SECONDS = 24 * 3600
POSITION_FRESH_SECONDS = 15
# Frigate publishes statistics roughly every 15 seconds, plus collection overhead.
HEALTH_GAP_SECONDS = 20


def initialize(store):
    visits_config.initialize(store)
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS retail_visits(
                id TEXT PRIMARY KEY, site_id TEXT NOT NULL, generation REAL NOT NULL,
                entrance_event TEXT UNIQUE NOT NULL, started_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS visits_site ON retail_visits(site_id,started_at DESC);
            CREATE TABLE IF NOT EXISTS retail_visit_tracks(
                origin_event TEXT PRIMARY KEY, source_id TEXT NOT NULL, camera TEXT NOT NULL, site_id TEXT NOT NULL,
                generation REAL NOT NULL, started REAL NOT NULL, last_seen REAL NOT NULL, ended REAL, has_clip INTEGER,
                entry_phase INTEGER DEFAULT 0, zone TEXT, path TEXT NOT NULL, snapshot_at REAL, box TEXT,
                signature TEXT, retry_at REAL DEFAULT 0, invalid INTEGER DEFAULT 0,
                visit_id TEXT, ambiguous INTEGER DEFAULT 0,
                UNIQUE(site_id,generation,camera,source_id));
            CREATE INDEX IF NOT EXISTS visit_track_time ON retail_visit_tracks(site_id,generation,last_seen);
            CREATE INDEX IF NOT EXISTS visit_track_timeline ON retail_visit_tracks(visit_id,started);
            CREATE TABLE IF NOT EXISTS retail_visit_health(camera TEXT, at REAL, healthy INTEGER, PRIMARY KEY(camera,at));
        """)


def configured_cameras(store):
    return {
        r['cam']
        for r in store.rows("""SELECT DISTINCT j.value AS cam FROM retail_visit_configs c,
        json_each(c.camera_ids) j WHERE c.state='active'""")
    }


def configured_zones(store):
    return {
        r['entrance_camera']: {n for n in [r['entrance_zone'], r['inside_zone']] if n}
        for r in store.rows(
            "SELECT entrance_camera,entrance_zone,inside_zone FROM retail_visit_configs WHERE state='active'"
        )
    }


def finite(x):
    return type(x) in (float, int) and math.isfinite(x)


def path_points(raw, started, cutoff):
    if not isinstance(raw, (tuple, list)) or len(raw) > 4096:
        return []
    points = []
    for sample in raw:
        if not isinstance(sample, (tuple, list)) or len(sample) != 2:
            return []
        point, at = sample
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or not all(finite(n) and 0 <= n <= 1 for n in point)
            or not finite(at)
            or not started <= at <= cutoff
        ):
            return []
        if points and at <= points[-1][1]:
            if at == points[-1][1] and list(point) == list(points[-1][0]):
                continue
            return []
        points.append([list(point), at])
    return points


def entry_state(config, camera, zones, points, phase, seen, previous_seen):
    entrance, inside = config['entrance_zone'], config['inside_zone']
    if camera['id'] != config['entrance_camera'] or phase in (-1, 2):
        return phase, None
    polygons = {z['name']: z['points'] for z in camera['zones']}
    if entrance not in polygons or (inside and inside not in polygons):
        return phase, None
    if inside is None:
        if entrance in zones:
            at = next((t for p, t in points if _inside(p, polygons[entrance])), seen)
            return 2, at
        return phase, None
    # Prefer capture-time observations; never derive order from the zones array.
    prior_door = phase == 1
    door_in_path = False
    for p, at in points:
        if inside in zones and _inside(p, polygons[inside]):
            # A late, complete path can reveal interior before the previously
            # reported door. Arrival order of HTTP updates is not capture order.
            if not door_in_path and (not prior_door or at <= previous_seen):
                return -1, None
            return 2, at
        if entrance in zones and _inside(p, polygons[entrance]):
            door_in_path = True
            phase = 1
    if prior_door and seen > previous_seen and inside in zones and entrance in zones:
        return 2, seen
    if inside in zones and entrance not in zones:
        return -1, None
    if entrance in zones and inside not in zones:
        phase = 1
    return phase, None


def observe(store, camera, event, origin_event_id, site_id):
    start, end = event.get('start_time'), event.get('end_time')
    now = time.time()
    if (
        event.get('label') != 'person'
        or event.get('false_positive')
        or not finite(start)
        or start > now + 1
        or (end is not None and (not finite(end) or not start <= end <= now + 1))
    ):
        return
    data = event.get('data') or {}
    if not isinstance(data, dict):
        return
    points = path_points(data.get('path_data'), start, end if end is not None else now + 1)
    timed = [
        at
        for at in (data.get('frame_time'), data.get('snapshot_frame_time'), event.get('frame_time'))
        if finite(at) and start <= at <= now + 1
    ]
    seen = end if end is not None else max([start, *timed, *([points[-1][1]] if points else [])])
    if seen < now - IDLE_SECONDS:
        return
    snap = data.get('snapshot_frame_time', data.get('frame_time'))
    snap = snap if finite(snap) and start <= snap <= now + 1 else None
    box = data.get('box')
    box = (
        json.dumps(box)
        if isinstance(box, (tuple, list)) and len(box) == 4 and all(finite(n) for n in box)
        else None
    )
    entered = event.get('zones', event.get('entered_zones')) or []
    if not isinstance(entered, list):
        return
    with store.connect() as db:
        config = db.execute(
            """SELECT c.* FROM retail_visit_configs c JOIN cameras cam
            ON cam.site_id=c.site_id AND cam.id=? WHERE c.site_id=? AND c.state='active' AND c.activated_at<=?
            AND EXISTS(SELECT 1 FROM json_each(c.camera_ids) WHERE value=cam.id)""",
            (camera['id'], site_id, start),
        ).fetchone()
        if not config:
            return
        db.execute('BEGIN IMMEDIATE')
        # Recheck under the same lock as the upsert so reconfiguration cannot relabel an old event.
        config = db.execute(
            "SELECT * FROM retail_visit_configs WHERE site_id=? AND activated_at=? AND state='active'",
            (site_id, config['activated_at']),
        ).fetchone()
        if not config:
            return
        old = db.execute(
            """SELECT * FROM retail_visit_tracks WHERE site_id=? AND generation=? AND camera=? AND source_id=?""",
            (site_id, config['activated_at'], camera['id'], event['id']),
        ).fetchone()
        if old and (
            old['invalid']
            or old['started'] != start
            or seen < old['last_seen']
            or (old['ended'] is not None and end is None)
        ):
            return
        phase = old['entry_phase'] if old else 0
        phase, entry = entry_state(
            config, camera, entered, points, phase, seen, old['last_seen'] if old else start
        )
        zone = config['entrance_zone'] if phase == 2 else None
        # Frigate may resend a cumulative path or just the newest points. Keep
        # actual capture times across both shapes; never interpolate missing motion.
        history = {at: xy for xy, at in json.loads(old['path'])} if old else {}
        history.update({at: xy for xy, at in points[-32:]})
        stored_path = json.dumps([[history[at], at] for at in sorted(history)[-32:]])
        key = old['origin_event'] if old else origin_event_id
        changed = db.execute(
            """INSERT INTO retail_visit_tracks
            (origin_event,source_id,camera,site_id,generation,started,last_seen,ended,has_clip,entry_phase,zone,path,snapshot_at,box)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(origin_event) DO UPDATE SET
            last_seen=excluded.last_seen,ended=COALESCE(excluded.ended,ended),has_clip=MAX(has_clip,excluded.has_clip),
            entry_phase=excluded.entry_phase,zone=excluded.zone,path=excluded.path,
            snapshot_at=COALESCE(excluded.snapshot_at,snapshot_at),box=COALESCE(excluded.box,box),
            retry_at=CASE WHEN excluded.ended IS NOT ended OR excluded.snapshot_at IS NOT snapshot_at THEN 0 ELSE retry_at END
            WHERE generation=excluded.generation AND site_id=excluded.site_id AND camera=excluded.camera""",
            (
                key,
                event['id'],
                camera['id'],
                site_id,
                config['activated_at'],
                start,
                seen,
                end,
                int(bool(event.get('has_clip'))),
                phase,
                zone,
                stored_path,
                snap,
                box,
            ),
        ).rowcount
        if changed and entry is not None:
            vid = uuid.uuid4().hex
            db.execute(
                """INSERT INTO retail_visits VALUES(?,?,?,?,?) ON CONFLICT(entrance_event) DO NOTHING""",
                (vid, site_id, config['activated_at'], key, entry),
            )
            db.execute(
                'UPDATE retail_visit_tracks SET visit_id=(SELECT id FROM retail_visits WHERE entrance_event=?) WHERE origin_event=?',
                (key, key),
            )


def snapshot_jobs(store, now, limit):
    rows = store.rows(
        """SELECT t.origin_event,t.camera,t.site_id,t.source_id,t.started,t.snapshot_at,t.ended,t.box,t.generation
        FROM retail_visit_tracks t JOIN retail_visit_configs c ON t.site_id=c.site_id AND t.generation=c.activated_at
        WHERE c.state='active' AND t.invalid=0 AND t.signature IS NULL AND t.last_seen>=? AND t.retry_at<=?
        AND (t.ended IS NULL OR t.box IS NOT NULL)
        ORDER BY t.retry_at,t.last_seen DESC,t.origin_event LIMIT ?""",
        (now - IDLE_SECONDS, now, limit),
    )
    for row in rows:
        row['box'] = json.loads(row['box']) if row['box'] else None
    return rows


def snapshot_result(store, job, descriptor, now):
    signature = json.dumps(descriptor) if compare_clothing(descriptor, descriptor) == 'compatible' else None
    store.execute(
        """UPDATE retail_visit_tracks SET signature=?,retry_at=?
        WHERE origin_event=? AND generation=? AND ended IS ? AND snapshot_at IS ? AND box IS ? AND signature IS NULL AND invalid=0
        AND EXISTS(SELECT 1 FROM retail_visit_configs c WHERE c.site_id=retail_visit_tracks.site_id
            AND c.activated_at=retail_visit_tracks.generation AND c.state='active')""",
        (
            signature,
            now + 30,
            job['origin_event'],
            job['generation'],
            job['ended'],
            job['snapshot_at'],
            json.dumps(job['box']) if job['box'] else None,
        ),
    )


def invalidate_track(store, camera_id, source_id, start, end, site_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            """SELECT origin_event,visit_id FROM retail_visit_tracks
            WHERE camera=? AND source_id=? AND site_id=? AND started=?""",
            (camera_id, source_id, site_id, start),
        ).fetchone()
        if not row:
            return
        db.execute(
            """UPDATE retail_visit_tracks SET invalid=1,ended=COALESCE(ended,last_seen),signature=NULL
            WHERE origin_event=?""",
            (row['origin_event'],),
        )
        if row['visit_id']:
            # A downstream position once supported by this observation must vanish
            # immediately, even before the next successful full NVR poll.
            db.execute(
                """UPDATE retail_visit_tracks SET visit_id=NULL WHERE visit_id=? AND started>=?
                AND entry_phase!=2""",
                (row['visit_id'], start),
            )
            db.execute(
                """UPDATE retail_visit_tracks SET ambiguous=1 WHERE origin_event=(
                SELECT origin_event FROM retail_visit_tracks WHERE visit_id=? ORDER BY started DESC LIMIT 1)""",
                (row['visit_id'],),
            )


def record_health(store, camera, timestamp, healthy):
    if finite(timestamp):
        store.execute(
            """INSERT INTO retail_visit_health VALUES(?,?,?) ON CONFLICT(camera,at) DO UPDATE
            SET healthy=MIN(healthy,excluded.healthy)""",
            (camera, timestamp, int(bool(healthy))),
        )


def _healthy(samples, times, start, end):
    first = bisect_right(times, start) - 1
    stop = bisect_right(times, end)
    if first < 0 or start - times[first] > HEALTH_GAP_SECONDS or end - times[stop - 1] > HEALTH_GAP_SECONDS:
        return False
    return all(samples[i] for i in range(first, stop)) and all(
        times[i] - times[i - 1] <= HEALTH_GAP_SECONDS for i in range(first + 1, stop)
    )


def _assign(db, config, now):
    # Only the recent window can grow. Stable older observations remain timeline anchors
    # when a shopper continues moving through the store for longer than one idle period.
    edges = json.loads(config['transitions'])
    oldest = now - IDLE_SECONDS
    buffer = max(step['max_seconds'] for step in edges)
    rows = [
        dict(r)
        for r in db.execute(
            """SELECT t.*,v.id AS entrance_visit FROM retail_visit_tracks t
        LEFT JOIN retail_visits v ON t.origin_event=v.entrance_event
        WHERE t.site_id=? AND t.generation=? AND t.last_seen>=? ORDER BY t.started,t.origin_event""",
            (config['site_id'], config['activated_at'], oldest - buffer),
        )
    ]
    if not rows:
        return
    cameras = defaultdict(list)
    signatures = [json.loads(r['signature']) if r['signature'] else None for r in rows]
    for i, r in enumerate(rows):
        cameras[r['camera']].append(i)
    starts = {c: [rows[i]['started'] for i in indexes] for c, indexes in cameras.items()}
    links = defaultdict(list)
    for edge in edges:
        links[edge['from_camera']].append(edge)
    health = defaultdict(list)
    health_times = defaultdict(list)
    for sample in db.execute(
        """SELECT h.* FROM retail_visit_health h JOIN json_each(?) j ON j.value=h.camera
            WHERE h.at>=? AND h.at<=? ORDER BY h.at""",
        (config['camera_ids'], oldest - buffer - HEALTH_GAP_SECONDS, now),
    ):
        health[sample['camera']].append(sample['healthy'])
        health_times[sample['camera']].append(sample['at'])
    # Plausible unknowns block assignments on either side. A single track and a single
    # compatible predecessor must uniquely select each other before we carry any ID.
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for p, source in enumerate(rows):
        departed = source['last_seen'] if source['ended'] is None else source['ended']
        for edge in links[source['camera']]:
            dest = edge['to_camera']
            times = starts.get(dest, [])
            first = bisect_left(times, departed + edge['min_seconds'])
            stop = bisect_right(times, departed + edge['max_seconds'])
            for q in cameras[dest][first:stop]:
                target = rows[q]
                if target['entrance_visit'] or target['started'] <= source['started']:
                    continue
                appearance = compare_clothing(signatures[p], signatures[q])
                if appearance == 'incompatible':
                    continue
                ok = (
                    appearance == 'compatible'
                    and source['ended'] is not None
                    and not source['invalid']
                    and not target['invalid']
                    and all(
                        _healthy(health[c], health_times[c], departed, target['started'])
                        for c in (source['camera'], dest)
                    )
                )
                incoming[q].append((p, ok))
                outgoing[p].append(q)
    assignments = {}
    for i, r in enumerate(rows):
        visit = r['entrance_visit']
        if not visit and r['started'] < oldest:
            visit = r['visit_id']
        if not visit and len(incoming[i]) == 1:
            p, ok = incoming[i][0]
            if ok and len(outgoing[p]) == 1:
                visit = assignments[p]
        assignments[i] = visit
        ambiguous = int(
            bool(
                visit
                and (
                    r['invalid']
                    or any(
                        len(incoming[j]) != 1 or len(outgoing[i]) != 1 or not incoming[j][0][1]
                        for j in outgoing[i]
                    )
                )
            )
        )
        if (visit, ambiguous) != (r['visit_id'], r['ambiguous']):
            db.execute(
                'UPDATE retail_visit_tracks SET visit_id=?,ambiguous=? WHERE origin_event=?',
                (visit, ambiguous, r['origin_event']),
            )


def advance(store, watermark):
    if not finite(watermark):
        return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for cfg in db.execute("SELECT * FROM retail_visit_configs WHERE state='active'").fetchall():
            _assign(db, cfg, watermark)


def cleanup(store, now):
    with store.connect() as db:
        db.execute('DELETE FROM retail_visit_health WHERE at<?', (now - IDLE_SECONDS - 620,))
        db.execute(
            'DELETE FROM retail_visit_tracks WHERE visit_id IS NULL AND last_seen<?',
            (now - IDLE_SECONDS - 600,),
        )
        db.execute(
            """UPDATE retail_visit_tracks SET signature=NULL WHERE signature IS NOT NULL AND last_seen<?
            AND NOT EXISTS(SELECT 1 FROM retail_visit_tracks active WHERE active.visit_id=retail_visit_tracks.visit_id
            AND active.last_seen>=?)""",
            (now - IDLE_SECONDS, now - IDLE_SECONDS),
        )
        db.execute(
            """DELETE FROM retail_visits WHERE NOT EXISTS(SELECT 1 FROM retail_visit_tracks t
                WHERE t.visit_id=retail_visits.id AND t.last_seen>=?)""",
            (now - HISTORY_SECONDS,),
        )
        db.execute(
            'DELETE FROM retail_visit_tracks WHERE last_seen<? AND visit_id NOT IN (SELECT id FROM retail_visits)',
            (now - HISTORY_SECONDS,),
        )


def _summaries(db, site_id, now, limit, visit_id=None, active_only=False):
    conditions = ['v.site_id=?', 't.visit_id=v.id']
    args = [site_id]
    if visit_id:
        conditions.append('v.id=?')
        args.append(visit_id)
    if active_only:
        conditions.append(
            "v.generation=c.activated_at AND c.state='active' AND EXISTS(SELECT 1 FROM retail_visit_tracks a WHERE a.visit_id=v.id AND a.last_seen>?)"
        )
        args.append(now - IDLE_SECONDS)
    sql = f"""WITH sightings AS (SELECT v.*,t.camera,t.last_seen,t.ended,t.invalid,t.ambiguous,c.state,
        c.activated_at current_generation, COUNT(*) OVER(PARTITION BY v.id) observations,
        ROW_NUMBER() OVER(PARTITION BY v.id ORDER BY t.started DESC,t.last_seen DESC,t.origin_event) AS n
        FROM retail_visits v JOIN retail_visit_tracks t ON t.visit_id=v.id LEFT JOIN retail_visit_configs c ON c.site_id=v.site_id
        WHERE {' AND '.join(conditions)}) SELECT s.*, (SELECT healthy FROM retail_visit_health h
         WHERE h.camera=s.camera AND h.at BETWEEN ? AND ? ORDER BY h.at DESC LIMIT 1) healthy
        FROM sightings s WHERE s.n=1 ORDER BY s.last_seen DESC,s.id LIMIT ?"""
    args.extend([now - HEALTH_GAP_SECONDS, now, limit])
    output = []
    for r in db.execute(sql, args):
        fresh = 0 <= now - r['last_seen'] <= POSITION_FRESH_SECONDS
        if (
            r['state'] != 'active'
            or r['generation'] != r['current_generation']
            or now - r['last_seen'] >= IDLE_SECONDS
        ):
            status = 'expired'
        elif r['invalid'] or r['ambiguous'] or not r['healthy'] or (r['ended'] is None and not fresh):
            status = 'uncertain'
        elif r['ended'] is None:
            status = 'observed'
        else:
            status = 'transit'
        output.append(
            {
                'id': r['id'],
                'site_id': site_id,
                'started_at': r['started_at'],
                'last_seen_at': r['last_seen'],
                'last_camera': r['camera'],
                'status': status,
                'active_camera': r['camera'] if status == 'observed' else None,
                'observations': r['observations'],
            }
        )
    return output


def list_visits(store, site_id, now, limit=100, active_only=False):
    with store.connect() as db:
        return _summaries(db, site_id, now, limit, active_only=active_only)


def get_visit(store, site_id, visit_id, now):
    with store.connect() as db:
        db.execute('BEGIN')
        found = _summaries(db, site_id, now, 1, visit_id)
        if not found:
            return None
        visit = found[0]
        rows = db.execute(
            """SELECT camera,zone,origin_event,started,last_seen,ended,has_clip,path,entry_phase
            FROM retail_visit_tracks WHERE visit_id=? AND site_id=? ORDER BY started,origin_event""",
            (visit_id, site_id),
        )
        visit['timeline'] = [
            {
                'camera': r['camera'],
                'zone': r['zone'],
                'origin_event': r['origin_event'],
                'role': 'entrance' if r['entry_phase'] == 2 else 'observation',
                'started_at': r['started'],
                'last_seen_at': r['last_seen'],
                'ended_at': r['ended'],
                'has_clip': bool(r['has_clip']),
                'path': json.loads(r['path']),
            }
            for r in rows
        ]
        return visit
