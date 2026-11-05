"""OpenAI-compatible server for the agent graph (model `agent`), run by `stack-agents serve`.

The gateway routes `"model": "agent"` here, so Open WebUI users can pick the agent like a model.

Thread id (conversation memory in $STACK_DATA/agents.db), first match wins:
  1. header `x-thread-id`                        - explicit, used as is
  2. header `x-openwebui-chat-id`                - Open WebUI with ENABLE_FORWARD_USER_INFO_HEADERS
  3. body `user` + hash of the first user message - `user` is usually per *user*, not per chat,
                                                   so the first message tells conversations apart
  4. none -> a fresh thread per request (the client's history is used as is)

Route/tool steps are shown according to AGENTS_STREAM_STEPS:
  reasoning (default)  as `reasoning_content` deltas (Open WebUI shows them as a collapsible "thinking")
  prefix               as short "> ..." lines before the answer
  off                  not at all
Non-streaming responses also carry an `agent` object (route, confidence, fallback, tools, thread_id).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from . import __version__, tracing
from .config import settings
from .graph import Deps, open_graph, stream_turn

MODEL_ID = "agent"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{"type": "text", "text": ...}, {"type": "image_url", ...}]
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def to_messages(raw: list[dict]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in raw:
        role, text = m.get("role"), _text(m.get("content"))
        if role == "user":
            out.append(HumanMessage(text))
        elif role == "assistant" and text:
            out.append(AIMessage(text))
        elif role in ("system", "developer"):
            out.append(SystemMessage(text))
        # tool / function messages from the client are ignored: the agent runs its own tools
    return out


def thread_id_for(request: Request, body: dict, messages: list[BaseMessage]) -> str | None:
    h = request.headers
    if tid := h.get("x-thread-id") or h.get("x-openwebui-chat-id"):
        return tid
    if user := body.get("user"):
        first = next((m.content for m in messages if isinstance(m, HumanMessage)), "")
        return f"{user}:{hashlib.sha1(str(first).encode()).hexdigest()[:12]}"
    return None


def create_app(deps: Deps | None = None, checkpoint_db: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tracing.setup()
        async with open_graph(deps, checkpoint_db) as graph:
            app.state.graph = graph
            yield
        tracing.shutdown()

    app = FastAPI(title="stack-agents", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        s = settings()
        return {"status": "ok", "model": MODEL_ID, "llm": s.llm_base_url, "chat_model": s.chat_model,
                "task_model": s.task_model, "decide_url": s.decide_url}

    @app.get("/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "stack-agents"}]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        msgs = to_messages(body.get("messages") or [])
        if not any(isinstance(m, HumanMessage) for m in msgs):
            return JSONResponse({"error": {"message": "messages must contain a user message", "type": "invalid_request_error"}}, 400)
        tid = thread_id_for(request, body, msgs)
        cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
        mode = settings().stream_steps
        events = stream_turn(app.state.graph, msgs, tid)

        if not body.get("stream"):
            steps, res = [], None
            async for kind, val in events:
                if kind == "step":
                    steps.append(val)
                elif kind == "done":
                    res = val
            answer = res.answer or (f"Error: {res.error}" if res.error else "")
            if mode == "prefix" and steps:
                answer = "".join(f"> {s}\n" for s in steps) + "\n" + answer
            message: dict = {"role": "assistant", "content": answer}
            if mode == "reasoning" and steps:
                message["reasoning_content"] = "\n".join(steps)
            return {
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "agent": {"route": res.route, "confidence": res.confidence, "fallback": res.fallback,
                          "tools": [t["name"] for t in res.tools], "thread_id": res.thread_id,
                          "trace_id": res.trace_id, "error": res.error},
            }

        def chunk(delta: dict, finish: str | None = None) -> str:
            data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def sse():
            yield chunk({"role": "assistant", "content": ""})
            sent = False
            async for kind, val in events:
                if kind == "step" and mode == "reasoning":
                    yield chunk({"reasoning_content": val + "\n"})
                elif kind == "step" and mode == "prefix":
                    yield chunk({"content": f"> {val}\n"})
                elif kind == "token":
                    if not sent and mode == "prefix":
                        yield chunk({"content": "\n"})
                    sent = True
                    yield chunk({"content": val})
                elif kind == "done":
                    if not sent:  # model did not stream (or failed): send the whole answer
                        yield chunk({"content": val.answer or (f"Error: {val.error}" if val.error else "")})
                    yield chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port or settings().port, log_level="info")
