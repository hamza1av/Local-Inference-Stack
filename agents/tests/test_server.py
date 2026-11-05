import json

from fastapi.testclient import TestClient

from stack_agents.server import create_app

from .conftest import fake_chat, tool_call
from .test_graph import deps


def client(llm, route="chat", tmp_path=None):
    return TestClient(create_app(deps(llm, route), str(tmp_path / "agents.db")))


def test_health_and_models(tmp_path):
    with client(fake_chat(), tmp_path=tmp_path) as c:
        assert c.get("/health").json()["status"] == "ok"
        m = c.get("/v1/models").json()
        assert m["object"] == "list" and m["data"][0]["id"] == "agent"


def test_non_stream_shape(tmp_path, stack_db):
    llm = fake_chat(tool_call("sql_query", {"query": "SELECT count(*) FROM gw_requests"}), "3 requests.")
    with client(llm, "data", tmp_path) as c:
        r = c.post("/v1/chat/completions", json={
            "model": "agent", "messages": [{"role": "system", "content": "be nice"},
                                           {"role": "user", "content": [{"type": "text", "text": "how many requests?"}]}]})
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "chat.completion" and j["model"] == "agent"
    msg = j["choices"][0]["message"]
    assert msg["role"] == "assistant" and msg["content"] == "3 requests." and j["choices"][0]["finish_reason"] == "stop"
    assert "route data" in msg["reasoning_content"] and "tool sql_query" in msg["reasoning_content"]
    assert j["agent"]["route"] == "data" and j["agent"]["tools"] == ["sql_query"]


def test_stream_sse(tmp_path):
    with client(fake_chat("Hello there friend"), tmp_path=tmp_path) as c:
        r = c.post("/v1/chat/completions", headers={"x-thread-id": "abc"},
                   json={"model": "agent", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    lines = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(l) for l in lines[:-1]]
    assert all(ch["object"] == "chat.completion.chunk" for ch in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    content = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)
    reasoning = "".join(ch["choices"][0]["delta"].get("reasoning_content", "") for ch in chunks)
    assert content == "Hello there friend" and reasoning.startswith("route chat")
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_thread_from_user_field_separates_conversations(tmp_path):
    llm = fake_chat("A1", "A2", "B1")
    with client(llm, tmp_path=tmp_path) as c:
        def ask(msgs):
            return c.post("/v1/chat/completions", json={"model": "agent", "user": "u1", "messages": msgs}).json()["agent"]["thread_id"]

        t1 = ask([{"role": "user", "content": "first chat"}])
        t1b = ask([{"role": "user", "content": "first chat"}, {"role": "assistant", "content": "A1"},
                   {"role": "user", "content": "more"}])
        t2 = ask([{"role": "user", "content": "second chat"}])
    assert t1 == t1b and t1 != t2 and t1.startswith("u1:")
    # the second turn of chat 1 saw its history, chat 2 did not
    assert [m.content for m in llm.seen[1][1:]] == ["first chat", "A1", "more"]
    assert [m.content for m in llm.seen[2][1:]] == ["second chat"]


def test_requires_user_message(tmp_path):
    with client(fake_chat(), tmp_path=tmp_path) as c:
        assert c.post("/v1/chat/completions", json={"messages": []}).status_code == 400
