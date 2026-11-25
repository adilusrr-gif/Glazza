"""Cancel the real queue worker with a second reserved clip at either download boundary."""

import asyncio
import json
import time
from collections import Counter
from contextlib import suppress

import httpx
import pytest

from app.main import create_app


def initialize(engine):
    engine.store.execute("""UPDATE sites SET modules='["theft"]' WHERE id='default'""")
    engine.health['analyzer'] = {'state': 'ready', 'models': [{'id': 'vadclip', 'state': 'ready'}]}


async def attach(engine, upstream):
    await engine.client.aclose()
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))


class PausedClip(httpx.AsyncByteStream):
    def __init__(self):
        self.paused = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield b'x' * (64 * 1024)
        self.paused.set()
        await asyncio.Future()

    async def aclose(self):
        self.closed = True


def success():
    return httpx.Response(200, json={'checked': ['theft'], 'duration_seconds': 12, 'detections': []})


async def stop(worker):
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize('mid_download', [True, False], ids=['unfinished-download', 'completed-prefetch'])
def test_cancel_and_restart_preserve_reservations_and_complete_clips(app, settings, mid_download):
    async def exercise():
        engine = app.state.engine
        initialize(engine)
        store = engine.store
        folder = settings.data / 'jobs'
        original_attempts = {'first': 1, 'second': 2}
        for order, (name, attempts) in enumerate(original_attempts.items()):
            assert store.enqueue(
                id=name, camera='shop', site_id='default', source_id=name, now=time.time() - 30 + order
            )
            store.execute('UPDATE jobs SET attempts=? WHERE id=?', (attempts, name))

        blocked = PausedClip()
        first_post = asyncio.Event()
        restarting = False
        gets, posts = Counter(), []
        media = {name: ('complete ' + name).encode() for name in original_attempts}

        async def upstream(request):
            if request.method == 'GET':
                name = request.url.path.split('/')[-2]
                gets[name] += 1
                if name == 'second' and mid_download and not restarting:
                    return httpx.Response(200, stream=blocked)
                return httpx.Response(200, content=media[name])
            name = json.loads(request.content)['job_id']
            assert (folder / (name + '.mp4')).read_bytes() == media[name]
            posts.append(name)
            if not restarting:
                first_post.set()
                await asyncio.Future()
            return success()

        await attach(engine, upstream)
        initial_tasks = asyncio.all_tasks()
        worker = asyncio.create_task(engine.work())
        try:
            await until(first_post.is_set)
            if mid_download:
                await until(blocked.paused.is_set)
                assert (folder / 'second.part').stat().st_size == 64 * 1024
            else:
                await until(lambda: (folder / 'second.mp4').is_file())
            assert store.rows("SELECT count(*) n FROM jobs WHERE state='running'")[0]['n'] == 2
        finally:
            await stop(worker)
            await engine.client.aclose()

        assert not (asyncio.all_tasks() - initial_tasks), 'worker left a nested task after shutdown'
        assert posts == ['first']
        names = sorted(f.name for f in folder.iterdir())
        assert names == (['first.mp4'] if mid_download else ['first.mp4', 'second.mp4'])
        assert not mid_download or blocked.closed
        saved = store.rows('SELECT id,state,attempts,file FROM jobs ORDER BY created')
        assert [(r['id'], r['state'], r['attempts'], r['file']) for r in saved] == [
            ('first', 'queued', 1, 'first.mp4'),
            ('second', 'queued', 2, None if mid_download else 'second.mp4'),
        ]

        restarting = True
        fresh = create_app(settings, background=False).state.engine
        initialize(fresh)
        await attach(fresh, upstream)
        worker = asyncio.create_task(fresh.work())
        try:
            await until(lambda: not fresh.store.rows("SELECT id FROM jobs WHERE state!='succeeded'"))
        finally:
            await stop(worker)
            await fresh.client.aclose()
        assert posts == ['first', 'first', 'second']
        assert gets == {'first': 1, 'second': 2 if mid_download else 1}
        attempts = fresh.store.rows('SELECT id,attempts FROM jobs ORDER BY created')
        assert [(r['id'], r['attempts']) for r in attempts] == [('first', 2), ('second', 3)]
        assert not (asyncio.all_tasks() - initial_tasks)

    asyncio.run(exercise())


def test_prefetched_clip_survives_disable_manual_retry_and_analyzer_503(app, monkeypatch):
    async def exercise():
        engine = app.state.engine
        initialize(engine)
        store = engine.store
        store.execute("""INSERT INTO sites VALUES('other','default','Other','retail','["theft"]')""")
        wall = time.time()
        monkeypatch.setattr(time, 'time', lambda: wall)
        for i, (name, site) in enumerate([('first', 'default'), ('second', 'other')]):
            assert store.enqueue(id=name, camera='shop', source_id=name, site_id=site, now=wall - 30 + i)
        release = asyncio.Event()
        gets, posts = Counter(), Counter()

        def second():
            return store.rows("SELECT state,attempts,file,updated FROM jobs WHERE id='second'")[0]

        async def upstream(req):
            if req.method == 'GET':
                name = req.url.path.split('/')[-2]
                gets[name] += 1
                return httpx.Response(200, content=('complete ' + name).encode())
            name = json.loads(req.content)['job_id']
            posts[name] += 1
            if name == 'first':
                await release.wait()
            if name == 'second' and posts[name] == 1:
                return httpx.Response(503)
            return success()

        await attach(engine, upstream)
        worker = asyncio.create_task(engine.work())
        try:
            await until(lambda: posts['first'] == 1 and second()['file'] == 'second.mp4')
            store.execute("""UPDATE sites SET modules='["person"]' WHERE id='other'""")
            release.set()
            await until(lambda: second()['state'] == 'failed')
            assert second()['attempts'] == 0 and posts['second'] == 0
            store.execute("""UPDATE sites SET modules='["theft"]' WHERE id='other'""")
            assert store.retry_job('second') == 'queued'
            await until(lambda: posts['second'] == 1 and second()['state'] == 'queued')
            row = second()
            assert row['attempts'] == 0 and row['updated'] > wall and row['file'] == 'second.mp4'
            wall = row['updated'] + 1
            await until(lambda: second()['state'] == 'succeeded')
            assert gets == {'first': 1, 'second': 1} and posts == {'first': 1, 'second': 2}
            assert second()['attempts'] == 1
        finally:
            release.set()
            await stop(worker)
            await engine.client.aclose()

    asyncio.run(exercise())


def test_process_restart_refunds_both_interrupted_claims_without_shortening_queued_backoff(
    app, settings, monkeypatch
):
    async def exercise():
        old = app.state.engine
        initialize(old)
        store = old.store
        clock = time.time()
        prior_attempts = {'active': 1, 'staged': 2, 'backoff': 1}
        cache = settings.data / 'jobs'
        for n, (name, attempts) in enumerate(prior_attempts.items()):
            filename = name + '.mp4'
            (cache / filename).write_bytes(('recording ' + name).encode())
            assert store.enqueue(
                id=name, camera='shop', site_id='default', source_id=name, file=filename, now=clock - 50 + n
            )
            store.execute('UPDATE jobs SET attempts=? WHERE id=?', (attempts, name))
        store.execute("UPDATE jobs SET updated=?,error='retry later' WHERE id='backoff'", (clock + 45,))

        assert [old.claim_next_job()['id'] for _ in range(2)] == ['active', 'staged']
        assert old.claim_next_job() is None
        query = 'SELECT * FROM jobs ORDER BY id'
        before = {r['id']: r for r in store.rows(query)}
        assert before['active']['state'] == before['staged']['state'] == 'running'
        # Hard process loss skips the old worker's cancellation/finally paths.
        await old.client.aclose()
        fresh = create_app(settings, background=False).state.engine

        async def idle(_name, _worker):
            await asyncio.Future()

        monkeypatch.setattr(fresh, 'supervise', idle)
        monkeypatch.setattr(fresh.restaurant_feed, 'start', lambda: None)
        await fresh.start()
        try:
            after = {r['id']: r for r in fresh.store.rows(query)}
            assert after['backoff'] == before['backoff']
            for name in ('active', 'staged'):
                row = after[name]
                assert row == {
                    **before[name],
                    'state': 'queued',
                    'attempts': prior_attempts[name],
                    'updated': row['updated'],
                }
                assert clock <= row['updated'] <= time.time()
                assert (cache / row['file']).read_bytes() == ('recording ' + name).encode()
        finally:
            await fresh.close()

    asyncio.run(exercise())
