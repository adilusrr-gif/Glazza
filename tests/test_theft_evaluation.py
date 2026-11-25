import json
import shutil
import subprocess

import pytest

from scripts.evaluate_theft import count_matches, load_manifest, load_targets, local_analyzer, replay, report


def recording(tmp_path, sample='continuous', camera='aisle', split='test'):
    clips = []
    for i, events in enumerate([[[2, 4]], []]):
        filename = f'{camera}{i}.mp4'
        (tmp_path / filename).write_bytes(filename.encode())
        clips.append(dict(file=filename, start_seconds=i*10, duration_seconds=10, events=events))
    return dict(camera=camera, recording='shift1', sample=sample, split=split, duration_seconds=20, clips=clips)


def manifest(tmp_path, records):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'recordings': records}))
    return load_manifest(path)


def targets(tmp_path):
    path = tmp_path / 'targets.json'
    path.write_text(json.dumps(dict(min_normal_camera_hours=.002, min_events=1, min_precision=.5,
                                    min_recall=.8, max_false_alerts_per_camera_hour=200)))
    return load_targets(path)


def predictions(file):
    intervals = [(2, 5), (6, 7)] if file.name.endswith('0.mp4') else []
    return dict(checked=['theft'], duration_seconds=10, detections=[
        dict(kind='theft', start_seconds=start, end_seconds=end) for start, end in intervals])


def test_false_alerts_in_positive_footage_and_camera_hours(tmp_path):
    rows = manifest(tmp_path, [recording(tmp_path), recording(tmp_path, 'curated', 'door')])
    result = report(replay(rows, predictions, .1), 'test', targets(tmp_path))
    field = result['continuous']
    assert result['verdict'] == 'offline_targets_met'
    assert (field['tp'], field['fp'], field['fn']) == (1, 1, 0)
    assert field['false_alerts_per_camera_hour'] == 180
    assert field['precision'] == .5
    assert field['normal_camera_hours'] == 10/3600
    assert 'false_alerts_per_camera_hour' not in result['curated']


def test_overlaps_match_once_and_require_minimum_temporal_agreement():
    assert count_matches([[1, 3], [4, 6]], [[2, 2.5], [2.6, 3], [4, 5]], .1) == 2
    assert count_matches([[2, 3]], [[0, 30]], .1) == 0
    assert count_matches([[1, 3], [5, 7]], [[1.5, 6]], .1) == 1
    assert count_matches([[1, 2]], [[2, 3]], .1) == 0


@pytest.mark.parametrize('bad_reply', [
    {'checked': [], 'detections': [], 'duration_seconds': 10},
    {'checked': ['theft'], 'detections': [], 'duration_seconds': 2},
    {'checked': ['theft'], 'detections': [{'kind':'theft'}], 'duration_seconds': 10},
])
def test_unchecked_footage_cannot_improve_quality_or_pass(tmp_path, bad_reply):
    rows = manifest(tmp_path, [recording(tmp_path)])
    output = report(replay(rows, lambda _: bad_reply, .1), 'test', targets(tmp_path))
    assert output['verdict'] == 'incomplete'
    field = output['continuous']
    assert field['failed_clips'] == 2 and field['checked_camera_hours'] == 0
    assert field['precision'] is field['recall'] is field['false_alerts_per_camera_hour'] is None


@pytest.mark.parametrize('sample,split', [('curated', 'test'), ('continuous', 'calibration')])
def test_selected_or_calibration_footage_cannot_pass_field_targets(tmp_path, sample, split):
    rows = manifest(tmp_path, [recording(tmp_path, sample, split=split)])
    thresholds = targets(tmp_path)
    assert report(replay(rows, predictions, .1), split, thresholds)['verdict'] == 'insufficient_evidence'


def test_inadequate_holdout_or_missed_target_is_explicit(tmp_path):
    rows = replay(manifest(tmp_path, [recording(tmp_path)]), predictions, .1)
    thresholds = targets(tmp_path)
    for key, value, verdict in [('min_normal_camera_hours', 5, 'insufficient_evidence'),
                                ('min_events', 10, 'insufficient_evidence'),
                                ('min_precision', .9, 'targets_missed')]:
        assert report(rows, 'test', {**thresholds, key: value})['verdict'] == verdict


def test_manifest_rejects_duplicate_bytes_across_splits_and_source_leak(tmp_path):
    a, b = recording(tmp_path), recording(tmp_path, 'curated', 'other', 'calibration')
    b['clips'][0]['file'] = a['clips'][0]['file']
    with pytest.raises(ValueError, match='Duplicate'):
        manifest(tmp_path, [a, b])
    b['camera'] = a['camera']
    with pytest.raises(ValueError, match='Source'):
        manifest(tmp_path, [a, b])


@pytest.mark.parametrize('start,total,message', [(11, 21, 'Gap'), (9, 20, 'Overlapping'), (10, 21, 'Incomplete')])
def test_continuous_recording_must_cover_entire_shift_once(tmp_path, start, total, message):
    rec = recording(tmp_path)
    rec['clips'][1]['start_seconds'], rec['duration_seconds'] = start, total
    with pytest.raises(ValueError, match=message):
        manifest(tmp_path, [rec])


def test_replay_reaches_production_endpoint_and_cannot_pass_missing_model(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    video = tmp_path / 'grey.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=64x64:r=16',
                    '-t', '3', '-c:v', 'mpeg4', str(video)], check=True)
    row = dict(file=video, sample='continuous', duration_seconds=3, expected=[])
    with local_analyzer(tmp_path / 'empty-models') as analyze:
        output = report(replay([row], analyze, .1), 'test', None)
    assert output['verdict'] == 'incomplete'
    assert '503' in output['clips'][0]['error']


def test_replay_decodes_mp4_and_matches_timed_production_signal(tmp_path, monkeypatch):
    import numpy as np
    from analyzer import main as worker
    from analyzer.models import LABELS, score_events

    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')

    class TestModel:
        theft_samples = 2
        theft_threshold = .75

        def analyze(self, frames, times, duration):
            assert len(times) == len(frames) >= 3
            scores = np.zeros((len(frames), len(LABELS)))
            scores[:2, LABELS.index('Shoplifting')] = .9
            return score_events(scores, sample_times=times, duration=duration)

    monkeypatch.setattr(worker, 'VadClipModel', lambda root: TestModel())
    video = tmp_path / 'grey.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=64x64:r=30',
                    '-t', '3', '-c:v', 'mpeg4', str(video)], check=True)
    row = dict(file=video, sample='continuous', duration_seconds=3, expected=[[0, 1.1]])
    with local_analyzer(tmp_path) as analyze:
        output = report(replay([row], analyze, .1), 'test', None)
    assert output['verdict'] == 'measured', output['clips']
    assert output['continuous']['tp'] == 1
    assert output['clips'][0]['predicted'][0][0] == 0
