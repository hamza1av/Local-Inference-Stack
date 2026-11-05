"""Tracing: Langfuse v3 SDK (OpenTelemetry) -> the stack's recorder, silent when it is down.

* `setup()` once per process: creates the Langfuse client (host = recorder base, public key
  `pk-lf-agents`) and instruments httpx so every outgoing call (OpenAI SDK -> gateway, decision
  API, MCP) carries a W3C `traceparent` header. The gateway nests its generation spans under it.
* `run_span(name, ...)` wraps one agent run in a root span (so the httpx spans have a parent).
* `callbacks()` returns the LangChain CallbackHandler for graph runs (or []).

Everything degrades to a no-op: tracing disabled (`AGENTS_TRACING=0`), no secret, SDK import
failure, recorder unreachable (export errors are logged at DEBUG only).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

PUBLIC_KEY = "pk-lf-agents"
_client: Any = None
_ready = False


def setup() -> bool:
    """Idempotent. Returns True when traces are being recorded."""
    global _client, _ready
    if _ready:
        return _client is not None
    _ready = True
    s = settings()
    if not s.tracing or not s.langfuse_secret:
        return False
    # exporter failures (recorder down) must not spam the logs
    for name in ("opentelemetry.exporter.otlp.proto.http", "opentelemetry.sdk._shared_internal",
                 "opentelemetry.sdk.trace.export", "langfuse"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        from langfuse import Langfuse
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        _client = Langfuse(
            public_key=PUBLIC_KEY,
            secret_key=s.langfuse_secret,
            host=s.recorder_base,
            timeout=3,
            flush_interval=2,
            environment="local",
        )
        HTTPXClientInstrumentor().instrument()
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("tracing disabled: %s", e)
        _client = None
        return False


def callbacks() -> list:
    if _client is None:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler(public_key=PUBLIC_KEY)]
    except Exception:  # noqa: BLE001
        return []


@contextlib.contextmanager
def run_span(name: str, *, input: Any = None, session_id: str | None = None) -> Iterator[dict]:
    """Root span for one agent run. Yields a dict that receives `trace_id` (or None)."""
    info: dict = {"trace_id": None}
    if _client is None:
        yield info
        return
    try:
        cm = _client.start_as_current_span(name=name, input=input)
        span = cm.__enter__()
    except Exception:  # noqa: BLE001
        yield info
        return
    try:
        info["trace_id"] = getattr(span, "trace_id", None)
        try:
            span.update_trace(name=name, session_id=session_id, tags=["app:agents"])
        except Exception:  # noqa: BLE001
            pass
        info["span"] = span
        yield info
    finally:
        try:
            if "output" in info:
                span.update(output=info["output"])
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def flush() -> None:
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.flush()


def shutdown() -> None:
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.shutdown()
