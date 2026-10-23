import os
import tempfile
from pathlib import Path

import yaml


def validate_names(cameras):
    ids = {camera['id'] for camera in cameras}
    if any(zone['name'] in ids for camera in cameras for zone in camera['zones']):
        raise ValueError('Имя зоны не должно совпадать с идентификатором любой камеры')


def make_config(cameras):
    validate_names(cameras)
    result = {
        'mqtt': {'enabled': False},
        'detectors': {'cpu': {'type': 'cpu', 'num_threads': 3}},
        'record': {'enabled': True, 'motion': {'days': 1},
                   'alerts': {'retain': {'days': 7, 'mode': 'motion'}},
                   'detections': {'retain': {'days': 3, 'mode': 'motion'}}},
        'snapshots': {'enabled': True, 'retain': {'default': 7}},
        'objects': {'track': ['person']},
        'cameras': {},
    }
    for cam in cameras:
        result['cameras'][cam['id']] = {
            'enabled': True,
            'ffmpeg': {'inputs': [{'path': cam['stream'], 'input_args': 'preset-rtsp-generic', 'roles': ['detect', 'record']}]},
            'detect': {'enabled': True, 'fps': 5},
            'zones': {z['name']: {'coordinates': ','.join(str(c) for p in z['points'] for c in p), 'objects': ['person'], 'inertia': 3} for z in cam['zones']},
        }
    return result


def atomic_config(path: Path, cameras):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = yaml.safe_dump(make_config(cameras), allow_unicode=True, sort_keys=False)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
