# Console release 1 — round four (last): the independent pass's findings

The independent final reviewer (`.claude/review/report4.md`) confirmed round three fixed its items, verified the
deadline contract with real probes, and **caught the flake you disclosed**. Three things remain, and the first is
a regression round three introduced. Fix them in this worktree on `feat/inferences-console-r1`. **Do not merge.**

## MUST-FIX A — the worker's own HTML 500 on Decide is now described as "not the worker"

The path: the worker turns any uncaught exception into Python's `send_error(500)`, which is an **HTML** page
(`packages/pylibs/worker/src/worker/metrics.py:360-363`); `POST /decisions` relays a worker's non-200
**verbatim** (`services/dita-orchestrator/handler/decisions/handler.go:193-198`); `toProblem` then yields
`{error: "<!DOCTYPE HTML>…"}` with no `error_type`, `worker` or `reason`; and `errors.ts` prints
"The orchestrator, or something in front of it, answered 500 … **This says nothing about any worker or its
model**". That sentence is a positive false statement about the worker's own answer — and round three
introduced it: before `ce5f00d` the same response fell to `default` and read the neutral "The request failed
(500)".

Fix — pick the cleanest that fits, and say which you chose and why:
1. the orchestrator wraps a relayed non-JSON worker answer in its own `{error, error_type: "Backend", worker}`
   (the reviewer's preferred option); or
2. the worker answers JSON `{error, error_type: "Backend"}` instead of `send_error(500)`; or
3. the console drops the "says nothing about any worker" sentence for an HTML body on a relaying route.

In every case: **never render raw HTML in the banner detail**, add the Decide row to `DecidePage.test.tsx`, and
correct the now-false `errors.ts:6` claim (a worker-written body does not always name its error) and the PR line
that repeats it.

## SHOULD-FIX B — pin the assumption the deadline rests on

`READ_DEADLINE_MS = 2 × probe + 5 s` holds only because `Workers` inspects each worker in its own goroutine
(`workers.go:33-40`). The reviewer's mutant making that sequential **survived both suites**, and the worst case
would then be 3 workers × 2 probes × 5 s = 30 s against a 15 s deadline. Add a Go test that runs `Workers` with
two or three slow workers and a short `ProbeTimeout`, and asserts the total stays within about 2 × `ProbeTimeout`
plus an epsilon. The contract must be held by a test on the side that owns the assumption.

## SHOULD-FIX C — fix the flake, and describe it honestly

The flake you disclosed is real and the reviewer localised it: **3 failures in 67 full suite runs**, always a
Testing Library `findBy*` giving up at its default **1000 ms**, at `src/pages/DecidePage.test.tsx:72` (the first
test in its file, so it pays for the first render in a fresh jsdom) and `src/pages/NotFoundPage.test.tsx:38`.
Contention crosses the margin; it failed with two suites running at once, in the first run of a cold copy, and
its margin is thin even at `--maxWorkers=12` (1150–1302 ms against a 1000 ms wait).

Give those waits real headroom (per-call timeouts, or `configure({ asyncUtilTimeout })` in `src/test/setup.ts` —
choose and justify), then show the fix under the condition that reproduced it if you can: two suites at once.
Replace the PR's "unexplained test failure" note with the two test names, the cause (contention against a 1 s
margin), the fact that the link to your original failure is **unproven**, and what you changed.

## NIT

- `errors.ts:24-25` says "A 5xx is read from its body, never its route" — but the title still switches on the
  route (`read ? "The gateway" : "The orchestrator"`), and so does `describeRead`. Fix the wording or the code so
  they agree.
- The PR's pasted `pnpm vitest run` output shows `Test Files 19 passed (19)` next to `Tests 296 passed (296)`;
  HEAD has **20** test files. Re-paste the real transcript.
- Reconcile the small numeric discrepancies the reviewer found: `todo.md` says 11 new mutants where the PR says
  12 (the difference is the removed-precondition row), the behaviour test uses 10.3 s not 10.2 s, and mutant
  M1a's "5 failed" is 16 on the full suite. Make the PR match what the tests actually do.
- Disclose, do not fix: any HTML 5xx still shows its raw HTML as the detail (Caddy error pages included), and
  `config.go`'s comment density is 16.5% — both predate this PR.

## Evidence

Gates: `pnpm lint`, `pnpm vitest run`, `pnpm build`, `go vet`, `gofmt -l`, `go test -count=1 ./...`. Mutation
proofs for the new guards, with real red output. For C, the run that shows the margin no longer breaks. A new
screenshot only if the UI changed. Push, update PR #31, leave it open.

## Constraints

Comments under ~10% of lines. Read and write only inside this worktree. Do not touch `main` or another worktree.
**Do not redeploy, reload or restart any service**; the deployed gateway on `127.0.0.1:2104` stays as it is.
