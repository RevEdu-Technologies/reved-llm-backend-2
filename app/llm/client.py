"""LLM client helpers for grounded answer generation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from app.core.config import Settings

logger = logging.getLogger(__name__)


class LLMClientError(RuntimeError):
    """Raised when the configured LLM request fails."""


@dataclass(slots=True)
class LLMResponse:
    """Normalized LLM completion payload."""

    text: str
    model: str


class GroqChatClient:
    """Minimal Groq chat-completions client for grounded QA."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float,
        max_completion_tokens: int,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens

        try:
            from groq import Groq
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LLMClientError("groq is required for Groq-backed answer generation.") from exc

        # Explicit timeouts are critical. A bare ``httpx.Client()`` applies
        # httpx's 5s default to *every* phase, including read. For
        # streaming completions ``read`` is the max gap between received
        # tokens — a long generation (e.g. a full study guide) routinely
        # pauses >5s, which would otherwise raise ``ReadTimeout`` mid-stream
        # and abort the answer. Generous read/write, tight connect.
        self._client = Groq(
            api_key=api_key,
            http_client=httpx.Client(
                trust_env=False,
                timeout=httpx.Timeout(120.0, connect=10.0),
            ),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "GroqChatClient":
        """Construct a Groq client from application settings."""

        return cls(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=settings.groq_temperature,
            max_completion_tokens=settings.groq_max_completion_tokens,
        )

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        """Generate a chat completion from Groq.

        ``model``, ``temperature``, and ``max_completion_tokens`` override the
        instance defaults for this call only — useful for the preflight pass,
        which uses a smaller/cheaper model than the answer generation.
        """

        effective_model = model or self.model
        kwargs: dict = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_completion_tokens": (
                max_completion_tokens
                if max_completion_tokens is not None
                else self.max_completion_tokens
            ),
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        try:
            completion = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise LLMClientError(
                f"Groq completion failed for model '{effective_model}'."
            ) from exc

        message = completion.choices[0].message.content if completion.choices else ""

        # Surface token usage to Prometheus. The Groq SDK exposes
        # `completion.usage.prompt_tokens` / `.completion_tokens`. Wrap
        # in try/except so a missing/changed field can never break the
        # response path — telemetry is best-effort.
        try:
            from app.core.metrics import record_llm_tokens

            usage = getattr(completion, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            if prompt_tokens or completion_tokens:
                record_llm_tokens(
                    provider="groq",
                    model=effective_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
        except Exception:  # noqa: BLE001
            pass

        return LLMResponse(text=(message or "").strip(), model=effective_model)

    async def generate_stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> AsyncIterator[str]:
        """Stream a chat completion from Groq token-by-token.

        Bridges the sync Groq SDK to an async generator via a producer
        thread + ``asyncio.Queue``. On consumer cancellation (e.g. the
        SSE client disconnected) we close the underlying Groq stream
        so the upstream HTTP socket is dropped and we stop being billed
        for tokens we'll never deliver.

        ``response_format={"type": "json_object"}`` is supported for
        endpoints that need strict JSON output (teacher lesson-notes,
        parent explain-topic). Groq's streaming + JSON mode emits
        deltas of the JSON document; assembling all deltas yields a
        single valid JSON document at the end.

        Yields each delta string; an empty stream yields nothing.
        Raises ``LLMClientError`` on provider errors. The token-usage
        metric does NOT fire here — Groq doesn't report token totals on
        streaming chunks. If accurate streaming token counts become
        important, a follow-up can either parse the final ``usage``
        message Groq emits or call the non-streaming endpoint after
        the fact with the prompt+answer for billing.
        """

        effective_model = model or self.model
        kwargs: dict = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_completion_tokens": (
                max_completion_tokens
                if max_completion_tokens is not None
                else self.max_completion_tokens
            ),
            "stream": True,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        loop = asyncio.get_running_loop()
        # Queue items: str (delta), None (end), or BaseException (failure).
        queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
        # The Groq stream object is captured here so the consumer can close
        # it on cancellation. Using a mutable container — a plain
        # ``stream`` reference assigned inside the producer wouldn't be
        # visible to the consumer because the producer runs on a thread.
        stream_holder: dict[str, object] = {}

        def _producer() -> None:
            try:
                stream = self._client.chat.completions.create(**kwargs)
                stream_holder["stream"] = stream
                for chunk in stream:
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    delta = getattr(choices[0].delta, "content", None)
                    if delta:
                        loop.call_soon_threadsafe(queue.put_nowait, delta)
            except BaseException as exc:  # noqa: BLE001 - propagated as item
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        producer_task = asyncio.create_task(asyncio.to_thread(_producer))

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise LLMClientError(
                        f"Groq streaming failed for model '{effective_model}'."
                    ) from item
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            # Consumer disconnected (or we were cancelled). Close the
            # upstream stream so Groq stops sending tokens; without this,
            # the producer thread blocks on socket reads until the
            # generation completes server-side and we keep paying for it.
            stream = stream_holder.get("stream")
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        logger.debug("Groq stream close after cancel raised; ignoring.")
            raise
        finally:
            # Don't await the producer — its iteration may still be
            # winding down. The thread is daemon-ish and will exit once
            # the closed socket raises in the underlying for-loop.
            if not producer_task.done():
                producer_task.cancel()
