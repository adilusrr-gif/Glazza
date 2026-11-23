"""Reconcile emitted retail-route cards against independent shift annotations."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from scripts.evaluate_theft import intervals, number, text


def overlaps(a, b):
    return max(a[0], b[0]) < min(a[1], b[1])


def field_period(db, period):
    site, camera = (text(period[k], k) for k in ('site_id', 'camera'))
    start, end = intervals([[period['start'], period['end']]], float('inf'))[0]
    if not isinstance(period['reference_complete'], bool):
        raise ValueError('Set reference_complete to true or false')
    duration = end - start
    gaps = intervals(period['gaps'], duration)
    targets = {}
    for event in period['events']:
        key = text(event['id'], 'reference id')
        if key in targets:
            raise ValueError(f'Duplicate reference id: {key}')
        targets[key] = intervals([event['span']], duration)[0]
    unknown = {key for key, span in targets.items() if any(overlaps(g, span) for g in gaps)}
    source = (site, camera)
    if not db.execute('SELECT 1 FROM cameras WHERE site_id=? AND id=? UNION ALL SELECT 1 FROM events WHERE site_id=? AND camera=? LIMIT 1',
                      source + source).fetchone():
        raise ValueError(f'Unknown site/camera in snapshot: {site}/{camera}')
    alerts = db.execute("""SELECT id, started, ended, status FROM events WHERE site_id=? AND camera=?
        AND source='retail_route' AND kind='theft' AND ended>=? AND ended<? ORDER BY ended,id""", (*source, start, end))
    reviews = dict(period.get('reviews', {}))
    paired, cards, pending, false_ids = set(), [], [], []
    seconds, timed = 0, 0
    for alert_id, left, right, status in alerts:
        span = [left-start, right-start]
        observed = left >= start and not any(overlaps(g, span) for g in gaps)
        card = dict(id=alert_id, span=span, queue_status=status, observable=observed)
        cards.append(card)
        if not observed:
            continue
        if alert_id not in reviews:
            pending.append(alert_id)
            continue
        decision = reviews.pop(alert_id)
        key = decision['reference_id']
        if key is None:
            false_ids.append(alert_id)
        elif key in paired or key not in targets or key in unknown or not overlaps(span, targets[key]):
            raise ValueError(f'Invalid or duplicate reference match for {alert_id}')
        else:
            paired.add(key)
        card['reference_id'] = key
        if decision.get('operator_seconds') is not None:
            seconds += number(decision['operator_seconds'], 'operator_seconds')
            timed += 1
    if reviews:
        raise ValueError(f'Unknown or unobservable card ids for period: {sorted(reviews)}')
    gap_seconds = sum(b-a for a,b in gaps)
    hours = (duration - gap_seconds) / 3600
    complete = period['reference_complete'] and not pending and hours > 0
    missed = sorted(targets.keys() - unknown - paired) if complete else None
    return dict(site_id=site, camera=camera, start=start, end=end, cards=cards,
                planned_camera_hours=duration/3600, observable_camera_hours=hours, gap_seconds=gap_seconds, gaps=gaps,
                unobservable_reference_ids=sorted(unknown), pending_card_ids=pending, reconciled=complete,
                matched=len(paired), false_card_ids=false_ids, missed_reference_ids=missed,
                escalated_cards=sum(c['queue_status']=='confirmed' for c in cards),
                timed_cards=timed, measured_operator_seconds=seconds)


def field_report(db, periods):
    if not isinstance(periods, list) or not periods:
        raise ValueError('No reference periods')
    output = [field_period(db, p) for p in periods]
    last_end = {}
    for p in sorted(output, key=lambda r: (r['site_id'], r['camera'], r['start'])):
        key = (p['site_id'], p['camera'])
        if key in last_end and p['start'] < last_end[key]:
            raise ValueError('Reference periods overlap on the same site/camera')
        last_end[key] = p['end']
    complete = all(p['reconciled'] for p in output)
    tp = sum(p['matched'] for p in output)
    fp = sum(len(p['false_card_ids']) for p in output)
    hours = sum(p['observable_camera_hours'] for p in output)
    fn = sum(len(p['missed_reference_ids']) for p in output) if complete else None
    return dict(mode='retail_route', periods=output, reconciled=complete,
                planned_camera_hours=sum(p['planned_camera_hours'] for p in output),
                observable_camera_hours=hours, observed_tp=tp, reviewed_fp=fp, missed=fn,
                precision=tp/(tp+fp) if complete and tp+fp else None,
                recall=tp/(tp+fn) if complete and tp+fn else None,
                false_alerts_per_observable_camera_hour=fp/hours if complete else None,
                measured_operator_seconds=sum(p['measured_operator_seconds'] for p in output),
                timed_cards=sum(p['timed_cards'] for p in output),
                escalated_cards=sum(p['escalated_cards'] for p in output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('reference', type=Path)
    args = parser.parse_args()
    try:
        with closing(sqlite3.connect(args.snapshot.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            output = field_report(db, json.loads(args.reference.read_text())['periods'])
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if output['reconciled'] else 1
    except (TypeError, KeyError, ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
