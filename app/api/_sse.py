"""Server-Sent Events helpers shared by streaming routes.

SSE framing is dead simple — ``event: <type>\ndata: <line>\n\n`` — but
two concerns are easy to get wrong and worth centralising:

1. **Multi-line ``data``.** SSE splits ``data`` on newlines; each line
   must be prefixed with ``data: `` and the event is terminated by a
   blank line. We JSON-serialize the payload with no embedded newlines
   (``separators=(",", ":")``) so we only ever emit one ``data:`` line
   per event.

2. **Proxy buffering.** Nginx and friends buffer responses by default,
   which breaks token-level streaming. The recommended headers below
   tell the proxy to forward bytes as they arrive.
"""

from __future__ import annotations

import json
from typing import Any

# Headers a streaming route should set on its StreamingResponse so the
# bytes actually reach the client as they're produced.
SSE_RESPONSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx-specific — without this, an nginx ingress buffers the whole
    # body before flushing, defeating streaming. Harmless when the proxy
    # is anything else.
    "X-Accel-Buffering": "no",
}

SSE_MEDIA_TYPE = "text/event-stream"


def format_sse(event: str, data: Any) -> bytes:
    """Serialize ``data`` as a single SSE event frame.

    ``event`` is the SSE event name (``meta``, ``chunk``, ``done``,
    ``error``). ``data`` is JSON-serialized — pass dicts, dataclass
    asdict()s, or primitives.
    """

    payload = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


# --- OpenAI-compatible chat-completions stream framing --------------------
#
# Some frontend consumers (e.g. the RevEd web app's content generator)
# parse the OpenAI streaming wire format directly: lines of
# ``data: {"choices":[{"delta":{"content":"..."}}]}`` terminated by a
# final ``data: [DONE]``. There is no ``event:`` line. These helpers emit
# that exact shape so such clients work unchanged.

OPENAI_SSE_DONE: bytes = b"data: [DONE]\n\n"


def format_openai_chunk(content: str) -> bytes:
    """Frame a content delta as an OpenAI chat-completions stream chunk."""

    payload = json.dumps(
        {"choices": [{"delta": {"content": content}, "index": 0}]},
        separators=(",", ":"),
    )
    return f"data: {payload}\n\n".encode("utf-8")


def format_openai_error(message: str) -> bytes:
    """Frame an error as a plain ``data:`` JSON line (no event name).

    Mirrors the ``{"error": "..."}`` body the frontend's previous content
    function returned on failure, so existing error handling still fires.
    """

    payload = json.dumps({"error": message}, separators=(",", ":"))
    return f"data: {payload}\n\n".encode("utf-8")


__all__ = [
    "SSE_MEDIA_TYPE",
    "SSE_RESPONSE_HEADERS",
    "OPENAI_SSE_DONE",
    "format_sse",
    "format_openai_chunk",
    "format_openai_error",
]
