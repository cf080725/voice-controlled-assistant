"""ASGI lifecycle and cancellation checks with fake synchronous inference."""
import asyncio
import threading

import pytest
from canaan_tts_ref_cache import ReferenceCache, SerializedTTSApp


class Pipeline:
    def __init__(self):
        self.events = []
        self.started, self.close_started = {}, {}
        self.before_yield, self.before_close = {}, {}

    def event(self, name, tag):
        self.events.append((name, tag))
        table = self.started if name == "started" else self.close_started
        table.setdefault(tag, threading.Event()).set()

    def run(self, inputs):
        tag = inputs["tag"]
        try:
            self.event("started", tag)
            if tag in self.before_yield:
                self.before_yield[tag]()
            yield 16000, tag
        finally:
            self.event("closing", tag)
            try:
                if tag in self.before_close:
                    self.before_close[tag]()
            finally:
                self.events.append(("closed", tag))

    def set_device(self, device):
        self.events.append(("mutated", device))


async def wait_thread_event(table, tag):
    for _ in range(100):
        event = table.get(tag)
        if event is not None:
            assert await asyncio.to_thread(event.wait, .5)
            return
        await asyncio.sleep(.001)
    pytest.fail(f"Fixture never reached {tag}")


async def call(app, path, tag="request", receive=None):
    async def receive_default():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await app({"type": "http", "path": path, "tag": tag}, receive or receive_default, send)
    return sent


def candidate(tmp_path, pipeline, app):
    cache = ReferenceCache(pipeline, [tmp_path / "ref.wav"]).install()
    return SerializedTTSApp(app, cache)


def test_nonstreaming_next_once_is_closed_before_request_finishes(tmp_path):
    pipeline = Pipeline()

    async def app(scope, receive, send):
        output = pipeline.run({"tag": scope["tag"]})
        assert await asyncio.to_thread(next, output)
        assert ("closed", scope["tag"]) not in pipeline.events
        await send({"type": "http.response.body", "body": b"fake audio"})

    wrapped = candidate(tmp_path, pipeline, app)
    asyncio.run(call(wrapped, "/tts"))
    assert pipeline.events[-1] == ("closed", "request")


def test_gate_covers_the_whole_streaming_response_and_model_mutations(tmp_path):
    pipeline = Pipeline()

    async def scenario():
        body_ready, release_body = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            if scope["path"].startswith("/set_"):
                pipeline.set_device(scope["tag"])
                return
            if scope["path"] == "/control":
                return
            output = pipeline.run({"tag": scope["tag"]})
            assert await asyncio.to_thread(next, output)
            await send({"type": "http.response.body", "body": b"first fragment", "more_body": True})
            if scope["tag"] == "first":
                body_ready.set()
                await release_body.wait()
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        wrapped = candidate(tmp_path, pipeline, app)
        first = asyncio.create_task(call(wrapped, "/tts", "first"))
        await asyncio.wait_for(body_ready.wait(), .5)
        second = asyncio.create_task(call(wrapped, "/tts", "second"))
        mutation = asyncio.create_task(call(wrapped, "/set_device", "cpu"))
        await asyncio.sleep(.01)
        assert ("started", "second") not in pipeline.events and ("mutated", "cpu") not in pipeline.events
        assert await asyncio.wait_for(call(wrapped, "/control"), .2) == []
        release_body.set()
        await asyncio.wait_for(asyncio.gather(first, second, mutation), 1)

    asyncio.run(scenario())
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("started", "second"))
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("mutated", "cpu"))


def test_disconnect_waits_for_generator_close_in_worker_without_blocking_asgi(tmp_path):
    pipeline = Pipeline()
    release_close = threading.Event()
    pipeline.before_close["first"] = lambda: release_close.wait(2)

    async def scenario():
        body_ready = asyncio.Event()
        disconnect = asyncio.Queue()

        async def app(scope, receive, send):
            if scope["path"] == "/control":
                return
            output = pipeline.run({"tag": scope["tag"]})
            assert await asyncio.to_thread(next, output)
            if scope["tag"] == "first":
                body_ready.set()
                assert (await receive())["type"] == "http.disconnect"

        wrapped = candidate(tmp_path, pipeline, app)
        first = asyncio.create_task(call(wrapped, "/tts", "first", disconnect.get))
        second = None
        try:
            await asyncio.wait_for(body_ready.wait(), .5)
            await disconnect.put({"type": "http.disconnect"})
            await wait_thread_event(pipeline.close_started, "first")
            second = asyncio.create_task(call(wrapped, "/tts", "second"))
            await asyncio.sleep(.01)
            assert ("started", "second") not in pipeline.events
            assert await asyncio.wait_for(call(wrapped, "/control"), .2) == []
        finally:
            release_close.set()
            await asyncio.wait_for(first, 1)
            if second:
                await asyncio.wait_for(second, 1)

    asyncio.run(scenario())
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("started", "second"))


def test_repeated_cancellation_waits_for_running_next_then_close_before_next_request(tmp_path):
    pipeline = Pipeline()
    release_next, release_close = threading.Event(), threading.Event()
    pipeline.before_yield["first"] = lambda: release_next.wait(2)
    pipeline.before_close["first"] = lambda: release_close.wait(2)

    async def scenario():
        async def app(scope, receive, send):
            if scope["path"] == "/control":
                return
            output = pipeline.run({"tag": scope["tag"]})
            assert await asyncio.to_thread(next, output)

        wrapped = candidate(tmp_path, pipeline, app)
        first = asyncio.create_task(call(wrapped, "/tts", "first"))
        second = None
        try:
            await wait_thread_event(pipeline.started, "first")
            first.cancel()
            await asyncio.sleep(.01)
            first.cancel()
            second = asyncio.create_task(call(wrapped, "/tts", "second"))
            await asyncio.sleep(.01)
            assert ("started", "second") not in pipeline.events
            assert not first.done(), "Cancellation abandoned cleanup while inference was still executing"
            assert await asyncio.wait_for(call(wrapped, "/control"), .2) == []
            release_next.set()
            await wait_thread_event(pipeline.close_started, "first")
            assert ("started", "second") not in pipeline.events
        finally:
            release_next.set()
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 1)
            if second:
                await asyncio.wait_for(second, 1)

    asyncio.run(scenario())
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("started", "second"))


def test_cancelling_a_gate_waiter_does_not_close_the_active_inference(tmp_path):
    pipeline = Pipeline()

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            output = pipeline.run({"tag": scope["tag"]})
            assert await asyncio.to_thread(next, output)
            if scope["tag"] == "first":
                ready.set()
                await release.wait()

        wrapped = candidate(tmp_path, pipeline, app)
        first = asyncio.create_task(call(wrapped, "/tts", "first"))
        await asyncio.wait_for(ready.wait(), .5)
        second = asyncio.create_task(call(wrapped, "/tts", "second"))
        await asyncio.sleep(.01)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert ("closing", "first") not in pipeline.events and ("started", "second") not in pipeline.events
        release.set()
        await asyncio.wait_for(first, 1)

    asyncio.run(scenario())


def test_app_error_closes_active_generator_and_releases_gate(tmp_path):
    pipeline = Pipeline()

    async def app(scope, receive, send):
        output = pipeline.run({"tag": scope["tag"]})
        assert await asyncio.to_thread(next, output)
        if scope["tag"] == "first":
            raise RuntimeError("fixture response failed")

    async def scenario():
        wrapped = candidate(tmp_path, pipeline, app)
        with pytest.raises(RuntimeError, match="response failed"):
            await call(wrapped, "/tts", "first")
        assert ("closed", "first") in pipeline.events
        await asyncio.wait_for(call(wrapped, "/tts", "second"), .5)

    asyncio.run(scenario())


def test_generator_cleanup_error_does_not_leave_gate_locked(tmp_path, capsys):
    pipeline = Pipeline()

    def fail_close():
        raise RuntimeError("fixture private content must not be logged")

    pipeline.before_close["first"] = fail_close

    async def app(scope, receive, send):
        output = pipeline.run({"tag": scope["tag"]})
        assert await asyncio.to_thread(next, output)

    async def scenario():
        wrapped = candidate(tmp_path, pipeline, app)
        await call(wrapped, "/tts", "first")
        await asyncio.wait_for(call(wrapped, "/tts", "second"), .5)

    asyncio.run(scenario())
    logged = capsys.readouterr().out
    assert "RuntimeError" in logged and "private content" not in logged


def test_non_http_lifecycle_and_unrelated_health_requests_are_unmodified(tmp_path):
    pipeline, seen = Pipeline(), []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    async def scenario():
        wrapped = candidate(tmp_path, pipeline, app)
        await wrapped({"type": "lifespan"}, None, None)
        await call(wrapped, "/health")

    asyncio.run(scenario())
    assert seen == ["lifespan", "http"]


def test_real_starlette_disconnect_waits_for_fake_synchronous_inference(tmp_path):
    responses = pytest.importorskip("starlette.responses")
    pipeline = Pipeline()
    release_next = threading.Event()
    pipeline.before_yield["first"] = lambda: release_next.wait(2)

    async def scenario():
        disconnected = asyncio.Queue()

        async def app(scope, receive, send):
            def audio():
                for _, data in pipeline.run({"tag": scope["tag"]}):
                    yield data.encode()

            await responses.StreamingResponse(audio())(scope, receive, send)

        async def send(_message):
            pass

        async def wait_receive():
            await asyncio.Future()

        wrapped = candidate(tmp_path, pipeline, app)
        first = asyncio.create_task(wrapped({"type": "http", "path": "/tts", "tag": "first"}, disconnected.get, send))
        second = None
        try:
            await wait_thread_event(pipeline.started, "first")
            await disconnected.put({"type": "http.disconnect"})
            second = asyncio.create_task(wrapped({"type": "http", "path": "/tts", "tag": "second"}, wait_receive, send))
            await asyncio.sleep(.01)
            assert ("started", "second") not in pipeline.events
        finally:
            release_next.set()
            await asyncio.wait_for(first, 1)
            if second:
                await asyncio.wait_for(second, 1)

    asyncio.run(scenario())
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("started", "second"))


def test_outer_anyio_cancel_scope_cannot_abandon_inference_cleanup(tmp_path):
    anyio = pytest.importorskip("anyio")
    pipeline = Pipeline()
    release_next = threading.Event()
    pipeline.before_yield["first"] = lambda: release_next.wait(2)

    async def scenario():
        scopes = {}
        first_done, second_done = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            if scope["path"] != "/control":
                output = pipeline.run({"tag": scope["tag"]})
                assert await asyncio.to_thread(next, output)

        wrapped = candidate(tmp_path, pipeline, app)

        async def first():
            with anyio.CancelScope() as cancel:
                scopes["first"] = cancel
                await call(wrapped, "/tts", "first")
            first_done.set()

        async def second():
            await call(wrapped, "/tts", "second")
            second_done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(first)
            await wait_thread_event(pipeline.started, "first")
            scopes["first"].cancel()
            group.start_soon(second)
            try:
                await asyncio.sleep(.01)
                assert not first_done.is_set() and ("started", "second") not in pipeline.events
                assert await asyncio.wait_for(call(wrapped, "/control"), .2) == []
            finally:
                release_next.set()
            await asyncio.wait_for(first_done.wait(), 1)
            await asyncio.wait_for(second_done.wait(), 1)

    asyncio.run(scenario())
    assert pipeline.events.index(("closed", "first")) < pipeline.events.index(("started", "second"))
