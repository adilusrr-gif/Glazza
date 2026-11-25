"""Probe the running Compose theft analyzer on one local MP4 without adding a case."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

LIMIT = 200 * 1024 * 1024


def probe(video):
    """Run inside the Glazza API container, which has write access to /data/jobs."""
    base = os.environ['ANALYZER_URL'].rstrip('/')
    headers = {'X-Worker-Key': os.environ['GLAZZA_WORKER_KEY'], 'Content-Type': 'application/json'}
    with urlopen(Request(base + '/health', headers=headers), timeout=10) as response:
        health = json.load(response)
    state = next((m['state'] for m in health['models'] if m['id'] == 'vadclip'), 'unavailable')
    if state != 'ready':
        raise ValueError(f'VadCLIP state: {state}. Check the installed checkpoint and analyzer logs.')

    job_id = str(uuid.uuid4())
    job = Path(os.environ['GLAZZA_DATA']) / 'jobs' / (job_id + '.mp4')
    total = 0
    with job.open('xb') as output:
        try:
            for chunk in iter(lambda: video.read(1024 * 1024), b''):
                total += len(chunk)
                if total > LIMIT:
                    raise ValueError('Control clip exceeds 200 MiB')
                output.write(chunk)
        except BaseException:
            job.unlink(missing_ok=True)
            raise
    try:
        payload = json.dumps({'job_id': job_id, 'modules': ['theft']}).encode()
        started = time.monotonic()
        with urlopen(Request(base + '/analyze', data=payload, headers=headers), timeout=600) as response:
            result = json.load(response)
        coverage = result.get('theft', {})
        if result.get('checked') != ['theft'] or coverage.get('status') != 'checked':
            raise ValueError('The analyzer returned no completed theft check; use a longer control clip.')
        if not isinstance(result.get('detections'), list) or not result.get('duration_seconds'):
            raise ValueError('Incomplete analyzer response')
        return {'model': result['model'], 'checked': result['checked'], 'duration_seconds': result['duration_seconds'],
                'processing_seconds': round(time.monotonic() - started, 2), 'alerts': len(result['detections']),
                'samples_analyzed': coverage['samples_analyzed'], 'threshold': coverage['threshold']}
    finally:
        job.unlink(missing_ok=True)


def main():
    # No published analyzer port/worker credentials or host Python dependencies.
    if sys.argv[1:] == ['--worker']:
        print(json.dumps(probe(sys.stdin.buffer), ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clip', type=Path, help='A local, short (up to 120 s) camera/control MP4')
    args = parser.parse_args()
    if not args.clip.is_file() or not 0 < args.clip.stat().st_size <= LIMIT:
        parser.error('Supply a nonempty local camera/control MP4 up to 200 MiB')
    with args.clip.open('rb') as video:
        command = ['docker', 'compose', 'exec', '-T', 'glazza', 'python', '-c',
                   Path(__file__).read_text(), '--worker']
        return subprocess.run(command, stdin=video).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except HTTPError as error:
        raise SystemExit(f'Analyzer returned HTTP {error.code}: {error.read(512).decode(errors="replace")}')
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'Theft probe failed: {error}')
