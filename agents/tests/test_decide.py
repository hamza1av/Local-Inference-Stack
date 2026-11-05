import json

import httpx
import pytest

from stack_agents.decide import (
    ChoiceAnswer,
    DecisionClient,
    DecisionError,
    NoulAnswer,
    ScoreAnswer,
    choice,
    score,
    yes_no,
)

from .conftest import fake_chat

QUESTIONS = {
    "topic": choice("What is it about?", {"praise": "compliment", "question": None, "complaint": "a problem"}),
    "recommend": yes_no("Would they recommend it?"),
    "satisfaction": score("How satisfied?", ["Unhappy", "Neutral", "Happy", "Delighted"]),
}

REAL = {
    "model": "d3-flash",
    "answers": {
        "topic": {"type": "choice", "choice": "praise", "probabilities": {"praise": 0.99, "question": 0.005, "complaint": 0.005}, "confidence": 0.99},
        "recommend": {"type": "noul", "noul": 0.97},
        "satisfaction": {"type": "score", "score": 2.9, "probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9},
                         "confidence": 0.8, "legend": {"0": "Unhappy", "1": "Neutral", "2": "Happy", "3": "Delighted"}},
    },
    "usage": {"input_tokens": 300, "output_tokens": 0},
}


def transport(status: int, body: dict | None = None, seen: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, json=body or {"error": {"message": "decision model not running"}})

    return httpx.MockTransport(handler)


def fallback_llm():
    # one reply per question, in question order (choice, noul, score)
    return fake_chat(
        '{"choice": "complaint", "confidence": 0.8}',
        '<think>hmm</think> {"answer": "no", "confidence": 0.9}',
        '```json\n{"level": 0, "confidence": 0.6}\n```',
    )


async def test_real_api_path_and_payload():
    seen: list[httpx.Request] = []
    c = DecisionClient("http://x/v1/decisions", "k", transport=transport(200, REAL, seen), task_llm=fake_chat())
    d = await c.adecide("great!", QUESTIONS, priority="background")
    assert not d.fallback and d.model == "d3-flash"
    assert isinstance(d.answers["topic"], ChoiceAnswer) and d.answers["topic"].choice == "praise"
    assert d.answers["recommend"].yes
    assert d.answers["satisfaction"].level == "Delighted"
    body = json.loads(seen[0].content)
    assert body["questions"]["topic"]["criteria"]["question"] is None
    assert body["questions"]["satisfaction"]["criteria"][0] == "Unhappy"
    assert "criteria" not in body["questions"]["recommend"]
    assert seen[0].headers["authorization"] == "Bearer k"
    assert seen[0].headers["x-stack-priority"] == "background"
    api = d.to_api()
    assert "fallback" not in api and api["usage"]["input_tokens"] == 300


@pytest.mark.parametrize("status", [503, 502, 404])
async def test_fallback_on_unavailable(status):
    c = DecisionClient("http://x/decisions", "k", transport=transport(status), task_llm=fallback_llm())
    d = await c.adecide("The battery died after a day.", QUESTIONS)
    assert d.fallback and d.model.startswith("fallback:")
    t, r, s = d.answers["topic"], d.answers["recommend"], d.answers["satisfaction"]
    assert isinstance(t, ChoiceAnswer) and t.choice == "complaint" and t.confidence == pytest.approx(0.8)
    assert sum(t.probabilities.values()) == pytest.approx(1.0) and set(t.probabilities) == {"praise", "question", "complaint"}
    assert isinstance(r, NoulAnswer) and r.noul == pytest.approx(0.1) and not r.yes
    assert isinstance(s, ScoreAnswer) and s.level == "Unhappy"
    assert sum(s.probabilities.values()) == pytest.approx(1.0)
    assert s.score == pytest.approx(0.4)  # 0.6 on level 0, 0.4 on its only neighbour 1
    assert s.legend["3"] == "Delighted"
    api = d.to_api()
    assert api["fallback"] is True and api["answers"]["recommend"] == {"type": "noul", "noul": pytest.approx(0.1)}


def test_fallback_when_unreachable_sync():
    def boom(req):
        raise httpx.ConnectError("refused")

    c = DecisionClient("http://x/decisions", "k", transport=httpx.MockTransport(boom), task_llm=fallback_llm())
    d = c.decide("The battery died after a day.", QUESTIONS)
    assert d.fallback and d.answers["topic"].choice == "complaint"


async def test_fallback_retries_invalid_then_gives_zero_confidence():
    llm = fake_chat('{"choice": "banana"}', "no json here")
    c = DecisionClient("http://x", "k", transport=transport(503), task_llm=llm)
    d = await c.adecide("hi", {"r": choice("route?", ["chat", "data"])})
    a = d.answers["r"]
    assert a.confidence == 0.0 and a.choice == "chat"  # safe default: first option, zero confidence
    assert len(llm.seen) == 2  # one retry with the validation error fed back
    assert "Invalid" in llm.seen[1][-1].content


async def test_fallback_retry_succeeds_and_accepts_case_and_percent():
    llm = fake_chat("sorry", '{"choice": "DATA", "confidence": 85}')
    c = DecisionClient("http://x", "k", transport=transport(503), task_llm=llm)
    d = await c.adecide("hi", {"r": choice("route?", ["chat", "data"])})
    assert d.answers["r"].choice == "data" and d.answers["r"].confidence == pytest.approx(0.85)


async def test_score_accepts_level_name():
    c = DecisionClient("http://x", "k", transport=transport(503), task_llm=fake_chat('{"level": "Happy", "confidence": 1}'))
    d = await c.adecide("ok", {"s": score("?", ["Unhappy", "Neutral", "Happy"])})
    assert d.answers["s"].score == pytest.approx(2.0) and d.answers["s"].level == "Happy"


async def test_bad_request_is_not_masked():
    c = DecisionClient("http://x", "k", transport=transport(400, {"error": {"param": "questions.x"}}), task_llm=fake_chat())
    with pytest.raises(DecisionError):
        await c.adecide("x", QUESTIONS)


def test_question_validation():
    with pytest.raises(ValueError):
        DecisionClient("http://x", "k", task_llm=fake_chat()).decide("x", {"s": score("?", ["only one"])})
    with pytest.raises(ValueError):
        DecisionClient("http://x", "k", task_llm=fake_chat()).decide("x", {"c": {"type": "choice", "criteria": {}}})
