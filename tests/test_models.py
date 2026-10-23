import json
import numpy as np
import pytest
from analyzer.models import LABELS, score_events, verify_artifact, sha256


def test_single_frame_spike_does_not_raise_alert():
    scores=np.zeros((4,14));scores[1,LABELS.index('Fighting')]=.99
    assert score_events(scores)==[]


def test_sustained_fight_and_theft_are_distinct():
    scores=np.zeros((6,14));scores[1:3,LABELS.index('Fighting')]=.85;scores[4:6,LABELS.index('Shoplifting')]=.91
    results=score_events(scores)
    assert {r['kind'] for r in results}=={'fight','theft'}
    assert all(r['review_required'] for r in results)


def test_normal_and_low_scores_never_generate_crime_alert():
    scores=np.full((4,14),.04);scores[:,0]=.9
    assert score_events(scores)==[]


def test_model_checksum_detects_changed_file(tmp_path):
    path=tmp_path/'model.pth';path.write_bytes(b'valid')
    (tmp_path/'model.pth.json').write_text(json.dumps({'sha256':sha256(path)}))
    assert verify_artifact(tmp_path,'model.pth')==path
    path.write_bytes(b'changed')
    with pytest.raises(ValueError):verify_artifact(tmp_path,'model.pth')


def test_missing_model_is_not_ready(tmp_path):
    with pytest.raises(FileNotFoundError):verify_artifact(tmp_path,'missing.pth')
