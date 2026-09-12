"""The dispatch half of the conformance corpus: `specs/dip/conformance/dispatch.json`.

One layer above framing: the control block has decoded, and the question is whether the op
exists and declares the fields it was sent. What happens after that depends on receiver
state and is not this corpus's business.
"""

from __future__ import annotations

import unittest

from dip import OP_FIELDS, validate

from . import corpus

CORPUS = corpus.load("dispatch.json")


class DispatchCorpusTest(unittest.TestCase):
    def test_the_op_table_is_the_corpus_op_table(self) -> None:
        """The fields each op declares are a fact about the protocol, not about a worker."""
        self.assertEqual(CORPUS["corpus"], "dip-dispatch")
        self.assertEqual(
            {op: sorted(fields) for op, fields in OP_FIELDS.items()},
            {op: sorted(fields) for op, fields in CORPUS["op_fields"].items()},
        )

    def test_every_case(self) -> None:
        for case in CORPUS["cases"]:
            with self.subTest(case["name"]):
                expect = case["expect"]
                refusal = validate(case["control"])

                if expect["outcome"] == "accept":
                    self.assertIsNone(refusal, "a well-formed op must dispatch")
                    continue

                self.assertEqual(expect["outcome"], "reject", "unknown outcome in the corpus")
                self.assertIsNotNone(refusal, "this op should have been refused")
                self.assertFalse(refusal["ok"])
                self.assertEqual(refusal["error"]["code"], expect["error"])
                if "message_contains" in expect:
                    self.assertIn(expect["message_contains"], refusal["error"]["message"])


if __name__ == "__main__":
    unittest.main()
