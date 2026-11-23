#!/usr/bin/env python3
"""Provision models explicitly; inference itself never downloads weights."""
import argparse
import hashlib
import json
import os
import pickle
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

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


def check_vadclip_checkpoint(path):
    # Catch expired provider sign-in pages, feature archives and truncated downloads
    # before touching an installed model. Tensor shapes are checked by the analyzer.
    if not path.is_file():
        raise ValueError('Supply the authors’ UCF-Crime checkpoint; see docs/MODELS.md')
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        if any(name.endswith('/data.pkl') for name in names) and any('/data/' in name for name in names):
            return
    else:
        with path.open('rb') as stream:
            # The legacy torch.save header contains a pickled fixed magic number.
            # Encode the known integer locally; never unpickle input here.
            header = stream.read(48)
            if any(header.startswith(pickle.dumps(0x1950A86A20F9469CFC6C, protocol=p))
                   for p in range(pickle.HIGHEST_PROTOCOL + 1)):
                return
    raise ValueError('Expected a PyTorch checkpoint, not a web page, feature archive or partial download')


def prepare_vadclip(root, checkpoint, expected_sha256=None, source_repo=None):
    source = Path(checkpoint).resolve()
    check_vadclip_checkpoint(source)
    if expected_sha256 and digest(source) != expected_sha256.lower():
        raise ValueError('Supplied checkpoint differs from the expected SHA-256')
    # Use a fresh checkout when first provisioning. With --source-repo this also
    # works on an offline target. Legacy installed checkouts are reused.
    target = root / 'vadclip'
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.vadclip-src-', dir=root) as temporary:
            checkout = Path(temporary) / 'vadclip'
            repo = str(Path(source_repo).resolve()) if source_repo else 'https://github.com/nwpu-zxr/VadCLIP.git'
            subprocess.run(['git', 'clone', '--no-local', '--', repo, str(checkout)], check=True)
            subprocess.run(['git', '-C', str(checkout), 'checkout', '--detach', COMMIT], check=True)
            try:
                checkout.rename(target)
            except OSError:
                if not (target / '.git').is_dir():
                    raise
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', COMMIT], check=True)
    patches = {
        'src/utils/layers.py': [(".to('cuda')", '.to(self.sigma.device)')],
        'src/clip/clip.py': [('from pkg_resources import packaging', 'import packaging.version')],
    }
    # Derive the manifest only from tracked, pinned source. Previously a second
    # install could bless unrelated local files in src as if they were upstream.
    tracked = subprocess.check_output(
        ['git', '-C', str(target), 'ls-tree', '-r', '--name-only', COMMIT, 'src'], text=True
    ).splitlines()
    files = {}
    with tempfile.TemporaryDirectory(prefix='.vadclip-patch-', dir=root) as temporary:
        staged = Path(temporary) / 'file'
        for relative in tracked:
            original = subprocess.check_output(['git', '-C', str(target), 'show', COMMIT + ':' + relative])
            content = original
            for old, new in patches.get(relative, []):
                if old.encode() not in content:
                    raise RuntimeError(f'Upstream patch mismatch: {relative}')
                content = content.replace(old.encode(), new.encode())
            expected = hashlib.sha256(content).hexdigest()
            path = target / relative
            if not path.is_file() or digest(path) != expected:
                staged.write_bytes(content)
                staged.replace(path)
            files[relative] = expected
        extra = [path for path in (target / 'src').rglob('*') if path.is_file()
                 and '__pycache__' not in path.parts and path.relative_to(target).as_posix() not in files]
        if extra:
            raise ValueError('Unexpected files in installed VadCLIP src; move them out before reinstalling')
        staged.write_text(json.dumps({'upstream_commit': COMMIT, 'patches': patches, 'files': files}, indent=2))
        staged.replace(target / 'glazza-source.json')
    destination = root / 'vadclip_ucf.pth'
    with tempfile.TemporaryDirectory(prefix='.vadclip-weights-', dir=root) as temporary:
        staging = Path(temporary) / destination.name
        # Even when supplied from the model directory, validate the staged bytes
        # before publishing. Cross-device or interrupted copies cannot truncate a model.
        shutil.copyfile(source, staging)
        check_vadclip_checkpoint(staging)
        actual = digest(staging)
        if expected_sha256 and actual != expected_sha256.lower():
            raise ValueError('Copied checkpoint differs from the expected SHA-256')
        # Publish the manifest only after its immutable blob is fully present.
        # Readers that started during an update keep the previous file.
        weights = root / 'vadclip-weights'
        weights.mkdir(exist_ok=True)
        staging.replace(weights / (actual + '.pth'))
        manifest = staging.with_suffix('.pth.json')
        manifest.write_text(json.dumps({
            'sha256': actual,
            'size': (weights / (actual + '.pth')).stat().st_size,
            'layout': 'sha256',
            'source': f'https://github.com/nwpu-zxr/VadCLIP/tree/{COMMIT} (operator-supplied UCF-Crime checkpoint)',
        }, indent=2))
        manifest.replace(root / manifest.name)
    print(f'Registered {destination.name}: {actual}')


def download_fight(root):
    print('VD-MIL by Roger Figueroa Quintero: CC BY-NC 4.0. Commercial use requires author permission.')
    artifacts = json.loads((Path(__file__).resolve().parents[1] / 'analyzer/fight_artifacts.json').read_text())
    target = root / 'vdmil'
    # A partially downloaded model must never appear ready. Existing valid installs are reusable.
    if target.exists():
        if all((target / name).is_file() and digest(target / name) == item['sha256']
               for name, item in artifacts['files'].items()):
            print('VD-MIL already installed and verified.')
            return
        raise RuntimeError('models/vdmil is incomplete or modified. Move it aside and rerun the installer.')
    with tempfile.TemporaryDirectory(prefix='.vdmil-', dir=root) as temporary:
        staging = Path(temporary) / 'vdmil'
        staging.mkdir()
        for name, item in artifacts['files'].items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            print('Downloading ' + name, flush=True)
            with urllib.request.urlopen(item['url'], timeout=60) as source, path.open('wb') as out:
                shutil.copyfileobj(source, out)
                out.flush()
                os.fsync(out.fileno())
            if digest(path) != item['sha256']:
                raise RuntimeError('Pinned artifact checksum mismatch: ' + name)
        (staging / 'glazza-source.json').write_text(json.dumps(artifacts, indent=2))
        staging.rename(target)
    print('VD-MIL installed. Restart analyzer; check the fight model status in Glazza.')


def download_identity(root):
    artifacts=json.loads((Path(__file__).resolve().parents[1]/'analyzer/identity_artifacts.json').read_text())
    target=root/'identity'
    target.mkdir(exist_ok=True)
    for name,item in artifacts['files'].items():
        path=target/name
        if path.exists() and digest(path)==item['sha256']: continue
        temporary=path.with_suffix('.part')
        try:
            print('Downloading '+name,flush=True)
            with urllib.request.urlopen(item['url'],timeout=90) as source, temporary.open('wb') as out:
                shutil.copyfileobj(source,out)
                out.flush();os.fsync(out.fileno())
            if digest(temporary)!=item['sha256']: raise RuntimeError('Identity checksum mismatch')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print('YuNet + SFace installed. Restart analyzer.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['face', 'fight', 'identity', 'vadclip'])
    parser.add_argument('--directory', default='models')
    parser.add_argument('--checkpoint', help='Official UCF-Crime .pth file, obtained from upstream README')
    parser.add_argument('--sha256', help='Expected SHA-256 for this exact locally supplied checkpoint')
    parser.add_argument('--source-repo', help='Offline Git checkout of VadCLIP containing the pinned commit')
    args = parser.parse_args()
    if (args.sha256 or args.source_repo) and args.action != 'vadclip':
        parser.error('--sha256 and --source-repo are only available for vadclip')
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.action == 'face':
        download_face(root)
    elif args.action == 'identity':
        download_identity(root)
    elif args.action == 'fight':
        download_fight(root)
    else:
        if not args.checkpoint:
            parser.error('--checkpoint is required for vadclip')
        try:
            prepare_vadclip(root, args.checkpoint, args.sha256, args.source_repo)
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            parser.exit(1, f'Installation failed: {error}\n')


if __name__ == '__main__':
    main()
