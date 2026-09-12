# ocrclient — the reference Go client

> Dials the worker's unix socket and runs the whole contract: handshake, list, load, infer, unload.

Standard library only, transitively: the framing lives in
[`packages/golibs/dip`](../../../../packages/golibs/dip), which is this repo's own module
and has no requires of its own. Nothing to vendor. This is the shape
`services/dita-orchestrator` will follow when it grows a worker client.

## Run it

Start a worker, then point the client at its socket:

```bash
# terminal 1 — the worker
cd services/inferences-ocr
MODELS_DIR=./models SOCKET_PATH=../../run/inferences-ocr.sock python -m ocr_worker

# terminal 2 — the client
cd services/inferences-ocr/examples/go
go run . -socket ../../../../run/inferences-ocr.sock -image /path/to/page.png
```

Flags: `-socket` (default `$SOCKET_PATH`, else `/run/dita/inferences-ocr.sock`), `-image`
(required), `-model` (default: whatever the worker reports as `default_model`).

Real output against a live worker, cold (the model was downloaded and verified during `load`):

```
handshake  inferences-ocr v0.1.0, protocol 2
           ops: handshake, version, list, load, unload, infer, livez, readyz, startupz
           max_chunk=65536 max_control=8388608 max_payload=67108864
list       3 models, default "rapidocr-ppocrv5"
           rapidocr-ppocrv5     rapidocr   ja,en,zh     20 MB
           tesseract            tesseract  ja,en        -
           manga-ocr            manga_ocr  ja           439 MB
load       rapidocr-ppocrv5 (rapidocr) in 6074ms, evicted nothing
infer      test_en_ja.png on rapidocr-ppocrv5 in 1218ms, 3 lines
           [0] 0.968  Hello dita OCR 2026
           [1] 0.991  The quick brown fox
           [2] 1.000  日本語のテキスト認識
unload     done
```

## The three things a client has to get right

None of them are in this file any more — `dip` does them, and the conformance corpus proves
it does them the same way the Python worker does.

**`unixpacket`, not `unix`.** Go's `unixpacket` network is `SOCK_SEQPACKET`: message
boundaries and ordering are preserved, so one `Write` is one datagram and one `Read` is one
datagram. Never wrap the connection in a `bufio.Writer` — it would merge datagrams and
destroy the framing the protocol depends on.

**Chunk both sections.** An AF_UNIX datagram cannot exceed `SO_SNDBUF`, 212992 bytes by
default; above that `Write` fails with `EMSGSIZE` rather than fragmenting. That applies to
the control block just as much as to the image: a dense page's OCR result runs past it. So a
message is a small prologue datagram carrying both lengths, then the control block and the
payload each split at `max_chunk`, which `dip.Handshake` learns from the worker rather than
assuming. See `packages/golibs/dip/framing.go`.

**Check `ok` on every response.** A failure is `{"ok": false, "error": {"code", "message"}}`,
and `dip` returns it as an error carrying the code: branch with `dip.CodeOf(err)`. `code` is
the stable part; `message` is for humans. `checksum_mismatch` and `fetch_failed` mean
retrying `infer` will not help — retry the `load` or pick another model.

The protocol table, the limits and the error codes are in
[the service README](../../README.md).
