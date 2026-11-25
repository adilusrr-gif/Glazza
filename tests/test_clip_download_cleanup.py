"""Only complete Frigate exports should become durable evidence for queued jobs."""
import asyncio
import gzip

import httpx
import pytest


JOB = {'camera': 'aisle', 'source_id': 'track123'}


class ClipStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, pause=False):
        self.chunks = chunks
        self.pause = pause
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.pause:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(('status', 'headers', 'chunks'), [
    (206, {'content-range': 'bytes 0-3/12'}, [b'data']),
    (200, {'content-range': 'bytes 0-3/12'}, [b'data']),
    (200, {'content-length': '12'}, [b'data']),
    (200, {'content-length': '2'}, [b'data']),
    (200, {'content-length': 'many'}, [b'data']),
    (200, {'content-length': '18'}, [b'x' * 18]),
    (200, {'content-length': '0'}, []),
    (200, {}, []),
    (200, {}, [b'abcdefgh', b'ijklmnopq']),
    (200, {'content-type': 'text/html'}, [b'<html>']),
])
def test_rejected_export_is_never_published(app, settings, status, headers, chunks):
    engine = app.state.engine
    object.__setattr__(settings, 'max_upload', 16)
    stream = ClipStream(chunks)
    path = settings.data / 'jobs' / 'track.mp4'
    path.write_bytes(b'previously complete')
    temporary = path.with_suffix('.part')
    temporary.write_bytes(b'previous incomplete transfer')

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(status, headers=headers, stream=stream))) as client:
            engine.client = client
            with pytest.raises(ValueError):
                await engine.download_clip(JOB, path)

    asyncio.run(run())
    assert stream.closed and not temporary.exists()
    assert path.read_bytes() == b'previously complete'


@pytest.mark.parametrize('stop_by', ['cancellation', 'total deadline'])
def test_interrupted_export_is_removed_then_retry_finishes(app, settings, monkeypatch, stop_by):
    engine = app.state.engine
    path = settings.data / 'jobs' / 'track.mp4'
    temporary = path.with_suffix('.part')
    temporary.write_bytes(b'partial data left by a process exit')
    interrupted = ClipStream([b'x' * 65536], pause=True)
    completed = ClipStream([b'complete ', b'Frigate export'])
    streams = iter([interrupted, completed])

    def respond(request):
        assert request.url.path == '/api/events/track123/clip.mp4'
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, headers={'content-type': 'video/mp4'}, stream=next(streams))

    async def run():
        if stop_by == 'total deadline':
            original_timeout = asyncio.timeout
            monkeypatch.setattr(asyncio, 'timeout', lambda seconds: original_timeout(0.1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine.client = client
            pending = asyncio.create_task(engine.download_clip(JOB, path))
            await asyncio.wait_for(interrupted.waiting.wait(), timeout=1)
            assert temporary.exists() and not path.exists()
            if stop_by == 'cancellation':
                pending.cancel()
            with pytest.raises(asyncio.CancelledError if stop_by == 'cancellation' else TimeoutError):
                await pending
            assert interrupted.closed and not temporary.exists() and not path.exists()
            await engine.download_clip(JOB, path)

    asyncio.run(run())
    assert completed.closed and not temporary.exists()
    assert path.read_bytes() == b'complete Frigate export'


@pytest.mark.parametrize('length_error', [0, 1])
def test_content_coded_export_checks_wire_length(app, settings, length_error):
    engine = app.state.engine
    path = settings.data / 'jobs' / 'track.mp4'
    decoded = b'media bytes' * 50
    encoded = gzip.compress(decoded)
    stream = ClipStream([encoded])
    headers = {'content-type': 'video/mp4', 'content-encoding': 'gzip',
               'content-length': str(len(encoded) + length_error)}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, headers=headers, stream=stream))) as client:
            engine.client = client
            if length_error:
                with pytest.raises(ValueError):
                    await engine.download_clip(JOB, path)
                assert not path.exists()
            else:
                await engine.download_clip(JOB, path)
                assert path.read_bytes() == decoded

    asyncio.run(run())
    assert not path.with_suffix('.part').exists()
