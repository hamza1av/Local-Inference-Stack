"""Typed client for the decision API ("System One", uzu decision models) with an LLM fallback.

A decision model does not generate text: it reads a `state` and answers *typed questions* with
calibrated probabilities in a single forward pass. That makes it ideal for routing, gating and
grading inside agents. The gateway forwards `POST $GATEWAY_URL/decisions` to uzu :8002, which
is **off by default** on a 16 GB machine; the gateway then answers 503.

So `decide()` always works:

* decision model up      -> real answer (`fallback` = False)
* 503 / 502 / 504 / 404,
  connection refused,
  timeout                -> the `task` model emulates each question with a strict-JSON prompt,
                            the result is validated and returned in the *same shape*,
                            plus `"fallback": true`.
* 400 (bad question)     -> `DecisionError` (a bug in the caller, never masked)

Question types (see uzu/README.md, "Decision models"):

    choice  criteria = {option: description | None}   (1..255 options)
            -> {"type":"choice","choice":name,"probabilities":{...},"confidence":x}
    noul    criteria = {"false": "...", "true": "..."} (optional)
            -> {"type":"noul","noul": p_yes}
    score   criteria = [level_low, ..., level_high]   (2..10 levels)
            -> {"type":"score","score": expected_index,"probabilities":{"0":..},
                "confidence":x,"legend":{"0": level, ...}}

Usage:

    from stack_agents.decide import DecisionClient, choice, yes_no, score
    d = await DecisionClient().adecide("I want a refund!", {
        "topic": choice("What is this about?", {"refund": None, "question": None}),
        "angry": yes_no("Is the customer angry?"),
    })
    d.answers["topic"].choice, d.answers["angry"].yes, d.fallback
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal, Union

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .config import settings

log = logging.getLogger(__name__)

#: HTTP statuses that mean "decision model not available" -> use the fallback.
UNAVAILABLE = {404, 502, 503, 504}


class DecisionError(RuntimeError):
    """The decision API rejected the request (e.g. 400 invalid question)."""


# --------------------------------------------------------------------------- questions


class Question(BaseModel):
    type: Literal["choice", "noul", "score"]
    instructions: str | None = None
    criteria: dict[str, str | None] | list[str] | None = None

    def validate_shape(self, name: str) -> None:
        c = self.criteria
        if self.type == "choice":
            if not isinstance(c, dict) or not 1 <= len(c) <= 255:
                raise ValueError(f"questions.{name}: choice needs 1..255 options")
        elif self.type == "score":
            if not isinstance(c, list) or not 2 <= len(c) <= 10 or len(set(c)) != len(c):
                raise ValueError(f"questions.{name}: score needs 2..10 distinct levels")
        elif self.type == "noul" and c is not None:
            if not isinstance(c, dict) or not set(c) <= {"false", "true"}:
                raise ValueError(f"questions.{name}: noul criteria may only have 'false'/'true'")


def choice(instructions: str, options: dict[str, str | None] | list[str]) -> Question:
    """Pick one of `options` (names -> optional descriptions)."""
    if isinstance(options, list):
        options = {o: None for o in options}
    return Question(type="choice", instructions=instructions, criteria=options)


def yes_no(instructions: str, *, false: str | None = None, true: str | None = None) -> Question:
    """Yes/no question ("noul"); the answer is P(yes)."""
    crit = {k: v for k, v in (("false", false), ("true", true)) if v}
    return Question(type="noul", instructions=instructions, criteria=crit or None)


def score(instructions: str, levels: list[str]) -> Question:
    """Rate on an ordered scale `levels` (low -> high, 2..10 entries)."""
    return Question(type="score", instructions=instructions, criteria=list(levels))


# --------------------------------------------------------------------------- answers


class _Answer(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChoiceAnswer(_Answer):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float] = {}
    confidence: float = 0.0


class NoulAnswer(_Answer):
    type: Literal["noul"] = "noul"
    noul: float  # P(yes)

    @property
    def yes(self) -> bool:
        return self.noul >= 0.5


class ScoreAnswer(_Answer):
    type: Literal["score"] = "score"
    score: float  # expected level index
    probabilities: dict[str, float] = {}
    confidence: float = 0.0
    legend: dict[str, str] = {}

    @property
    def level(self) -> str:
        """Name of the most likely level."""
        if self.probabilities:
            best = max(self.probabilities, key=self.probabilities.__getitem__)
            return self.legend.get(best, best)
        return self.legend.get(str(round(self.score)), str(round(self.score)))


Answer = Annotated[Union[ChoiceAnswer, NoulAnswer, ScoreAnswer], Field(discriminator="type")]


class Decision(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    answers: dict[str, Answer]
    fallback: bool = False

    def to_api(self) -> dict[str, Any]:
        """The wire shape (`fallback` only present when true, like the contract says)."""
        d = self.model_dump(exclude_none=True)
        if not self.fallback:
            d.pop("fallback", None)
        return d


_decision = TypeAdapter(Decision)


# --------------------------------------------------------------------------- fallback (task model)

_SYSTEM = "You are a precise classifier. Reply with one JSON object only. No prose, no code fences."
_THINK = re.compile(r"<think>.*?</think>", re.S)
_PAIR = re.compile(r'"(\w+)"\s*:\s*(?:"([^"]*)"|<?\s*([-\w.]+))')


def _prompt(state: str, q: Question) -> str:
    head = f"Text:\n<<<\n{state.strip()}\n>>>\n\nQuestion: {q.instructions or ''}".rstrip()
    if q.type == "choice":
        opts = "\n".join(f"- {k}" + (f": {v}" if v else "") for k, v in q.criteria.items())  # type: ignore[union-attr]
        return (
            f"{head}\nOptions:\n{opts}\n\n"
            'Reply with JSON like {"choice": "OPTION", "confidence": 0.8} where OPTION is exactly one '
            "option name and confidence (0 to 1) is how sure you are."
        )
    if q.type == "noul":
        hint = ""
        if q.criteria:
            hint = "".join(f"\n{'yes' if k == 'true' else 'no'} means: {v}" for k, v in q.criteria.items())  # type: ignore[union-attr]
        return (
            f'{head}{hint}\n\nReply with JSON like {{"answer": "yes", "confidence": 0.8}} where answer is '
            '"yes" or "no" and confidence (0 to 1) is how sure you are.'
        )
    levels = "\n".join(f"- {lvl}" for lvl in q.criteria)  # type: ignore[union-attr]
    return (
        f"{head}\nLevels (low to high):\n{levels}\n\n"
        'Reply with JSON like {"level": "LEVEL", "confidence": 0.8} where LEVEL is exactly one level '
        "name and confidence (0 to 1) is how sure you are."
    )


def _extract_json(text: str) -> dict:
    text = _THINK.sub("", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        # small models often echo the template ("confidence": <0.8>) - salvage the key/value pairs
        obj = {k: (v if v != "" else n) for k, v, n in _PAIR.findall(text[start : end + 1])}
        if not obj:
            raise ValueError("reply is not valid JSON") from None
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def _conf(obj: dict, default: float = 0.7) -> float:
    try:
        c = float(obj.get("confidence", default))
    except (TypeError, ValueError):
        c = default
    if c > 1.0 and c <= 100.0:  # "85" -> 0.85
        c /= 100.0
    return min(max(c, 0.0), 1.0)


def _parse(q: Question, text: str) -> _Answer:
    """Validate the model's JSON and turn it into a decision-API answer. Raises ValueError."""
    obj = _extract_json(text)
    c = _conf(obj)
    if q.type == "choice":
        names = list(q.criteria)  # type: ignore[arg-type]
        raw = str(obj.get("choice", "")).strip().strip("\"'`").lower()
        match = next((n for n in names if n.lower() == raw), None)
        if match is None:
            raise ValueError(f"choice must be one of {names}, got {obj.get('choice')!r}")
        rest = (1.0 - c) / (len(names) - 1) if len(names) > 1 else 0.0
        probs = {n: (c if n == match else rest) for n in names} if len(names) > 1 else {match: 1.0}
        return ChoiceAnswer(choice=match, probabilities=probs, confidence=probs[match])
    if q.type == "noul":
        ans = obj.get("answer", obj.get("noul"))
        if isinstance(ans, str):
            ans = ans.strip().lower()
            if ans not in ("yes", "no", "true", "false"):
                raise ValueError(f"answer must be yes/no, got {ans!r}")
            ans = ans in ("yes", "true")
        if not isinstance(ans, bool):
            raise ValueError("answer must be yes/no")
        return NoulAnswer(noul=c if ans else 1.0 - c)
    # score
    levels: list[str] = q.criteria  # type: ignore[assignment]
    lv = obj.get("level")
    if isinstance(lv, str) and not lv.strip().isdigit():
        idx = next((i for i, name in enumerate(levels) if name.lower() == lv.strip().lower()), None)
    else:
        try:
            idx = int(lv)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            idx = None
    if idx is None or not 0 <= idx < len(levels):
        raise ValueError(f"level must be 0..{len(levels) - 1}")
    # put `c` on the chosen level, spread the rest over its neighbours (keeps the mean close)
    probs = [0.0] * len(levels)
    probs[idx] = c
    nbrs = [i for i in (idx - 1, idx + 1) if 0 <= i < len(levels)]
    for i in nbrs:
        probs[i] += (1.0 - c) / len(nbrs)
    return ScoreAnswer(
        score=sum(i * p for i, p in enumerate(probs)),
        probabilities={str(i): p for i, p in enumerate(probs)},
        confidence=c,
        legend={str(i): name for i, name in enumerate(levels)},
    )


def _worst_case(q: Question) -> _Answer:
    """Answer used when even the fallback fails: zero confidence, so callers take the safe path."""
    if q.type == "choice":
        names = list(q.criteria)  # type: ignore[arg-type]
        return ChoiceAnswer(choice=names[0], probabilities={n: 1 / len(names) for n in names}, confidence=0.0)
    if q.type == "noul":
        return NoulAnswer(noul=0.5)
    levels: list[str] = q.criteria  # type: ignore[assignment]
    n = len(levels)
    return ScoreAnswer(
        score=(n - 1) / 2,
        probabilities={str(i): 1 / n for i in range(n)},
        confidence=0.0,
        legend={str(i): name for i, name in enumerate(levels)},
    )


# --------------------------------------------------------------------------- client


def _normalize(questions: dict[str, Question | dict]) -> dict[str, Question]:
    out = {}
    for name, q in questions.items():
        q = q if isinstance(q, Question) else Question.model_validate(q)
        q.validate_shape(name)
        out[name] = q
    return out


class DecisionClient:
    """Decision API client. `task_llm` is any LangChain chat model (the fallback)."""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        task_llm: BaseChatModel | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        retries: int = 1,
    ):
        s = settings()
        self.url = url or s.decide_url
        self.api_key = api_key if api_key is not None else s.llm_api_key
        self.timeout = timeout or s.decide_timeout
        self.transport = transport
        self.retries = retries
        self._task_llm = task_llm

    @property
    def task_llm(self) -> BaseChatModel:
        if self._task_llm is None:
            from .llm import task_model

            self._task_llm = task_model()
        return self._task_llm

    def _headers(self, priority: str | None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if priority:
            h["x-stack-priority"] = priority
        return h

    def _payload(self, state: str, qs: dict[str, Question]) -> dict:
        return {"state": state, "questions": {k: q.model_dump(exclude_none=True) for k, q in qs.items()}}

    def _handle(self, resp: httpx.Response) -> Decision | None:
        """Decision on success, None if unavailable (-> fallback), raises on client errors."""
        if resp.status_code in UNAVAILABLE:
            log.info("decision API unavailable (%s), using task-model fallback", resp.status_code)
            return None
        if resp.status_code >= 400:
            raise DecisionError(f"decision API {resp.status_code}: {resp.text[:300]}")
        return _decision.validate_python(resp.json())

    # ---- async

    async def adecide(self, state: str, questions: dict[str, Question | dict], *, priority: str | None = None) -> Decision:
        qs = _normalize(questions)
        try:
            kw = {"transport": self.transport} if self.transport else {}
            async with httpx.AsyncClient(timeout=self.timeout, **kw) as http:
                resp = await http.post(self.url, json=self._payload(state, qs), headers=self._headers(priority))
            if (d := self._handle(resp)) is not None:
                return d
        except (httpx.TransportError, httpx.TimeoutException) as e:
            log.info("decision API unreachable (%s), using task-model fallback", type(e).__name__)
        # sequential on purpose: local model servers (uzu) serve one request at a time
        answers = [await self._afallback(state, q) for q in qs.values()]
        return self._fallback_result(qs, answers)

    async def _afallback(self, state: str, q: Question) -> _Answer:
        msgs = [SystemMessage(_SYSTEM), HumanMessage(_prompt(state, q))]
        for _ in range(self.retries + 1):
            try:
                reply = await self.task_llm.ainvoke(msgs)
            except Exception as e:  # noqa: BLE001 - the fallback must never take the agent down
                log.warning("decision fallback failed: %s", e)
                break
            try:
                return _parse(q, str(reply.content))
            except ValueError as e:  # invalid JSON / invalid value -> tell the model, retry once
                msgs = [*msgs[:2], reply, HumanMessage(f"Invalid: {e}. Reply with the JSON object only.")]
        return _worst_case(q)

    # ---- sync (scripts, examples)

    def decide(self, state: str, questions: dict[str, Question | dict], *, priority: str | None = None) -> Decision:
        qs = _normalize(questions)
        try:
            kw = {"transport": self.transport} if self.transport else {}
            with httpx.Client(timeout=self.timeout, **kw) as http:
                resp = http.post(self.url, json=self._payload(state, qs), headers=self._headers(priority))
            if (d := self._handle(resp)) is not None:
                return d
        except (httpx.TransportError, httpx.TimeoutException) as e:
            log.info("decision API unreachable (%s), using task-model fallback", type(e).__name__)
        return self._fallback_result(qs, [self._fallback(state, q) for q in qs.values()])

    def _fallback(self, state: str, q: Question) -> _Answer:
        msgs = [SystemMessage(_SYSTEM), HumanMessage(_prompt(state, q))]
        for _ in range(self.retries + 1):
            try:
                reply = self.task_llm.invoke(msgs)
            except Exception as e:  # noqa: BLE001
                log.warning("decision fallback failed: %s", e)
                break
            try:
                return _parse(q, str(reply.content))
            except ValueError as e:
                msgs = [*msgs[:2], reply, HumanMessage(f"Invalid: {e}. Reply with the JSON object only.")]
        return _worst_case(q)

    def _fallback_result(self, qs: dict[str, Question], answers: list[_Answer]) -> Decision:
        name = getattr(self.task_llm, "model_name", None) or getattr(self.task_llm, "model", None) or "task"
        return Decision(model=f"fallback:{name}", answers=dict(zip(qs, answers)), fallback=True)


async def adecide(state: str, questions: dict[str, Question | dict], **kw: Any) -> Decision:
    """Shortcut with a default client."""
    return await DecisionClient().adecide(state, questions, **kw)


def decide(state: str, questions: dict[str, Question | dict], **kw: Any) -> Decision:
    return DecisionClient().decide(state, questions, **kw)
