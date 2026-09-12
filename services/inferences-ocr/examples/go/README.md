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

## Why this module has both a `go.work` entry and a `replace`

`go.mod` here requires `github.com/mrdhira/dita/packages/golibs/dip v0.0.0` — a version that
was never published — and pins it with:

```
replace github.com/mrdhira/dita/packages/golibs/dip => ../../../../packages/golibs/dip
```

The repository root also has a `go.work` listing both modules, which makes the `replace`
look redundant. It is not, and the difference is worth knowing before someone deletes it.

**Building does not need it. The module graph does.** `go build`, `go vet`, `go test` and
`go run` all resolve `dip` through the workspace and are happy with the `replace` removed.
`go mod tidy` and `go list -m` are not: those commands work on one module at a time and
deliberately ignore `go.work`, so with the `replace` gone they fall back to the network and
ask a proxy for a version that does not exist.

Measured from this directory, with the `replace` line deleted:

```
$ go build ./...                          # exit 0
$ go vet ./...                            # exit 0
$ go test ./...
ok  	github.com/mrdhira/dita/services/inferences-ocr/examples/go	0.003s
$ go run .
error: -image is required                 # exit 1 from the flag check, so it built and ran

$ go mod tidy
go: downloading github.com/mrdhira/dita/packages/golibs/dip v0.0.0
go: github.com/mrdhira/dita/services/inferences-ocr/examples/go imports
	github.com/mrdhira/dita/packages/golibs/dip: reading
	github.com/mrdhira/dita/packages/golibs/dip/go.mod at revision packages/golibs/dip/v0.0.0:
	unknown revision packages/golibs/dip/v0.0.0
```

Outside the workspace nothing survives the deletion. With `GOWORK=off` and no `replace`, all
four build commands fail the same way:

```
$ GOWORK=off go build ./...
go: downloading github.com/mrdhira/dita/packages/golibs/dip v0.0.0
main.go:22:2: reading github.com/mrdhira/dita/packages/golibs/dip/go.mod at revision
	packages/golibs/dip/v0.0.0: unknown revision packages/golibs/dip/v0.0.0
```

With the `replace` restored, `GOWORK=off go build`, `go vet`, `go test` and `go run` all pass
again. That is what the line buys: this module stands on its own outside the workspace, which
is what a container build gets when it copies one module directory rather than the repository.

**So it stays.** The bar was "something breaks without it", and `go mod tidy` breaks without
it inside the workspace, where the `replace` was supposed to be redundant.

**What it does not do.** A `replace` only applies while this module is the main module; it is
ignored by anything that consumes this module as a dependency, and a relative path cannot
survive publication. If `dip` is ever tagged and published, the `replace` goes and the
`require` takes a real version. Until then `go.work` is what makes editing both modules at
once work, and the `replace` is what keeps this one buildable on its own.

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
