"""The data plane: `POST /decide`, `GET /info`, `GET /health`, beside `/metrics`.

The reply is the shape `services/dita-orchestrator/decisions/worker.go` parses; the contract
is in docs/inferences/system-one/[1]technical-requirement.md. A request is refused whole or
answered whole: an answer that skipped a question would be stored as a prediction.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Mapping, Tuple

from textinfer import Admission, TeiError, method_not_allowed, unhealthy
from worker import Response, Route, json_response

from .engines.decision import Decision, DeadlineExceeded, TooMuchWork

MAX_QUESTIONS = 20
MIN_OPTIONS = 2
MAX_OPTIONS = 20
# One lock serialises the model, so a second slot would only be a queue: refuse it instead.
MAX_CONCURRENT_REQUESTS = 1
# The orchestrator gives up at 120 s (INFERENCES_TIMEOUT); stop well before it does.
DEADLINE_S = 100.0
# Measured 169.5 s for 20 questions x 1024 tokens (20480 padded tokens, 8.3 ms a token, host
# load 13.8). 8192 tokens is about 68 s at that rate, inside DEADLINE_S with room for a slower
# moment; the deadline check stops whatever still overruns.
MAX_PLANNED_TOKENS = 8192
TYPES = ("choice", "score", "noul")
NOUL_OPTIONS = ("false", "true")
FIELDS = frozenset({"text", "questions"})
QUESTION_FIELDS = frozenset({"name", "type", "options", "criteria", "range"})


class Refusal(Exception):
    """A request this worker will not run. The orchestrator counts 400 as `schema_invalid`.

    `path` names the fault in zod's dotted style (`questions.1.options`). It and `error_type` are
    the contract the orchestrator's CheckQuestions matches through specs/decisions/worker-cases.json;
    the message is prose for people."""

    def __init__(self, message: str, status: int = 400, error_type: str = "Validation",
                 path: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.path = path

    def response(self) -> Response:
        body = {"error": str(self), "error_type": self.error_type}
        if self.path is not None:
            body["path"] = self.path
        return json_response(self.status, body)


def routes(max_concurrent: int = MAX_CONCURRENT_REQUESTS, deadline_s: float = DEADLINE_S,
           max_planned_tokens: int = MAX_PLANNED_TOKENS) -> Mapping[str, Route]:
    admission = Admission(max_concurrent)

    def decide(manager: Any, method: str, body: bytes) -> Response:
        if method != "POST":
            return method_not_allowed("POST")
        deadline = time.monotonic() + deadline_s

        def work(engine: Any) -> Any:
            # Returned, not raised: Admission reports anything it catches as a Backend failure.
            try:
                return engine.decide(text, internal, deadline, max_planned_tokens)
            except (TooMuchWork, DeadlineExceeded) as exc:
                return exc

        try:
            text, questions = parse_decide(body)
            internal = [to_model(q) for q in questions]
            model_id, decisions, infer_ms = admission.run(manager, work)
        except Refusal as exc:
            return exc.response()
        except TeiError as exc:
            return exc.response()
        if isinstance(decisions, TooMuchWork):
            return Refusal(str(decisions), 413, "Validation").response()
        if isinstance(decisions, DeadlineExceeded):
            return Refusal(str(decisions), 504, "Timeout").response()
        spec = manager.registry.get(model_id)
        return json_response(200, reply(model_id, revision(spec), questions, decisions),
                             headers=(("x-model-id", model_id), ("x-compute-time", str(infer_ms))))

    def info(manager: Any, method: str, body: bytes) -> Response:
        if method != "GET":
            return method_not_allowed("GET")
        resident = manager.resident()
        if resident is None:
            return unhealthy()
        spec = manager.registry.get(resident["id"])
        return json_response(200, {"model_id": spec.id, "model_revision": revision(spec), "engine": spec.engine,
                                   "max_concurrent_requests": max_concurrent, "max_questions": MAX_QUESTIONS,
                                   "max_planned_tokens": max_planned_tokens, "deadline_s": deadline_s})

    def health(manager: Any, method: str, body: bytes) -> Response:
        if method != "GET":
            return method_not_allowed("GET")
        return Response(200, b"") if manager.resident() is not None else unhealthy()

    return {"/decide": decide, "/info": info, "/health": health}


def revision(spec: Any) -> str:
    return spec.files[0].revision if spec.files else ""


def parse_decide(body: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"the body is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise Refusal("the body must be a JSON object")
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise Refusal(f"unknown field(s): {', '.join(unknown)}")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refusal("`text` must be a non-blank string")
    questions = raw.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise Refusal(f"`questions` must be a list of 1 to {MAX_QUESTIONS} questions", path="questions")
    seen = set()
    for index, q in enumerate(questions):
        _check_question(index, q)
        if q["name"] in seen:
            raise Refusal(f"question {q['name']!r} appears twice", path=f"questions.{index}.name")
        seen.add(q["name"])
    return text, questions


def _check_question(index: int, q: Any) -> None:
    at, path = f"questions[{index}]", f"questions.{index}"
    if not isinstance(q, dict):
        raise Refusal(f"{at} must be an object", path=path)
    unknown = sorted(set(q) - QUESTION_FIELDS)
    if unknown:
        raise Refusal(f"{at} has unknown field(s): {', '.join(unknown)}", path=path)
    if not isinstance(q.get("name"), str) or not q["name"]:
        raise Refusal(f"{at}.name must be a non-empty string", path=f"{path}.name")
    if q.get("type") not in TYPES:
        raise Refusal(f"{at}.type must be one of {', '.join(TYPES)}", path=f"{path}.type")
    options = q.get("options")
    if (not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS
            or not all(isinstance(o, str) and o.strip() for o in options)):
        raise Refusal(f"{at}.options must be {MIN_OPTIONS} to {MAX_OPTIONS} non-blank strings", path=f"{path}.options")
    if len(set(options)) != len(options):
        raise Refusal(f"{at}.options repeats an option", path=f"{path}.options")
    if q["type"] == "noul" and sorted(options) != list(NOUL_OPTIONS):
        raise Refusal(f"{at} is a noul: the model answers it only as `false` and `true`, "
                      f"so its options must be exactly those, not {options}", path=f"{path}.options")
    if "criteria" in q and not isinstance(q["criteria"], str):
        raise Refusal(f"{at}.criteria must be a string", path=f"{path}.criteria")
    if q.get("range") is not None:
        _check_range(at, f"{path}.range", q)


def _check_range(at: str, path: str, q: Dict[str, Any]) -> None:
    """The model reads a score's options as its levels in order and never sees the range, so a
    range is accepted and ignored. It is refused only when it contradicts its own options."""
    if q["type"] != "score":
        raise Refusal(f"{at}.range belongs only to a score question", path=path)
    bounds = q["range"]
    if not isinstance(bounds, dict) or not all(
            isinstance(bounds.get(k), (int, float)) and not isinstance(bounds.get(k), bool) for k in ("min", "max")):
        raise Refusal(f"{at}.range must be {{min, max}} numbers", path=path)
    low, high = bounds["min"], bounds["max"]
    if not low < high:
        raise Refusal(f"{at}.range min must be below max", path=path)
    try:
        levels = [float(o) for o in q["options"]]
    except ValueError:
        return
    if levels != sorted(levels) or levels[0] < low or levels[-1] > high:
        raise Refusal(f"{at}.range {low}..{high} contradicts its options {q['options']}: numeric levels must "
                      "rise within the range", path=path)


def to_model(q: Mapping[str, Any]) -> Dict[str, Any]:
    """A contract question as the model reads it. With no criteria the question's name is
    the only text that says what is asked, so it stands in as the instructions."""
    t = q["type"]
    ins = q.get("criteria") or q["name"].replace("_", " ")
    if t == "choice":
        crit: Any = {o: None for o in q["options"]}
    elif t == "score":
        crit = list(q["options"])
    else:
        crit = None
    return {"t": t, "ins": ins, "crit": crit}


def labels(q: Mapping[str, Any]) -> List[str]:
    """The option each probability belongs to, in the model's order."""
    return list(NOUL_OPTIONS) if q["type"] == "noul" else list(q["options"])


def reply(model_id: str, model_revision: str, questions: List[Dict[str, Any]],
          decisions: List[Decision]) -> Dict[str, Any]:
    answers = []
    for q, d in zip(questions, decisions, strict=True):
        probabilities = {label: float(p) for label, p in zip(labels(q), d.probabilities, strict=True)}
        answers.append({"name": q["name"], "probabilities": probabilities,
                        "confidence": d.confidence, "act_probability": d.act_probability})
    return {"model_id": model_id, "model_revision": model_revision, "answers": answers}
