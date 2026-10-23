#!/usr/bin/env python3
"""Provision models explicitly; inference itself never downloads weights."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

COMMIT = 'c41067f07d252efcda18008bea367886070c33b0'
FACE_URL = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
FACE_SHA256 = '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def register(path, origin):
    record = {'sha256': digest(path), 'source': origin, 'size': path.stat().st_size}
    path.with_name(path.name + '.json').write_text(json.dumps(record, indent=2))
    print(f'Registered {path.name}: {record["sha256"]}')


def download_face(root):
    path = root / 'face_landmarker.task'
    temporary = path.with_suffix('.part')
    try:
        with urllib.request.urlopen(FACE_URL, timeout=45) as source, temporary.open('wb') as target:
            shutil.copyfileobj(source, target)
        if digest(temporary) != FACE_SHA256:
            raise RuntimeError('Official Face Landmarker v1 checksum mismatch')
        temporary.replace(path)
        register(path, FACE_URL)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_vadclip(root, checkpoint):
    target = root / 'vadclip'
    if not target.exists():
        subprocess.run(['git', 'clone', 'https://github.com/nwpu-zxr/VadCLIP.git', str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', COMMIT], check=True)
    patches = {
        'src/utils/layers.py': [(".to('cuda')", '.to(self.sigma.device)')],
        'src/clip/clip.py': [('from pkg_resources import packaging', 'import packaging')],
    }
    for relative, edits in patches.items():
        path = target / relative
        content = subprocess.check_output(['git', '-C', str(target), 'show', COMMIT + ':' + relative], text=True)
        for old, new in edits:
            if old not in content:
                raise RuntimeError(f'Upstream patch mismatch: {relative}')
            content = content.replace(old, new)
        path.write_text(content)
    files = {str(p.relative_to(target)): digest(p) for p in (target / 'src').rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    (target / 'glazza-source.json').write_text(json.dumps({'upstream_commit': COMMIT, 'patches': patches, 'files': files}, indent=2))
    source = Path(checkpoint).resolve()
    destination = root / 'vadclip_ucf.pth'
    if not source.is_file():
        raise RuntimeError('Download the official UCF-Crime checkpoint first; see docs/MODELS.md')
    if source != destination.resolve():
        shutil.copyfile(source, destination)
    register(destination, f'https://github.com/nwpu-zxr/VadCLIP/tree/{COMMIT} (official UCF-Crime checkpoint supplied by operator)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['face', 'vadclip'])
    parser.add_argument('--directory', default='models')
    parser.add_argument('--checkpoint', help='Official UCF-Crime .pth file, obtained from upstream README')
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.action == 'face':
        download_face(root)
    else:
        if not args.checkpoint:
            parser.error('--checkpoint is required for vadclip')
        prepare_vadclip(root, args.checkpoint)


if __name__ == '__main__':
    main()
