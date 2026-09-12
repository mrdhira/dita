"""The response half of the conformance corpus: `specs/dip/conformance/responses.json`.

Whole response bodies, decoded with the dataclasses generated from the IDL. `framing.json`
proves the bytes reassemble and `dispatch.json` proves the op is known; neither ever hands
a body to a generated type, which is how a generated validator that refused every real
infer response -- it applied the outer `minItems` of `box` to the inner points -- reached
both implementations unnoticed.

The generated dataclasses do not validate, so the decoder here does: it refuses an unknown
field, a missing required one, a wrong scalar type and a wrongly nested list. Without that
`InferResponse(**body)` would accept almost anything and this file would be scenery.
"""

from __future__ import annotations

import unittest
from dataclasses import MISSING, asdict, fields, is_dataclass
from enum import StrEnum
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from dip import types

from . import corpus

CORPUS = corpus.load("responses.json")

# The corpus `type` field, mapped onto the generated dataclass it names. A case naming
# something absent from here fails; skipping it would leave the corpus looking green while
# proving nothing.
RESPONSE_TYPES: dict[str, type] = {
    "InferResponse": types.InferResponse,
    "HandshakeResponse": types.HandshakeResponse,
    "ListResponse": types.ListResponse,
    "LoadResponse": types.LoadResponse,
    "UnloadResponse": types.UnloadResponse,
    "ProbeResponse": types.ProbeResponse,
    "ErrorResponse": types.ErrorResponse,
}


def decode(annotation: Any, value: Any, path: str = "body") -> Any:
    """Build `annotation` out of `value`, refusing anything the annotation does not describe."""
    while hasattr(annotation, "__value__"):  # `type Point = List[float]` and friends.
        annotation = annotation.__value__

    origin = get_origin(annotation)

    if origin is Literal:
        allowed = get_args(annotation)
        if value not in allowed or not isinstance(value, type(allowed[0])):
            raise ValueError(f"{path} is {value!r}, the type allows {allowed!r}")
        return value

    if origin is Union or origin is UnionType:
        arms = get_args(annotation)
        if value is None:
            if NoneType in arms:
                return None
            raise ValueError(f"{path} is null, the type does not allow it")
        refusals = []
        for arm in arms:
            if arm is NoneType:
                continue
            try:
                return decode(arm, value, path)
            except (TypeError, ValueError) as refusal:
                refusals.append(str(refusal))
        raise TypeError(f"{path} fits no arm of {annotation}: {'; '.join(refusals)}")

    if origin is list:
        if not isinstance(value, list):
            raise TypeError(f"{path} is {type(value).__name__}, the type says list")
        (item,) = get_args(annotation)
        return [decode(item, element, f"{path}[{i}]") for i, element in enumerate(value)]

    if is_dataclass(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"{path} is {type(value).__name__}, the type says {annotation.__name__}")
        hints = get_type_hints(annotation)
        declared = {field.name: field for field in fields(annotation)}
        unknown = sorted(set(value) - set(declared))
        if unknown:
            raise TypeError(f"{path} carries {unknown} which {annotation.__name__} does not declare")
        missing = sorted(
            name
            for name, field in declared.items()
            if name not in value and field.default is MISSING and field.default_factory is MISSING
        )
        if missing:
            raise TypeError(f"{path} is missing {missing}, required by {annotation.__name__}")
        return annotation(
            **{name: decode(hints[name], item, f"{path}.{name}") for name, item in value.items()}
        )

    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return annotation(value)  # ValueError names the offending value.

    if annotation is bool:
        if not isinstance(value, bool):
            raise TypeError(f"{path} is {type(value).__name__}, the type says bool")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{path} is {type(value).__name__}, the type says int")
        return value
    if annotation is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{path} is {type(value).__name__}, the type says float")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise TypeError(f"{path} is {type(value).__name__}, the type says str")
        return value

    raise TypeError(f"{path}: this reader has no rule for {annotation!r}")


def flatten_box(body: dict[str, Any]) -> dict[str, Any]:
    """The same body with every box as [x, y, x, y, ...], a shape the schema does not describe."""
    lines = [
        line
        | {"box": [number for corner in line["box"] for number in corner]}
        if line.get("box")
        else line
        for line in body["lines"]
    ]
    return body | {"lines": lines}


class ResponseCorpusTest(unittest.TestCase):
    def test_the_corpus_can_still_bite(self) -> None:
        """What the cases have to contain for decoding them to mean anything.

        Shrink the corpus to bodies with no box, no resident and no reasons and this fails,
        rather than every case below passing for the wrong reason.
        """
        self.assertEqual(CORPUS["corpus"], "dip-responses")
        self.assertEqual(CORPUS["protocol"], 2)
        self.assertNotEqual(CORPUS["cases"], [])

        boxes = [
            line["box"]
            for case in CORPUS["cases"]
            for line in case["body"].get("lines", [])
            if line.get("box")
        ]
        self.assertNotEqual(boxes, [], "no case carries a box: the bug this corpus exists for is unreachable")
        self.assertIn(
            [4, 2],
            [[len(box), len(box[0])] for box in boxes],
            "no case carries four corners of two numbers, which is the shape that was refused",
        )
        self.assertNotEqual(
            [case for case in CORPUS["cases"] if case["body"].get("resident")],
            [],
            "no case carries a non-null resident",
        )
        self.assertNotEqual(
            [case for case in CORPUS["cases"] if case["body"].get("reasons")],
            [],
            "no case carries a reason",
        )

    def test_every_case_decodes_with_the_generated_types(self) -> None:
        for case in CORPUS["cases"]:
            with self.subTest(case["name"]):
                self.assertIn(
                    case["type"],
                    RESPONSE_TYPES,
                    "the corpus names a type this reader does not know: add it here rather than "
                    "letting the case go unrun",
                )
                declared = RESPONSE_TYPES[case["type"]]
                body = case["body"]

                decoded = decode(declared, body, case["type"])
                self.assertIsInstance(decoded, declared)
                self.assert_nothing_dropped(body, asdict(decoded), case["type"])

                if isinstance(decoded, types.InferResponse):
                    self.assertEqual(
                        [line.box for line in decoded.lines],
                        [line["box"] for line in body["lines"]],
                        "box did not survive decoding with the generated type",
                    )
                    self.assertEqual([line.text for line in decoded.lines], [line["text"] for line in body["lines"]])
                elif isinstance(decoded, (types.HandshakeResponse, types.ListResponse, types.ProbeResponse)):
                    resident = body.get("resident")
                    if resident is None:
                        self.assertIsNone(decoded.resident)
                    else:
                        self.assertIsNotNone(decoded.resident, "resident was dropped")
                        self.assertEqual(asdict(decoded.resident), resident)
                    if isinstance(decoded, types.ProbeResponse):
                        self.assertEqual(decoded.reasons, body["reasons"], "reasons did not survive decoding")
                elif isinstance(decoded, types.LoadResponse):
                    self.assertEqual(decoded.id, body["id"])
                    self.assertEqual(decoded.unloaded, body["unloaded"])
                elif isinstance(decoded, (types.UnloadResponse, types.ErrorResponse)):
                    pass  # The round trip above already compared every field of these.
                else:
                    self.fail(f"no field assertions for {type(decoded).__name__}")

    def test_the_reader_refuses_a_box_of_the_wrong_shape(self) -> None:
        """The negative control: a decode that cannot fail would pass every case above."""
        localised = [
            case
            for case in CORPUS["cases"]
            if case["type"] == "InferResponse" and any(line.get("box") for line in case["body"]["lines"])
        ]
        self.assertNotEqual(localised, [], "no case carries a box, so this control proves nothing")

        for case in localised:
            with self.subTest(case["name"]):
                flattened = flatten_box(case["body"])
                self.assertNotEqual(flattened, case["body"], "flattening changed nothing")
                with self.assertRaises((TypeError, ValueError)):
                    decode(types.InferResponse, flattened, "InferResponse")

    def test_the_reader_refuses_an_unknown_field(self) -> None:
        """The other negative control: `InferResponse(**body)` would swallow a typo silently."""
        case = next(case for case in CORPUS["cases"] if case["type"] == "InferResponse")
        with self.assertRaises(TypeError):
            decode(types.InferResponse, case["body"] | {"lnies": []}, "InferResponse")
        with self.assertRaises(TypeError):
            decode(types.InferResponse, {k: v for k, v in case["body"].items() if k != "lines"}, "InferResponse")

    def assert_nothing_dropped(self, body: Any, round_tripped: Any, path: str) -> None:
        """Every field of the body must come back. A field the type does not know would
        otherwise vanish in silence, which is how a response loses `box` or `unloaded`.
        A field the body omits may come back as None: absence and null mean the same here."""
        if isinstance(body, dict):
            self.assertIsInstance(round_tripped, dict, f"{path} came back as {type(round_tripped).__name__}")
            for key, value in body.items():
                self.assertIn(key, round_tripped, f"{path}.{key} was dropped by the round trip")
                self.assert_nothing_dropped(value, round_tripped[key], f"{path}.{key}")
            for key, value in round_tripped.items():
                if key not in body:
                    self.assertIsNone(value, f"{path}.{key} appeared in the round trip as {value!r}")
        elif isinstance(body, list):
            self.assertIsInstance(round_tripped, list, f"{path} came back as {type(round_tripped).__name__}")
            self.assertEqual(len(body), len(round_tripped), f"{path} changed length in the round trip")
            for i, (want, got) in enumerate(zip(body, round_tripped, strict=True)):
                self.assert_nothing_dropped(want, got, f"{path}[{i}]")
        else:
            self.assertEqual(body, round_tripped, f"{path} changed in the round trip")


if __name__ == "__main__":
    unittest.main()
