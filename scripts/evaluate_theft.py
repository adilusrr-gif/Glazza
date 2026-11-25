"""Replay labelled footage through Glazza's theft analyzer, without a running camera or server."""
import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid
from unittest.mock import patch


def number(value, name, minimum=0, maximum=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'Invalid {name}: {value!r}')
    return value


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing {name}')
    return value


def intervals(values, duration):
    if not isinstance(values, list):
        raise ValueError('Expected a list of event intervals')
    result = []
    for value in values:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError('Use [start_seconds, end_seconds] for each interval')
        start, end = (number(t, 'event time', maximum=duration) for t in value)
        if end <= start or (result and start < result[-1][1]):
            raise ValueError('Event intervals must be ordered, positive and disjoint')
        result.append([start, end])
    return result


def load_manifest(path):
    data = json.loads(path.read_text())
    records = data['recordings']
    if not isinstance(records, list) or not records:
        raise ValueError('No recordings in manifest')
    sources, digests, rows = set(), set(), []
    for recording in records:
        camera, source = (text(recording[k], k) for k in ('camera', 'recording'))
        split, sample = recording['split'], recording['sample']
        if split not in ('test', 'calibration') or sample not in ('continuous', 'curated'):
            raise ValueError('Unknown split or sample type')
        if (camera, source) in sources:
            raise ValueError(f'Source appears more than once: {camera}/{source}')
        sources.add((camera, source))
        length = number(recording['duration_seconds'], 'recording duration')
        previous_end = 0
        if not isinstance(recording['clips'], list) or not recording['clips']:
            raise ValueError(f'Empty recording: {source}')
        for clip in recording['clips']:
            file = (path.parent / text(clip['file'], 'file')).resolve()
            with file.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            if checksum in digests:
                raise ValueError(f'Duplicate footage: {file.name}')
            digests.add(checksum)
            offset = number(clip['start_seconds'], 'clip offset', maximum=length)
            duration = number(clip['duration_seconds'], 'clip duration', 0.1, 120)
            if offset < previous_end - 1e-6 or offset + duration > length + 1e-6:
                raise ValueError(f'Overlapping clips or clip outside recording: {file.name}')
            if sample == 'continuous' and abs(previous_end - offset) > 1e-6:
                raise ValueError(f'Gap in continuous footage: {file.name}')
            previous_end = offset + duration
            rows.append(dict(camera=camera, recording=source, split=split, sample=sample, file=file,
                             sha256=checksum, start_seconds=offset, duration_seconds=duration,
                             expected=intervals(clip['events'], duration)))
        if sample == 'continuous' and abs(previous_end - length) > 1e-6:
            raise ValueError(f'Incomplete continuous recording: {source}')
    return rows


def count_matches(expected, predicted, min_iou):
    # Both sets have sorted, disjoint intervals; once an interval ends it cannot
    # intersect any later interval in the opposite list.
    i = j = matched = 0
    while i < len(expected) and j < len(predicted):
        a, b = expected[i]
        x, y = predicted[j]
        intersection = max(0, min(b, y) - max(a, x))
        if intersection / (max(b, y) - min(a, x)) >= min_iou:
            matched += 1
            i += 1
            j += 1
        elif b <= y:
            i += 1
        else:
            j += 1
    return matched


def replay(rows, analyze, min_iou):
    result = []
    for row in rows:
        item = {k: v for k, v in row.items() if k != 'file'}
        item['file'] = row['file'].name
        started = time.monotonic()
        try:
            reply = analyze(row['file'])
            if 'theft' not in reply.get('checked', []):
                status = reply.get('theft', {}).get('status', 'unchecked')
                raise ValueError(f'Analyzer did not check theft: {status}')
            duration = number(reply['duration_seconds'], 'decoded duration', 0.1, 120)
            if abs(duration - row['duration_seconds']) > 0.10:
                raise ValueError('Decoded duration differs from manifest by more than 0.10 s')
            detections = [d for d in reply['detections'] if d['kind'] == 'theft']
            predicted = intervals([[d['start_seconds'], d['end_seconds']] for d in detections], duration)
            matched = count_matches(row['expected'], predicted, min_iou)
            item.update(predicted=predicted, decoded_seconds=duration, tp=matched, fp=len(predicted)-matched,
                        fn=len(row['expected'])-matched)
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
            item['error'] = str(error)
        item['processing_seconds'] = round(time.monotonic() - started, 4)
        result.append(item)
    return result


def summarize(rows):
    checked = [r for r in rows if 'error' not in r]
    tp, fp, fn = (sum(r[k] for r in checked) for k in ('tp', 'fp', 'fn'))
    hours = sum(r['decoded_seconds'] for r in checked) / 3600
    return dict(clips=len(rows), checked_clips=len(checked), failed_clips=len(rows)-len(checked),
                annotated_events=sum(len(r['expected']) for r in rows), checked_camera_hours=hours,
                normal_camera_hours=sum(r['decoded_seconds'] for r in checked if not r['expected']) / 3600,
                tp=tp, fp=fp, fn=fn, precision=tp/(tp+fp) if tp+fp else None,
                recall=tp/(tp+fn) if tp+fn else None, false_alerts_per_camera_hour=fp/hours if hours else None)


def load_targets(path):
    raw = json.loads(path.read_text())
    expected = {'min_normal_camera_hours', 'min_events', 'min_precision', 'min_recall', 'max_false_alerts_per_camera_hour'}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError(f'Expected targets: {sorted(expected)}')
    for key in expected:
        number(raw[key], key, maximum=1 if key in ('min_precision', 'min_recall') else math.inf)
    if not isinstance(raw['min_events'], int) or raw['min_events'] < 1 or raw['min_normal_camera_hours'] <= 0:
        raise ValueError('Specify positive minimum held-out hours and event count')
    return raw


def report(rows, split, targets):
    field = summarize([r for r in rows if r['sample'] == 'continuous'])
    curated = summarize([r for r in rows if r['sample'] == 'curated'])
    # Curated controls do not estimate real-world prevalence or alerts per shift.
    curated.pop('false_alerts_per_camera_hour')
    verdict = 'measured'
    if field['failed_clips'] or curated['failed_clips']:
        verdict = 'incomplete'
    elif targets:
        if (split != 'test' or field['normal_camera_hours'] < targets['min_normal_camera_hours']
                or field['annotated_events'] < targets['min_events']):
            verdict = 'insufficient_evidence'
        elif (field['precision'] is None or field['precision'] < targets['min_precision']
              or field['recall'] < targets['min_recall']
              or field['false_alerts_per_camera_hour'] > targets['max_false_alerts_per_camera_hour']):
            verdict = 'targets_missed'
        else:
            verdict = 'offline_targets_met'
    return dict(verdict=verdict, split=split, targets=targets, continuous=field, curated=curated, clips=rows)


@contextmanager
def local_analyzer(models):
    from fastapi.testclient import TestClient
    from analyzer.main import create_app

    with tempfile.TemporaryDirectory(prefix='glazza-theft-eval-') as temporary:
        jobs = Path(temporary) / 'jobs'
        jobs.mkdir()
        key = secrets.token_hex(32)
        env = dict(GLAZZA_WORKER_KEY=key, GLAZZA_MODELS=str(models.resolve()), GLAZZA_DATA=temporary)
        with patch.dict(os.environ, env), TestClient(create_app()) as client:
            def analyze(file):
                job = uuid.uuid4()
                # MOV is accepted as an MP4 container by the upload route.
                suffix = '.mp4' if file.suffix.lower() == '.mov' else file.suffix.lower()
                link = jobs / (str(job) + suffix)
                link.symlink_to(file)
                try:
                    reply = client.post('/analyze', json={'job_id': str(job), 'modules': ['theft']},
                                        headers={'X-Worker-Key': key})
                    if reply.status_code != 200:
                        raise RuntimeError(f'Analyzer HTTP {reply.status_code}: {reply.text[:300]}')
                    return reply.json()
                finally:
                    link.unlink()
            yield analyze


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--models', type=Path, default=Path(os.getenv('GLAZZA_MODELS', 'models')))
    parser.add_argument('--split', choices=['test', 'calibration'], default='test')
    parser.add_argument('--min-iou', type=float, default=0.1)
    parser.add_argument('--targets', type=Path)
    args = parser.parse_args()
    try:
        if not 0 < args.min_iou <= 1:
            raise ValueError('--min-iou must be in (0, 1]')
        targets = load_targets(args.targets) if args.targets else None
        rows = [row for row in load_manifest(args.manifest) if row['split'] == args.split]
        if not rows:
            raise ValueError(f'No clips in split: {args.split}')
        with redirect_stdout(sys.stderr), local_analyzer(args.models) as analyze:
            output = report(replay(rows, analyze, args.min_iou), args.split, targets)
        model_manifest = args.models / 'vadclip_ucf.pth.json'
        output.update(min_iou=args.min_iou,
                      manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                      checkpoint_sha256=json.loads(model_manifest.read_text())['sha256'] if model_manifest.is_file() else None,
                      threshold=float(os.getenv('GLAZZA_THEFT_THRESHOLD') or os.getenv('GLAZZA_ACTION_THRESHOLD', '0.75')),
                      consecutive_samples=int(os.getenv('GLAZZA_THEFT_SAMPLES', '2')))
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if output['verdict'] in ('measured', 'offline_targets_met') else 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f'Cannot evaluate: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
