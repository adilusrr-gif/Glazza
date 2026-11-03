"""Real decoder checks; run in the analyzer image (FFmpeg required)."""
import shutil
import subprocess

import pytest

from analyzer.main import read_video

pytestmark = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='Requires FFmpeg')


@pytest.mark.parametrize('container', ['mp4', 'mov', 'matroska', 'avi'])
def test_supported_container_decodes(tmp_path, container):
    path = tmp_path / ('control.' + container)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=8',
                    '-t', '5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-f', container, str(path)], check=True)
    frames, duration = read_video(path, 'fight')
    assert duration == pytest.approx(5, abs=.15)
    assert 39 <= len(frames) <= 41


@pytest.mark.parametrize('duration', [5, 121.5])
def test_streamed_webm_without_duration_is_bounded(tmp_path, duration):
    # Streaming to stdout omits the seekable WebM duration, like MediaRecorder.
    result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=8',
                             '-t', str(duration), '-c:v', 'libvpx', '-f', 'webm', 'pipe:1'], capture_output=True, check=True)
    path = tmp_path / 'recording.webm'
    path.write_bytes(result.stdout)
    if duration > 120:
        with pytest.raises(ValueError, match='120 seconds'):
            read_video(path, 'fight')
    else:
        frames, measured = read_video(path, 'fight')
        assert measured == pytest.approx(5, abs=.15)
        assert len(frames) >= 39
