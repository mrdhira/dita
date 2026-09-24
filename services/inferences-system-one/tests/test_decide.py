"""The contract's pure parts: what /decide refuses, how a contract question reaches the model,
and the reply the orchestrator's `ParseReply` reads."""

from __future__ import annotations

import json
import unittest

import numpy as np

from system_one_worker.decide import Refusal, parse_decide, reply, to_model
from system_one_worker.engines.decision import Decision, confidence_from_probs

SEVERITY = {"name": "severity", "type": "choice", "options": ["info", "warning", "critical"]}
HUMAN = {"name": "needs_human", "type": "noul", "options": ["false", "true"]}
SCORE = {"name": "risk_score", "type": "score", "options": ["1", "2", "3", "4", "5"], "range": {"min": 1, "max": 5}}


def body(**fields) -> bytes:
    return json.dumps({"text": "oom", "questions": [SEVERITY], **fields}).encode()


class ParseTest(unittest.TestCase):
    def test_what_is_refused_before_the_model_is_touched(self) -> None:
        q = dict(SEVERITY)
        cases = [
            ("not JSON", b"{", "not JSON"),
            ("not an object", b"[]", "JSON object"),
            ("an unknown field", body(model="x"), "unknown field"),
            ("blank text", body(text="  "), "`text`"),
            ("text not a string", body(text=3), "`text`"),
            ("no questions", body(questions=[]), "1 to 20"),
            ("too many questions", body(questions=[{**q, "name": f"q{i}"} for i in range(21)]), "1 to 20"),
            ("a question not an object", body(questions=["x"]), "must be an object"),
            ("an unknown question field", body(questions=[{**q, "hint": "x"}]), "unknown field"),
            ("a nameless question", body(questions=[{**q, "name": ""}]), ".name"),
            ("an unknown type", body(questions=[{**q, "type": "rank"}]), ".type"),
            ("one option", body(questions=[{**q, "options": ["a"]}]), ".options"),
            ("a blank option", body(questions=[{**q, "options": ["a", " "]}]), ".options"),
            ("a repeated option", body(questions=[{**q, "options": ["a", "a"]}]), "repeats"),
            ("a question asked twice", body(questions=[q, q]), "appears twice"),
            ("a noul with its own labels", body(questions=[{**HUMAN, "options": ["yes", "no"]}]), "`false` and `true`"),
            ("criteria not a string", body(questions=[{**q, "criteria": ["x"]}]), "criteria"),
            ("a range on a choice", body(questions=[{**q, "range": {"min": 1, "max": 3}}]), "only to a score"),
            ("a range that is not two numbers", body(questions=[{**SCORE, "range": {"min": "1", "max": 5}}]),
             "min, max"),
            ("a range upside down", body(questions=[{**SCORE, "range": {"min": 5, "max": 1}}]), "below max"),
            ("levels outside their range", body(questions=[{**SCORE, "range": {"min": 1, "max": 3}}]),
             "contradicts its options"),
            ("levels out of order", body(questions=[{**SCORE, "options": ["1", "3", "2"]}]), "contradicts its options"),
        ]
        for name, raw, mentions in cases:
            with self.subTest(name), self.assertRaisesRegex(Refusal, mentions):
                parse_decide(raw)

    def test_a_score_range_is_accepted_whenever_it_agrees_with_its_options(self) -> None:
        cases = [
            ("the dashboard's own score", SCORE),
            ("integer and float bounds", {**SCORE, "range": {"min": 0.5, "max": 5}}),
            ("words cannot contradict a range", {**SCORE, "options": ["low", "high"], "range": {"min": 1, "max": 2}}),
            ("no range", {k: v for k, v in SCORE.items() if k != "range"}),
            ("a null range", {**SCORE, "range": None}),
        ]
        for name, question in cases:
            with self.subTest(name):
                _, questions = parse_decide(body(questions=[question]))
                self.assertEqual(to_model(questions[0])["crit"], question["options"], "the levels are the options")

    def test_the_orchestrators_own_request_is_accepted(self) -> None:
        score = {"name": "urgency", "type": "score", "options": ["1", "2", "3"], "criteria": "How urgent?"}
        text, questions = parse_decide(body(questions=[SEVERITY, {**HUMAN, "options": ["true", "false"]}, score]))
        self.assertEqual((text, [q["name"] for q in questions]), ("oom", ["severity", "needs_human", "urgency"]))

    def test_a_refusal_is_a_400_the_orchestrator_counts_as_schema_invalid(self) -> None:
        response = Refusal("nope").response()
        self.assertEqual((response.status, json.loads(response.body)),
                         (400, {"error": "nope", "error_type": "Validation"}))


class ModelInputTest(unittest.TestCase):
    def test_contract_questions_as_the_model_reads_them(self) -> None:
        cases = [
            ("choice options are its keys", SEVERITY,
             {"t": "choice", "ins": "severity", "crit": {"info": None, "warning": None, "critical": None}}),
            ("criteria are the instructions", {**SEVERITY, "criteria": "How bad is it?"},
             {"t": "choice", "ins": "How bad is it?", "crit": {"info": None, "warning": None, "critical": None}}),
            ("score options are its levels", {"name": "urgency", "type": "score", "options": ["low", "high"]},
             {"t": "score", "ins": "urgency", "crit": ["low", "high"]}),
            ("noul keeps the model's own false and true", HUMAN, {"t": "noul", "ins": "needs human", "crit": None}),
        ]
        for name, q, want in cases:
            with self.subTest(name):
                self.assertEqual(to_model(q), want)


class ReplyTest(unittest.TestCase):
    def test_the_reply_is_what_parse_reply_reads(self) -> None:
        decisions = [Decision(np.array([0.0112, 0.8507, 0.1381]), np.array([1400.0, -1600.0])),
                     Decision(np.array([0.9828, 0.0172]), np.array([0.0, np.log(3.0)]))]
        got = reply("laya-multilingual", "b4a904d", [SEVERITY, {**HUMAN, "options": ["true", "false"]}], decisions)
        self.assertEqual(got, {"model_id": "laya-multilingual", "model_revision": "b4a904d", "answers": [
            {"name": "severity", "probabilities": {"info": 0.0112, "warning": 0.8507, "critical": 0.1381},
             "confidence": confidence_from_probs(np.array([0.0112, 0.8507, 0.1381]), 3), "act_probability": 1.0},
            {"name": "needs_human", "probabilities": {"false": 0.9828, "true": 0.0172},
             "confidence": confidence_from_probs(np.array([0.9828, 0.0172]), 2), "act_probability": 0.25}]})
        self.assertAlmostEqual(got["answers"][0]["confidence"], 0.5801, places=3)
        self.assertIsInstance(got["answers"][0]["act_probability"], float)

    def test_a_decision_that_does_not_cover_the_question_is_an_error_not_a_partial_reply(self) -> None:
        with self.assertRaises(ValueError):
            reply("m", "r", [SEVERITY], [Decision(np.array([0.5, 0.5]), np.zeros(2))])
        with self.assertRaises(ValueError):
            reply("m", "r", [SEVERITY, HUMAN], [Decision(np.array([0.2, 0.3, 0.5]), np.zeros(2))])


if __name__ == "__main__":
    unittest.main()
