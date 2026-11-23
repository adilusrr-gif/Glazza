import json
import subprocess
import zipfile

import pytest

from analyzer.models import verify_artifact
from scripts import models


def archive(path, value):
    # Torch-format fixture tests transport only. It deliberately contains no model.
    with zipfile.ZipFile(path, 'w') as output:
        output.writestr('archive/data.pkl', 'fixture')
        output.writestr('archive/data/0', value)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def test_install_offline_reproducibly_without_replacing_good_model_on_bad_input(tmp_path, monkeypatch):
    repo = tmp_path / 'upstream'
    (repo / 'src/utils').mkdir(parents=True)
    (repo / 'src/clip').mkdir()
    (repo / 'src/utils/layers.py').write_text("x.to('cuda')")
    (repo / 'src/clip/clip.py').write_text('from pkg_resources import packaging')
    git(repo, 'init', '-q')
    git(repo, 'add', 'src')
    git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'fixture')
    revision = git(repo, 'rev-parse', 'HEAD')
    monkeypatch.setattr(models, 'COMMIT', revision)
    root = tmp_path / 'installed'
    root.mkdir()
    source = tmp_path / 'input.pth'
    archive(source, 'first')
    pin = models.digest(source)
    models.prepare_vadclip(root, source, pin, repo)
    original = verify_artifact(root, 'vadclip_ucf.pth')
    assert models.digest(original) == pin
    assert json.loads((root / 'vadclip/glazza-source.json').read_text())['upstream_commit'] == revision
    assert (root / 'vadclip/src/clip/clip.py').read_text() == 'import packaging.version'
    archive(source, 'second')
    with pytest.raises(ValueError, match='expected SHA'):
        models.prepare_vadclip(root, source, pin)
    assert verify_artifact(root, 'vadclip_ucf.pth') == original
    models.prepare_vadclip(root, source)
    assert verify_artifact(root, 'vadclip_ucf.pth') != original
    assert models.digest(original) == pin


def test_signin_page_and_features_archive_are_rejected_before_clone(tmp_path):
    bad = tmp_path / 'bad.pth'
    bad.write_text('<!doctype html><title>Sign in</title>')
    with pytest.raises(ValueError, match='PyTorch'):
        models.prepare_vadclip(tmp_path, bad)
    with zipfile.ZipFile(bad, 'w') as out:
        out.writestr('features/normal.npy', 'x')
    with pytest.raises(ValueError, match='PyTorch'):
        models.prepare_vadclip(tmp_path, bad)
    assert not (tmp_path / 'vadclip').exists()


def test_legacy_checkpoint_layout_remains_readable(tmp_path):
    path = tmp_path / 'vadclip_ucf.pth'
    archive(path, 'legacy')
    models.register(path, 'fixture')
    assert verify_artifact(tmp_path, path.name) == path


@pytest.mark.parametrize('state,coverage,error', [
    ('missing', None, 'VadCLIP state: missing'),
    ('ready', 'incomplete_coverage', 'no completed theft check'),
    ('ready', 'checked', None),
])
def test_probe_requires_real_coverage_and_cleans_up(tmp_path, monkeypatch, state, coverage, error):
    import io
    from scripts import probe_theft

    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    monkeypatch.setenv('GLAZZA_DATA', str(tmp_path))
    monkeypatch.setenv('GLAZZA_WORKER_KEY', 'fixture')
    monkeypatch.setenv('ANALYZER_URL', 'http://analyzer:8090')

    def request(req, timeout):
        if req.full_url.endswith('/health'):
            result = {'models': [{'id': 'vadclip', 'state': state}]}
        else:
            body = json.loads(req.data)
            assert body['modules'] == ['theft']
            assert (jobs / (body['job_id'] + '.mp4')).read_bytes() == b'video-fixture'
            result = {'model': 'VadCLIP', 'checked': ['theft'], 'duration_seconds': 3, 'detections': [],
                      'theft': {'status': coverage, 'samples_analyzed': 6, 'threshold': .75}}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(probe_theft, 'urlopen', request)
    video = io.BytesIO(b'video-fixture')
    if error:
        with pytest.raises(ValueError, match=error):
            probe_theft.probe(video)
    else:
        assert probe_theft.probe(video)['samples_analyzed'] == 6
    assert not list(jobs.iterdir())
