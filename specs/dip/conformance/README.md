# DIP conformance corpus

Language-neutral, so neither implementation owns it. Both `packages/golibs/dip` and
`packages/pylibs/dip` read these files and must reach the same verdict on every case.

| file | layer | what it pins |
| --- | --- | --- |
| `framing.json` | bytes on the wire | prologue parsing, chunk reassembly, every ceiling, and the three outcomes: accept, reject, incomplete |
| `dispatch.json` | decoded control block | which ops exist, which fields each declares, and the refusal of anything undeclared |

`framing.json` is generated. Regenerate with `make dip-corpus`; the builder is
`build_corpus.py`, stdlib only, and it asserts that its own bulk fixtures still exceed the
limits they exist to test. `dispatch.json` is written by hand, because its cases are
judgements rather than byte sequences.

Datagrams are base64 so binary survives JSON exactly. Payloads are asserted by length and
sha256 rather than echoed, which keeps the file reviewable without weakening the assertion.

Adding a case means adding it here once. Both suites pick it up; neither needs editing.
