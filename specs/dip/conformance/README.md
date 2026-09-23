# DIP conformance corpus

Language-neutral, so neither implementation owns it. Both `packages/golibs/dip` and
`packages/pylibs/dip` read these files and must reach the same verdict on every case.

| file | layer | what it pins |
| --- | --- | --- |
| `framing.json` | bytes on the wire | prologue parsing, chunk reassembly, every ceiling, and the three outcomes: accept, reject, incomplete |
| `dispatch.json` | decoded control block | which ops exist, which fields each declares, and the refusal of anything undeclared |
| `responses.json` | generated types | real response bodies both languages must decode with the types generated from the IDL |

The three layers catch different things, which was learned the hard way. A generated Go
validator once rejected every real OCR response — it applied the outer `minItems` of `box`
(4 corners) to the inner point arrays (2 numbers) — and neither of the first two corpora
could see it, because neither decodes a response with the generated types. `responses.json`
is that layer.

`framing.json` is generated. Regenerate with `make dip-corpus`; the builder is
`build_corpus.py`, stdlib only, and it asserts that its own bulk fixtures still exceed the
limits they exist to test. `dispatch.json` and `responses.json` are written by hand, because
their cases are judgements and captured real bodies rather than byte sequences.

Datagrams are base64 so binary survives JSON exactly. Payloads are asserted by length and
sha256 rather than echoed, which keeps the file reviewable without weakening the assertion.

Adding a case means adding it here once. Both suites pick it up; neither needs editing.
