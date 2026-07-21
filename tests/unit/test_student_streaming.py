"""Unit tests for ``POST /api/v1/student/ask/stream``.

These run end-to-end against the real ASGI app with the tutor service
overridden via FastAPI's ``dependency_overrides`` — that's the same
pattern the E2E flow tests use, and the cheapest way to exercise the
real SSE serialisation + middleware stack.

Concerns covered:

1. **Framing** — the response is ``text/event-stream``, events parse
   per the SSE grammar (``event:`` line, ``data:`` line, blank line).
2. **Event sequence** — exactly one ``meta`` up-front, ≥1 ``chunk``
   for the happy path, then exactly one ``done`` with ``final_answer``
   matching the assembled chunks.
3. **Clarifier short-circuit** — when the tutor service yields a
   ``needs_clarification`` meta + done, no ``chunk`` events appear.
4. **Guard replacement** — when the streamed text differs from the
   guarded final, ``done.final_answer`` is non-null.
5. **Cancellation** — disconnecting mid-stream cancels the upstream
   generator (the stub records that it was cancelled).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_tutor_service
from app.services.student.tutor_service import (
    StudentTutorService,
    TutorStreamChunk,
    TutorStreamDone,
    TutorStreamEvent,
    TutorStreamMeta,
)


# --- SSE parsing helper -------------------------------------------------


_EVENT_RE = re.compile(
    r"event:\s*(?P<event>[^\n]+)\ndata:\s*(?P<data>[^\n]*)\n\n",
    re.MULTILINE,
)


def _parse_sse(blob: str) -> list[tuple[str, dict]]:
    """Parse a full SSE response into ``[(event_name, data_dict), ...]``."""

    out: list[tuple[str, dict]] = []
    for m in _EVENT_RE.finditer(blob):
        out.append((m["event"].strip(), json.loads(m["data"])))
    return out


# --- Stub tutor service -------------------------------------------------


class _StubStreamingTutor:
    """Yields a scripted sequence of events for the route to format."""

    def __init__(
        self,
        events: list[TutorStreamEvent],
        *,
        sleep_per_chunk: float = 0.0,
    ) -> None:
        self._events = events
        self._sleep = sleep_per_chunk
        self.cancelled = False
        self.chunks_emitted = 0

    async def ask_stream(
        self,
        *,
        question: str,
        student_class: str,
        subject: str | None = None,
        history=None,
        learning_state=None,
        user_id=None,
        conversation_id=None,
    ) -> AsyncIterator[TutorStreamEvent]:
        try:
            for ev in self._events:
                if isinstance(ev, TutorStreamChunk) and self._sleep:
                    await asyncio.sleep(self._sleep)
                if isinstance(ev, TutorStreamChunk):
                    self.chunks_emitted += 1
                yield ev
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _meta(**overrides) -> TutorStreamMeta:
    defaults = dict(
        status="answered",
        student_class="JSS2",
        subject="biology",
        conversation_id=uuid.uuid4(),
        original_question=None,
        corrected_question=None,
        original_subject=None,
        clarifying_question=None,
    )
    defaults.update(overrides)
    return TutorStreamMeta(**defaults)  # type: ignore[arg-type]


async def _read_stream(client: AsyncClient, body: dict | None = None) -> str:
    body = body or {"question": "What is photosynthesis?", "student_class": "JSS2"}
    async with client.stream(
        "POST",
        "/api/v1/student/ask/stream",
        json=body,
        headers={"X-Dev-Role": "student"},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        body_text = ""
        async for piece in resp.aiter_text():
            body_text += piece
        return body_text


# --- Tests --------------------------------------------------------------


async def test_happy_path_emits_meta_chunks_done():
    from main import app

    stub = _StubStreamingTutor(
        [
            _meta(),
            TutorStreamChunk(text="Plants "),
            TutorStreamChunk(text="convert "),
            TutorStreamChunk(text="sunlight."),
            TutorStreamDone(final_answer=None),
        ]
    )
    app.dependency_overrides[get_tutor_service] = lambda: stub
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            blob = await _read_stream(c)
    finally:
        app.dependency_overrides.pop(get_tutor_service, None)

    events = _parse_sse(blob)
    assert [e for e, _ in events] == ["meta", "chunk", "chunk", "chunk", "done"]

    meta_data = events[0][1]
    assert meta_data["status"] == "answered"
    assert meta_data["subject"] == "biology"
    assert meta_data["conversation_id"] is not None
    # 32-hex UUID without dashes is OK too; uuid4 ``str`` is dashed canonical.
    uuid.UUID(meta_data["conversation_id"])

    assembled = "".join(d["text"] for ev, d in events if ev == "chunk")
    assert assembled == "Plants convert sunlight."

    done_data = events[-1][1]
    assert done_data == {"final_answer": None}


async def test_clarifier_short_circuit_emits_no_chunks():
    from main import app

    stub = _StubStreamingTutor(
        [
            _meta(
                status="needs_clarification",
                clarifying_question="Which subject?",
                subject=None,
            ),
            TutorStreamDone(final_answer=None),
        ]
    )
    app.dependency_overrides[get_tutor_service] = lambda: stub
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            blob = await _read_stream(c)
    finally:
        app.dependency_overrides.pop(get_tutor_service, None)

    events = _parse_sse(blob)
    assert [e for e, _ in events] == ["meta", "done"]
    assert events[0][1]["status"] == "needs_clarification"
    assert events[0][1]["clarifying_question"] == "Which subject?"


async def test_guard_replacement_surfaces_in_done():
    from main import app

    stub = _StubStreamingTutor(
        [
            _meta(),
            TutorStreamChunk(text="A streamed answer that the guards will replace."),
            TutorStreamDone(final_answer="The cleaned replacement."),
        ]
    )
    app.dependency_overrides[get_tutor_service] = lambda: stub
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            blob = await _read_stream(c)
    finally:
        app.dependency_overrides.pop(get_tutor_service, None)

    events = _parse_sse(blob)
    done_data = events[-1][1]
    assert done_data["final_answer"] == "The cleaned replacement."


async def test_response_headers_disable_proxy_buffering():
    from main import app

    stub = _StubStreamingTutor(
        [_meta(), TutorStreamChunk(text="hi."), TutorStreamDone(final_answer=None)]
    )
    app.dependency_overrides[get_tutor_service] = lambda: stub
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async with c.stream(
                "POST",
                "/api/v1/student/ask/stream",
                json={"question": "Q?", "student_class": "JSS2"},
                headers={"X-Dev-Role": "student"},
            ) as resp:
                # Headers that matter for proxies:
                assert resp.headers["cache-control"] == "no-cache"
                assert resp.headers["x-accel-buffering"] == "no"
                # Consume the body so the client doesn't error on
                # cleanup.
                async for _ in resp.aiter_bytes():
                    pass
    finally:
        app.dependency_overrides.pop(get_tutor_service, None)


async def test_role_gate_rejects_non_student():
    """The streaming route is wrapped by the same ``require_role`` as /ask."""

    from main import app

    # No stub override needed — we expect a 403 before the service is touched.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/student/ask/stream",
            json={"question": "Q?", "student_class": "JSS2"},
            headers={"X-Dev-Role": "parent"},
        )
    assert resp.status_code == 403


async def test_groq_stream_cancellation_closes_upstream():
    """``GroqChatClient.generate_stream`` closes the Groq stream on cancel.

    Tested against the client directly because httpx's ``ASGITransport``
    doesn't simulate a real socket disconnect — the in-process transport
    drains the server generator before the consumer can break. The
    contract that matters is "cancelling the consumer closes the upstream
    HTTP connection"; that's owned by ``generate_stream``, not the route.
    """

    from app.llm.client import GroqChatClient

    class _FakeChunk:
        def __init__(self, text: str) -> None:
            self.choices = [
                type("C", (), {"delta": type("D", (), {"content": text})()})()
            ]

    closed_flag = {"closed": False}

    class _FakeStream:
        def __init__(self) -> None:
            # Yield slowly enough for the consumer to cancel mid-stream.
            self._chunks = iter([_FakeChunk(f"t{i} ") for i in range(50)])

        def __iter__(self):
            return self

        def __next__(self):
            import time
            time.sleep(0.02)  # 20 ms per chunk → ~1s total
            return next(self._chunks)

        def close(self):
            closed_flag["closed"] = True

    class _FakeCompletions:
        def create(self, **kwargs):
            assert kwargs.get("stream") is True
            return _FakeStream()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    # Build a GroqChatClient without going through __init__ (which
    # constructs the real Groq SDK). We swap the underlying client.
    chat_client = object.__new__(GroqChatClient)
    chat_client.model = "test-model"
    chat_client.temperature = 0.1
    chat_client.max_completion_tokens = 100
    chat_client._client = _FakeClient()

    # Consume a couple of chunks then aclose() the generator — same
    # signal Starlette sends on client disconnect.
    gen = chat_client.generate_stream(system_prompt="sys", user_prompt="user")
    received = 0
    async for _ in gen:
        received += 1
        if received >= 2:
            break
    await gen.aclose()

    # Give the producer thread a moment to react to the close().
    await asyncio.sleep(0.1)
    assert closed_flag["closed"], "Upstream Groq stream was not closed on cancel"


async def test_tutor_ask_stream_skips_persistence_on_cancel():
    """When the consumer cancels mid-stream, ``ask_stream`` must not persist.

    Tested at the tutor-service layer (not route) for the same reason
    as the test above — ASGITransport doesn't simulate disconnect.
    """

    from app.services.student.tutor_service import StudentTutorService
    from app.rag.query_engine.engine import StreamChunk

    class _Router:
        async def route_stream(self, **kwargs):
            for i in range(50):
                await asyncio.sleep(0.02)
                yield StreamChunk(text=f"t{i} ")
            # Intentionally no StreamDone — we'll cancel before completion.

    persisted = {"called": False}

    class _TutorWithStub(StudentTutorService):
        async def _persist_streamed_turn(self, **kwargs):  # type: ignore[override]
            persisted["called"] = True

    svc = _TutorWithStub(router=_Router(), preflight=None, persist_chat=True)  # type: ignore[arg-type]

    gen = svc.ask_stream(
        question="What is gravity?",
        student_class="JSS2",
        subject="physics",
        user_id=uuid.uuid4(),
    )
    seen = 0
    async for _ in gen:
        seen += 1
        if seen >= 3:  # meta + ≥1 chunk
            break
    await gen.aclose()

    await asyncio.sleep(0.05)
    assert persisted["called"] is False, (
        "Persistence task should not be spawned when the consumer cancelled "
        "mid-stream — the accumulated answer is partial."
    )
