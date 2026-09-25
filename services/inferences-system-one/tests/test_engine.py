"""The engine's own logic, on an in-memory tokenizer, a per-token fake encoder and small random
head weights. The parity guard (test_parity.py) holds the same code to the real model."""

from __future__ import annotations

import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from textinfer import InvalidRequest

from system_one_worker.engines import ENGINE_NAMES, build_engine, decision
from system_one_worker.engines.decision import (DeadlineExceeded, TooMuchWork, act_features, check_provenance,
                                                check_temperature,
                                                confidence_from_probs, gelu, head_count, layer_norm,
                                                load_safetensors, read_special, render_options, softmax,
                                                temp_bucket, to_internal)
from worker import UnknownEngine

from .support import CFG, D, HEADS, SPECIAL, TOKENIZER_CONFIG, VOCAB, FakeSession, decider, head_weights, tokenizer, \
    write_safetensors

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_head.npz"

SEVERITY = {"t": "choice", "ins": "severity", "crit": {"info": None, "warning": None, "critical": None}}
HUMAN = {"t": "noul", "ins": "needs human", "crit": None}
LEVELS = {"t": "score", "ins": "severity", "crit": ["ok", "down"]}


class SequenceTest(unittest.TestCase):
    def test_the_layout_is_the_references(self) -> None:
        ids, markers = decider().build_sequence("oom restart", SEVERITY)
        v = VOCAB
        self.assertEqual(ids, [v["<bos>"], v["choice"], v["question:"], v["severity"], v["<eos>"],
                               v["<mask>"], v["info"], v["<mask>"], v["warning"], v["<mask>"], v["critical"],
                               v["<eos>"], v["oom"], v["restart"], v["<eos>"]])
        self.assertEqual(markers, [5, 7, 9])
        self.assertTrue(all(ids[m] == SPECIAL.mask_id for m in markers))

    def test_rendered_options_per_type(self) -> None:
        cases = [
            ("choice keys, with a description when given",
             {"t": "choice", "crit": {"a": None, "b": "the b one"}}, ["a", "b: the b one"]),
            ("score levels are numbered", {"t": "score", "crit": ["ok", "down"]}, ["level 0: ok", "level 1: down"]),
            ("noul is false then true", {"t": "noul", "crit": None},
             ["false: no, the statement does not hold", "true: yes, the statement holds"]),
            ("noul criteria replace the defaults", {"t": "noul", "crit": {"true": "it is down"}},
             ["false: no, the statement does not hold", "true: it is down"]),
        ]
        for name, q, want in cases:
            with self.subTest(name):
                self.assertEqual(render_options(q), want)

    def test_the_state_is_cut_to_max_len_and_still_ends_in_sep(self) -> None:
        ids, markers = decider().build_sequence(" ".join(["disk"] * 500), SEVERITY)
        self.assertEqual(len(ids), CFG["max_len"])
        self.assertEqual(ids[-1], SPECIAL.sep_id)
        self.assertEqual(len(markers), 3)

    def test_a_mask_token_in_the_input_cannot_forge_a_marker(self) -> None:
        ids, markers = decider().build_sequence("oom <mask> restart", SEVERITY)
        self.assertEqual([i for i, t in enumerate(ids) if t == SPECIAL.mask_id], markers)

    def test_too_many_options_are_shrunk_evenly_and_an_unfit_question_is_refused(self) -> None:
        many = {"t": "choice", "ins": "x", "crit": {("info warning critical " * 3 + str(i)): None for i in range(4)}}
        ids, markers = decider().build_sequence("ok", many)
        self.assertEqual(len(markers), 4)
        self.assertLessEqual(markers[-1], CFG["head_max_len"] + 8)
        cramped = {**CFG, "max_len": 16, "head_max_len": 18}
        with self.assertRaises(InvalidRequest):
            decider(cfg=cramped, max_batch_tokens=40).decide("ok", [many])

    def test_the_upstream_request_shape_maps_to_the_references(self) -> None:
        self.assertEqual(to_internal({"type": "choice", "instructions": "x", "criteria": ["a", "b"]}),
                         {"t": "choice", "ins": "x", "crit": {"a": None, "b": None}})
        self.assertEqual(to_internal({"type": "noul", "instructions": {"k": 1}})["ins"], '{"k": 1}')


class HeadTest(unittest.TestCase):
    def test_the_last_layer_on_selected_rows_equals_the_full_layer_gathered(self) -> None:
        d = decider()
        h = np.random.default_rng(1).standard_normal((2, 12, D)).astype(np.float32)
        pad = np.zeros((2, 12), bool)
        pad[1, 9:] = True
        rows = np.array([[0, 3, 5], [0, 2, 8]])
        full = d._attn_layer(1, h, pad)
        some = d._attn_layer(1, h, pad, rows=rows)
        np.testing.assert_allclose(some, np.take_along_axis(full, rows[:, :, None], 1), rtol=1e-5, atol=1e-5)
        self.assertGreater(np.abs(full[:, 1:] - full[:, :-1]).max(), 1e-2, "rows must differ for this to prove anything")

    def test_padding_and_grouping_do_not_move_any_answer(self) -> None:
        questions = [SEVERITY, HUMAN, LEVELS]
        session = FakeSession()
        together = decider(session=session).decide("oom restart restart", questions)
        self.assertEqual(len(session.calls), 1, "the questions must have shared one padded batch")
        alone = [decider().decide("oom restart restart", [q])[0] for q in questions]
        for a, b in zip(together, alone):
            np.testing.assert_allclose(a.probabilities, b.probabilities, atol=1e-6)
            self.assertAlmostEqual(a.act_probability, b.act_probability, places=6)

    def test_the_padded_batch_really_was_padded(self) -> None:
        session = FakeSession()
        decider(session=session).decide("oom", [SEVERITY, HUMAN])
        mask = session.calls[0]["attention_mask"]
        self.assertEqual(mask.shape[0], 2)
        self.assertLess(mask.sum(), mask.size)

    def test_the_answer_changes_with_the_input(self) -> None:
        d = decider()
        a = d.decide("oom restart", [SEVERITY])[0].probabilities
        b = d.decide("disk ok", [SEVERITY])[0].probabilities
        self.assertGreater(np.abs(a - b).max(), 1e-3)

    def test_probabilities_are_a_distribution_over_the_questions_options(self) -> None:
        for q, k in ((SEVERITY, 3), (HUMAN, 2), (LEVELS, 2)):
            with self.subTest(q["t"]):
                d = decider().decide("oom", [q])[0]
                self.assertEqual(d.probabilities.shape, (k,))
                self.assertAlmostEqual(float(d.probabilities.sum()), 1.0, places=12)
                self.assertTrue(0.0 <= d.act_probability <= 1.0)

    def test_the_temperature_comes_from_the_bucket_then_the_type(self) -> None:
        base = decider().decide("oom", [SEVERITY, HUMAN])
        cfg = {**CFG, "temperature": [1.0, 1.0, 4.0], "temperature_by_options": {"choice:3-5": 0.5}}
        hot = decider(cfg=cfg).decide("oom", [SEVERITY, HUMAN])
        logits = [np.log(b.probabilities) for b in base]
        np.testing.assert_allclose(hot[0].probabilities, softmax(logits[0] / 0.5), atol=1e-6)
        np.testing.assert_allclose(hot[1].probabilities, softmax(logits[1] / 4.0), atol=1e-6)

    def test_temperature_buckets(self) -> None:
        for qtype, k, want in ((0, 2, "choice:2"), (0, 5, "choice:3-5"), (0, 6, "choice:6-10"),
                               (0, 11, "choice:11+"), (1, 4, "score:3-5"), (2, 2, "noul:2")):
            with self.subTest(want):
                self.assertEqual(temp_bucket(qtype, k), want)

    def test_the_head_must_match_its_config(self) -> None:
        cases = [
            ("a missing layer", dict(weights=head_weights(layers=1)), "head has 1 layers, config says 2"),
            ("a batch that cannot hold one question", dict(max_batch_tokens=10), "max_batch_tokens"),
            ("heads that do not split the width", dict(heads=3), "3 heads cannot split"),
        ]
        for name, kwargs, mentions in cases:
            with self.subTest(name), self.assertRaisesRegex(ValueError, mentions):
                decider(**kwargs)

    def test_gelu_is_exact_erf_and_layer_norm_normalises(self) -> None:
        x = np.array([-2.0, -0.5, 0.0, 1.0, 3.0], np.float32)
        want = [v * 0.5 * (1 + math.erf(v / math.sqrt(2))) for v in x]
        np.testing.assert_allclose(gelu(x), want, atol=1e-6)
        y = layer_norm(np.array([[1.0, 2.0, 3.0, 4.0]], np.float32), np.ones(4, np.float32), np.zeros(4, np.float32))
        self.assertAlmostEqual(float(y.mean()), 0.0, places=6)
        self.assertAlmostEqual(float(y.std()), 1.0, places=4)


class GoldenHeadTest(unittest.TestCase):
    """The numpy head against upstream's torch head (export/golden_head.py) on the same fake-size
    weights: two heads, padded rows, three question types, unused option slots."""

    class Replay:
        def __init__(self, h: np.ndarray) -> None:
            self.h = h

        def run(self, names, feeds):
            return [self.h]

    def test_the_numpy_head_is_torchs_arithmetic(self) -> None:
        g = np.load(GOLDEN)
        self.assertEqual(g["h"].shape[-1], D)
        self.assertEqual(HEADS, 2, "one head would never exercise the multi-head split")
        self.assertTrue((g["attention_mask"] == 0).any(), "the fixture must pad, or the mask is untested")
        d = decider(session=self.Replay(g["h"]))
        logits, act = d.forward(np.zeros(g["attention_mask"].shape, np.int64), g["attention_mask"],
                                g["marker_pos"], g["marker_mask"], g["qtype"])
        np.testing.assert_allclose(logits, g["logits"], rtol=1e-5, atol=1e-4)
        np.testing.assert_allclose(act, g["act_logits"], rtol=1e-5, atol=1e-4)
        self.assertGreater(np.abs(np.diff(g["act_logits"], axis=0)).max(), 1e-2, "rows must differ")


class ActFeaturesTest(unittest.TestCase):
    def test_the_four_features_worked_by_hand(self) -> None:
        h3 = -(0.5 * math.log(0.5) + 2 * 0.25 * math.log(0.25)) / math.log(3)
        cases = [
            ("three options", [0.5, 0.25, 0.25, 0.0], [1, 1, 1, 0], [0.5, 0.25, h3, 3 / 255]),
            ("certain of two", [1.0, 0.0], [1, 1], [1.0, 1.0, 0.0, 2 / 255]),
            ("one option counts as two", [1.0, 0.0], [1, 0], [1.0, 1.0, 0.0, 2 / 255]),
        ]
        for name, p, mask, want in cases:
            with self.subTest(name):
                mask = np.array([mask], bool)
                logits = np.where(mask, np.log(np.maximum(np.array([p], np.float32), 1e-30)), np.float32(-1e4))
                np.testing.assert_allclose(act_features(logits.astype(np.float32), mask)[0], want, atol=1e-6)


class ConfidenceTest(unittest.TestCase):
    def test_upstreams_measure_one_minus_normalised_entropy(self) -> None:
        cases = [
            ("certain", np.array([1.0, 0.0, 0.0]), 3, 1.0),
            ("uniform", np.array([0.25] * 4), 4, 0.0),
            ("the real alert", np.array([0.011216, 0.850673, 0.138110]), 3, 0.5801),
            ("one option", np.array([1.0]), 1, 1.0),
        ]
        for name, p, k, want in cases:
            with self.subTest(name):
                self.assertAlmostEqual(confidence_from_probs(p, k), want, places=4)
        self.assertAlmostEqual(decider().decide("oom", [HUMAN])[0].confidence,
                               confidence_from_probs(decider().decide("oom", [HUMAN])[0].probabilities, 2))


class BudgetTest(unittest.TestCase):
    def test_too_much_planned_work_is_refused_before_the_encoder_runs(self) -> None:
        session = FakeSession()
        with self.assertRaisesRegex(TooMuchWork, "2 questions plan [0-9]+ padded tokens, over the 20"):
            decider(session=session).decide("oom", [SEVERITY, HUMAN], max_tokens=20)
        self.assertEqual(session.calls, [])
        self.assertEqual(len(decider(session=session).decide("oom", [SEVERITY, HUMAN], max_tokens=128)), 2)

    def test_a_passed_deadline_stops_between_batches_and_returns_nothing(self) -> None:
        session = FakeSession()
        with self.assertRaisesRegex(DeadlineExceeded, "after 0 of 3 batches"):
            decider(session=session, max_batch_tokens=CFG["max_len"]).decide(
                " ".join(["disk"] * 60), [SEVERITY, HUMAN, LEVELS], deadline=time.monotonic() - 1)
        self.assertEqual(session.calls, [], "no batch may start after the deadline")

    def test_the_deadline_is_checked_before_every_batch_not_only_the_first(self) -> None:
        session = FakeSession()
        clock = iter([0.0, 0.0, 10.0])
        with mock.patch.object(decision.time, "monotonic", side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(DeadlineExceeded, "after 2 of 3 batches \\(2 of 3 questions\\)"):
            decider(session=session, max_batch_tokens=CFG["max_len"]).decide(
                " ".join(["disk"] * 60), [SEVERITY, HUMAN, LEVELS], deadline=5.0)
        self.assertEqual(len(session.calls), 2)


class CheckpointTest(unittest.TestCase):
    def test_the_heads_come_from_upstreams_rule_and_must_match_the_checkpoint(self) -> None:
        self.assertEqual(head_count(768, {"num_attention_heads": 12}), 12)
        with self.assertRaisesRegex(ValueError, "into 12 heads, the checkpoint states 16"):
            head_count(768, {"num_attention_heads": 16})

    def test_the_temperature_is_the_configs_and_a_disagreeing_buffer_is_refused(self) -> None:
        check_temperature({"temperature": [1.0, 1.0, 1.0]}, {"temperature": np.ones(3, np.float32)})
        check_temperature({"temperature": [1.5, 1.0, 1.0]}, {})
        with self.assertRaisesRegex(ValueError, "disagrees with the checkpoint's buffer"):
            check_temperature({"temperature": [1.5, 1.0, 1.0]}, {"temperature": np.ones(3, np.float32)})


class FilesTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_safetensors_are_read_as_fp32_and_the_encoder_is_skipped(self) -> None:
        tensors = {"encoder.x": np.ones((2, 2)), "a": np.array([[1.5, -2.0]]), "b": np.array([0.25, 3.0])}
        write_safetensors(self.root / "m.safetensors", tensors, {"a": "F16"})
        got = load_safetensors(self.root / "m.safetensors")
        self.assertEqual(sorted(got), ["a", "b"])
        self.assertEqual(got["a"].dtype, np.float32)
        np.testing.assert_array_equal(got["a"], [[1.5, -2.0]])
        np.testing.assert_array_equal(got["b"], [0.25, 3.0])

    def test_a_dtype_it_cannot_read_is_refused_not_misread(self) -> None:
        write_safetensors(self.root / "m.safetensors", {"a": np.zeros(2)}, {"a": "BF16"})
        with self.assertRaisesRegex(ValueError, "BF16"):
            load_safetensors(self.root / "m.safetensors")

    def test_the_encoder_must_come_from_the_pinned_weights(self) -> None:
        stamp = self.root / "encoder.json"
        stamp.write_text(json.dumps({"weights_sha256": "abc"}))
        check_provenance(stamp, "abc")
        cases = [("other weights", "abd", "exported from weights abc"),
                 ("no stamp", None, "cannot read the encoder's provenance")]
        for name, expected, mentions in cases:
            with self.subTest(name), self.assertRaisesRegex(ValueError, mentions):
                check_provenance(stamp if expected else self.root / "missing.json", expected or "abc")

    def test_special_tokens_must_exist_in_the_tokenizer(self) -> None:
        self.assertEqual(read_special(tokenizer(), TOKENIZER_CONFIG), SPECIAL)
        with self.assertRaisesRegex(ValueError, "pad_token"):
            read_special(tokenizer(), {**TOKENIZER_CONFIG, "pad_token": "[PAD]"})


class OnnxDecisionTest(unittest.TestCase):
    """The adapter's file handling, with only the ONNX session patched out."""

    OPTIONS = {"onnx": "encoder.onnx", "weights": "model.safetensors", "config": "rl_agent_config.json",
               "encoder_config": "encoder/config.json",
               "tokenizer": "tokenizer/tokenizer.json", "tokenizer_config": "tokenizer/tokenizer_config.json",
               "encoder_built_from": "feed", "max_batch_tokens": 128}

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tokenizer").mkdir()
        tokenizer().save(str(self.root / "tokenizer" / "tokenizer.json"))
        (self.root / "tokenizer" / "tokenizer_config.json").write_text(json.dumps(TOKENIZER_CONFIG))
        (self.root / "rl_agent_config.json").write_text(json.dumps(CFG))
        (self.root / "encoder").mkdir()
        (self.root / "encoder" / "config.json").write_text(json.dumps({"num_attention_heads": HEADS}))
        weights = {**head_weights(), "encoder.embeddings.tok_embeddings.weight": np.zeros((4, 64))}
        write_safetensors(self.root / "model.safetensors", weights, {k: "F16" for k in weights})
        (self.root / "onnx").mkdir()
        (self.root / "onnx" / "encoder.json").write_text(json.dumps({"weights_sha256": "feed"}))

    def build(self, options=None):
        session = FakeSession()
        with mock.patch.object(decision, "open_session", return_value=session) as opened:
            engine = build_engine("onnx_decision", self.root, options or dict(self.OPTIONS))
        return engine, opened

    def test_it_opens_the_graph_beside_the_model_and_answers(self) -> None:
        engine, opened = self.build()
        self.assertEqual(opened.call_args.args[0], self.root / "onnx" / "encoder.onnx")
        answers = engine.decide("oom", [SEVERITY])
        self.assertEqual(answers[0].probabilities.shape, (3,))
        with self.assertRaisesRegex(ValueError, "POST /decide"):
            engine.infer(b"x")
        engine.close()
        engine.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            engine.decide("oom", [SEVERITY])

    def test_the_graph_directory_can_be_set_by_the_image(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "encoder.json").write_text(json.dumps({"weights_sha256": "feed"}))
        with mock.patch.dict("os.environ", {decision.ONNX_DIR_ENV: str(other)}):
            _, opened = self.build()
        self.assertEqual(opened.call_args.args[0], other / "encoder.onnx")

    def test_it_refuses_before_opening_anything_it_cannot_trust(self) -> None:
        cases = [("a missing option", {k: v for k, v in self.OPTIONS.items() if k != "weights"}, "missing weights"),
                 ("an encoder from other weights", {**self.OPTIONS, "encoder_built_from": "beef"}, "rebuild it"),
                 ("a head split the checkpoint does not state", {**self.OPTIONS, "encoder_config": "tokenizer/tokenizer_config.json"},
                  "checkpoint states None")]
        for name, options, mentions in cases:
            with self.subTest(name):
                with mock.patch.object(decision, "open_session") as opened, \
                        self.assertRaisesRegex(ValueError, mentions):
                    build_engine("onnx_decision", self.root, options)
                opened.assert_not_called()

    def test_an_unknown_engine_is_refused_by_name(self) -> None:
        self.assertEqual(ENGINE_NAMES, ("onnx_decision",))
        with self.assertRaisesRegex(UnknownEngine, "onnx_decision"):
            build_engine("torch", self.root, {})


if __name__ == "__main__":
    unittest.main()
