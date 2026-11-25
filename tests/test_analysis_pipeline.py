"""Queue integration tests. Fake only Frigate's clip response and the analyzer HTTP boundary."""
import asyncio
from contextlib import suppress
import json
import threading
import time

import httpx
import pytest


@pytest.fixture
def engine(app):
    engine = app.state.engine
    engine.store.execute("UPDATE sites SET modules=? WHERE id='default'", (json.dumps(['theft']),))
    engine.health['analyzer'] = {'state': 'ready', 'models': [{'id': 'vadclip', 'state': 'ready'}]}
    return engine


def video(name):
    return ('complete recording ' + name).encode()


def enqueue(engine, name, *, local=False, now=None):
    filename = name + '.mp4' if local else None
    if filename:
        (engine.settings.data / 'jobs' / filename).write_bytes(video(name))
    assert engine.store.enqueue(id=name, camera='shop', source_id=None if local else name,
                                site_id='default', file=filename, now=now)


def result():
    return httpx.Response(200, json={'checked': ['theft'], 'duration_seconds': 12, 'detections': []})


async def wait_for_success(engine):
    while engine.store.rows("SELECT id FROM jobs WHERE state!='succeeded'"):
        await asyncio.sleep(0)


async def stop(worker, engine):
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    await engine.client.aclose()


@pytest.mark.parametrize('late', [False, True], ids=['already-ready', 'arrives-after-empty-check'])
def test_ready_local_jobs_need_no_clock_tick_between_inferences(engine, monkeypatch, late):
    async def exercise():
        loop = asyncio.get_running_loop()
        completed, empty_check = asyncio.Event(), asyncio.Event()
        posts = []
        recording_dir = engine.settings.data / 'jobs'
        enqueue(engine, 'first', local=True, now=time.time() - 30)
        if late:
            # Observe the real SQLite queue read so the arrival falls precisely
            # after a miss during inference. Do not alter the selected job.
            select_job = engine.claim_next_job

            def observed_claim():
                claimed = select_job()
                if claimed is None and posts == ['first']:
                    empty_check.set()
                return claimed

            monkeypatch.setattr(engine, 'claim_next_job', observed_claim)
        else:
            enqueue(engine, 'second', local=True, now=time.time() - 29)

        async def analyze(request):
            assert request.method == 'POST' and request.url.path == '/analyze'
            name = json.loads(request.content)['job_id']
            assert (recording_dir / (name + '.mp4')).read_bytes() == video(name)
            posts.append(name)
            if name == 'first' and late:
                await empty_check.wait()
                enqueue(engine, 'second', local=True)
                # Return the HTTP result immediately, before another poll can run.
            if name == 'second':
                completed.set()
            return result()

        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(analyze))
        frozen = loop.time()
        # A real-time watchdog makes an accidental worker sleep fail the test.
        # Event-loop time never advances, including across the inference handoff.
        watchdog = threading.Timer(4, lambda: loop.call_soon_threadsafe(completed.set))
        worker = asyncio.create_task(engine.work())
        try:
            with monkeypatch.context() as clock:
                clock.setattr(loop, 'time', lambda: frozen)
                watchdog.start()
                await completed.wait()
            assert posts == ['first', 'second'], 'A queued, ready job waited for a timer after its predecessor'
            async with asyncio.timeout(4):
                await wait_for_success(engine)
        finally:
            watchdog.cancel()
            watchdog.join()
            await stop(worker, engine)

    asyncio.run(exercise())


@pytest.mark.parametrize('late_arrivals', [False, True], ids=['already-queued', 'enqueued-during-inference'])
def test_one_future_recording_is_staged_while_current_inference_is_running(engine, late_arrivals):
    async def exercise():
        started, release, second_download, third_inference = (asyncio.Event() for _ in range(4))
        names = ('current', 'next', 'later')
        downloads, posts, active_models = [], [], set()
        overlaps = []
        recording_dir = engine.settings.data / 'jobs'
        base = time.time() - 30

        async def upstream(request):
            if request.method == 'GET':
                name = request.url.path.split('/')[-2]
                assert request.url.path == '/api/events/' + name + '/clip.mp4'
                if name == 'next':
                    # Let the first inference start even if the next GET was dispatched
                    # just before its POST. The bytes never arrive before that POST.
                    await started.wait()
                downloads.append((name, release.is_set()))
                if name == 'next':
                    second_download.set()
                return httpx.Response(200, content=video(name))
            assert request.method == 'POST' and request.url.path == '/analyze'
            name = json.loads(request.content)['job_id']
            assert (recording_dir / (name + '.mp4')).read_bytes() == video(name)
            if active_models:
                overlaps.append((name, sorted(active_models)))
            posts.append(name)
            active_models.add(name)
            try:
                if name == 'current':
                    started.set()
                    await release.wait()
                if name == 'later':
                    third_inference.set()
                return result()
            finally:
                active_models.remove(name)

        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        enqueue(engine, names[0], now=base)
        if not late_arrivals:
            enqueue(engine, names[1], now=base + 1)
            enqueue(engine, names[2], now=base + 2)
        worker = asyncio.create_task(engine.work())
        try:
            async with asyncio.timeout(8):
                await started.wait()
                if late_arrivals:
                    enqueue(engine, names[1])
                    enqueue(engine, names[2])
                await second_download.wait()
                staged = recording_dir / (names[1] + '.mp4')
                while not staged.is_file():
                    await asyncio.sleep(0)
                assert staged.read_bytes() == video(names[1])
                # Flush runnable continuations before unlocking inference; a third
                # clip must still be queued even after the second is fully on disk.
                for _ in range(3):
                    await asyncio.sleep(0)
                assert posts == ['current']
                assert not (recording_dir / (names[2] + '.mp4')).exists()
                release.set()
                await third_inference.wait()
                await wait_for_success(engine)
            assert downloads == [('current', False), ('next', False), ('later', True)]
            assert posts == list(names) and not overlaps
            rows = engine.store.rows('SELECT id, attempts FROM jobs ORDER BY created')
            assert [(r['id'], r['attempts']) for r in rows] == [(n, 1) for n in names]
        finally:
            release.set()
            await stop(worker, engine)

    asyncio.run(exercise())
