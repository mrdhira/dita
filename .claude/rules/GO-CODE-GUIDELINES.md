# Go code guidelines

## Dependencies

- **Stdlib first.** A third-party module needs a stated reason in the PR: what it buys, and
  why the standard library cannot. The reference client for the worker protocol is stdlib
  only and will stay that way — the framing is forty lines.
- **CGO-free.** `CGO_ENABLED=0` everywhere. A static binary is why the orchestrator image can
  be `FROM scratch`.
- **Pin the toolchain.** Every `go.mod` carries `go` at the exact version in
  `.tool-versions`; `go.work` carries that plus a matching `toolchain` directive. Do **not**
  add a `toolchain` line to a `go.mod` that already names the same version: Go treats it as
  redundant, `go build` fails with "updates to go.mod needed" and `go mod tidy` deletes it.
  A `go` directive at the full patch is the pin. `toolchain` in a `go.mod` is only meaningful
  when it names a version newer than the `go` line.
- `go.sum` and `go.work.sum` are committed even while empty. An empty lockfile is not a lock;
  it is the file that will hold one the day a dependency arrives.

## Layout

- A service owns `services/<name>/`. Shared Go lives in `packages/golibs/<name>/`, each its
  own module, listed in `go.work`.
- Copying code between services is how two implementations drift. If two services need it, it
  belongs in `packages/golibs/`.

## Style

- **`gofmt` and `go vet` clean.** Not negotiable, and not something a reviewer should have to
  mention.
- **Errors are wrapped with context**: `fmt.Errorf("dial %s: %w", path, err)`. The `%w` keeps
  `errors.Is` working; the prefix tells the reader which call failed.
- **No `panic` outside `main`.** A library that panics removes the caller's choice. Return an
  error and let the caller decide.
- Accept interfaces, return structs. Keep interfaces small and define them where they are
  consumed, not where they are implemented.
- Exported identifiers have doc comments beginning with the identifier's name.
- Contexts are the first parameter and are honoured, not accepted and ignored.

## Comments

A comment exists to prevent a mistake. Nothing else earns its place.

Keep: a non-obvious why (a footgun, a spec quirk, a limit that would surprise a reader); the
contract a caller cannot infer from the code (what it guarantees, what it refuses); a build
tag or a generated-file marker.

Cut: what the code already says; how the file got here, what was tried, what went wrong
before; a restated signature, field name or JSON key; section banners; anything a reviewer
would call narration.

Where the cut text belongs instead: the pull request description, `docs/`, or
`.claude/tasks/lessons.md`. Not the source.

The bar: a reader should find the file comparable to the Go standard library, which is close
to comment-free. As a smell test, comment lines stay well under 10% of a source file; above
that, the file is usually carrying prose that belongs somewhere else.

## Tests

- **Table-driven with `t.Run(name, ...)`**, so a failure names the case.
- `t.Helper()` in helpers; `t.Cleanup` over `defer` when the fixture outlives the function.
- Use `testing.T.TempDir` and real files rather than mocking the filesystem.
- Race-sensitive code runs under `-race` in CI and before you claim it works.
- A test that asserts a mock was called proves something about the mock. Prefer a real
  dependency, or assert on the observable result.

## The worker protocol

- Dial `unixpacket` — it is `SOCK_SEQPACKET`, so one `Write` is one datagram. Never wrap the
  connection in `bufio`; it would merge datagrams and destroy the framing.
- Never write more than the advertised `max_chunk` in one call: the kernel rejects an
  oversized datagram rather than splitting it.
- Handshake once per connection, check the protocol version, and read the limits and timeouts
  from the response instead of hardcoding them.
- `error.code` is the stable contract; `error.message` is prose. Branch on the code.
