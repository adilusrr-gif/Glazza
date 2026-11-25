import shutil
import subprocess
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

import analyzer.main as worker
from analyzer.models import LABELS, score_events


def predictions(labels):
    scores = np.zeros((len(labels), len(LABELS)))
    for index, label in enumerate(labels):
        scores[index, LABELS.index(label)] = .9
    return scores


@pytest.mark.parametrize('labels', [
    ['Normal', 'Normal', 'Normal'],
    ['Burglary', 'Burglary', 'Robbery', 'Robbery'],
    ['Normal', 'Shoplifting', 'Stealing', 'Normal'],
    ['Shoplifting', 'Normal', 'Shoplifting'],
])
def test_no_sustained_retail_class(labels):
    assert score_events(predictions(labels)) == []


def test_normal_wins_at_relaxed_threshold():
    scores = predictions(['Normal'] * 3) * .6
    scores[:, LABELS.index('Shoplifting')] = .45
    assert score_events(scores, theft_threshold=.40) == []


def test_retail_detections_keep_distinct_intervals_and_their_best_supported_score():
    scores = predictions(['Normal', 'Shoplifting', 'Shoplifting', 'Shoplifting', 'Normal', 'Stealing', 'Stealing'])
    scores[1:4, LABELS.index('Shoplifting')] = [.75, .95, .88]
    times = [0, .625, 1.25, 1.875, 2.5, 3.125, 3.75]
    result = score_events(scores, sample_times=times, duration=4.)
    assert [(d['label'], d['start_seconds'], d['end_seconds'], d['sample_count'], d['score']) for d in result] == [
        ('Shoplifting', .625, 2.5, 3, .88), ('Stealing', 3.125, 4., 2, .9)]
    result = score_events(scores, theft_samples=3, sample_times=times, duration=4.)
    assert len(result) == 1 and result[0]['score'] == .75


def test_contiguous_confirmed_retail_labels_form_one_review_episode():
    scores = predictions(['Normal', 'Shoplifting', 'Shoplifting', 'Stealing', 'Stealing'])
    scores[3:, LABELS.index('Stealing')] = .96
    [event] = score_events(scores, sample_times=[0, .6, 1.2, 1.8, 2.4], duration=3.)
    assert (event['label'], event['start_seconds'], event['end_seconds'], event['sample_count']) == ('Stealing', .6, 3., 4)
    split = score_events(scores, sample_times=[0, .6, 1.2, 6., 6.6], duration=7.)
    assert len(split) == 2


def test_gap_cannot_corroborate_two_pictures_or_extend_an_interval():
    scores = predictions(['Shoplifting'] * 3)
    times = [0, .533333, 9.]
    [event] = score_events(scores, sample_times=times, duration=9.5)
    assert 1 < event['end_seconds'] < 1.1
    assert score_events(scores[1:], sample_times=times[1:], duration=9.5) == []


@pytest.mark.parametrize('corrupt', [float('nan'), float('inf'), -.01, 1.1])
def test_invalid_output_cannot_silently_count_as_a_negative(corrupt):
    scores = predictions(['Normal', 'Normal'])
    scores[0, 0] = corrupt
    with pytest.raises(ValueError, match='class scores'):
        score_events(scores)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='Requires FFmpeg')
@pytest.mark.parametrize(('fps', 'duration', 'count', 'status'), [
    (1, 4, 4, 'checked'),
    (30, 4, 8, 'checked'),
    (60, 4, 8, 'checked'),
    (1, 1, 1, 'insufficient_samples'),
])
def test_worker_uses_original_video_timestamps(tmp_path, monkeypatch, fps, duration, count, status):
    class StaticScores:
        theft_threshold = .75
        theft_samples = 2

        def __init__(self, root):
            pass

        def analyze(self, frames, times, duration):
            assert len(frames) == len(times) == count
            labels = ['Normal'] + ['Shoplifting'] * (count - 1)
            return score_events(predictions(labels), sample_times=times, duration=duration)

    def missing(root):
        raise FileNotFoundError()

    monkeypatch.setattr(worker, 'VadClipModel', StaticScores)
    for model in ['FightModel', 'FaceModel', 'IdentityModel']:
        monkeypatch.setattr(worker, model, missing)
    key = 'test-worker-key-' * 3
    monkeypatch.setenv('GLAZZA_WORKER_KEY', key)
    monkeypatch.setenv('GLAZZA_DATA', str(tmp_path))
    path = tmp_path / 'jobs' / f'{uuid.uuid4()}.mp4'
    path.parent.mkdir()
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=black:s=160x90:r={fps}',
                    '-t', str(duration), '-threads', '1', '-c:v', 'mpeg4', str(path)], check=True)
    with TestClient(worker.create_app()) as client:
        response = client.post('/analyze', json={'job_id': path.stem, 'modules': ['theft']},
                               headers={'X-Worker-Key': key})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['theft'] == {'status': status, 'threshold': .75, 'required_samples': 2, 'samples_analyzed': count}
    if status == 'checked':
        [event] = result['detections']
        assert result['checked'] == ['theft']
        assert event['start_seconds'] == pytest.approx(1. if fps == 1 else 16/30)
        assert event['end_seconds'] == duration
    else:
        assert result['checked'] == result['detections'] == []


def test_worker_reports_missing_theft_model_on_partial_analysis(tmp_path, monkeypatch):
    class Face:
        def __init__(self, root):
            self.model = self

        def analyze(self, frames):
            return {}

        def close(self):
            pass

    def missing(root):
        raise FileNotFoundError()

    monkeypatch.setattr(worker, 'FaceModel', Face)
    for model in ['FightModel', 'VadClipModel', 'IdentityModel']:
        monkeypatch.setattr(worker, model, missing)
    monkeypatch.setattr(worker, 'read_video', lambda path: ([None], 10.))
    key = 'test-worker-key-' * 3
    monkeypatch.setenv('GLAZZA_WORKER_KEY', key)
    monkeypatch.setenv('GLAZZA_DATA', str(tmp_path))
    path = tmp_path / 'jobs' / f'{uuid.uuid4()}.mp4'
    path.parent.mkdir()
    path.touch()
    with TestClient(worker.create_app()) as client:
        response = client.post('/analyze', json={'job_id': path.stem, 'modules': ['theft', 'face']},
                               headers={'X-Worker-Key': key})
        body = response.json()
        assert response.status_code == 200
        assert body['checked'] == ['face']
        assert body['theft'] == {'status': 'model_missing'}
        response = client.post('/analyze', json={'job_id': path.stem, 'modules': ['theft']},
                               headers={'X-Worker-Key': key})
        assert response.status_code == 503
