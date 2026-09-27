# Console release 1 — round three: the third pass's findings

The third adversarial pass (`.claude/review/report3.md`) confirmed A–E are fixed and each guard dies for the
right reason — and then found a sixth path of the same class, a timing problem, and three claims in the PR body
it **disproved**. Fix all of this, in this worktree, on `feat/inferences-console-r1`. **Do not merge.**

## MUST-FIX

**1. `describeError` is only route-aware for `/workers` and `/metrics`; every other page still does it wrong,
and the PR now documents the opposite.**
History (`GET /decisions?limit=50`), Templates (`GET /schemas`), the decision page, Eval, Decide and the
correction form all still go through the untouched switch: a reasonless 503 reads **"The worker has no model
loaded"** and a bare 502 reads **"The worker answered in a shape it was not asked for"**. Neither route touches a
worker. The 502 half is **live today**: Caddy answers an empty 502 when the orchestrator is down
(probe: History and Templates both render that line).
Worse, round two made two statements about this that are false:
- `lib/errors.ts:26-29`: "kept only for a pass-through route (Try it, decide), never on the console's own
  reads" — it is applied on every route *except* the two reads.
- the PR body, line 11, says the same thing. Note also that **Try it never routes an HTTP status through
  `describeError`** (`TryItTab.tsx:261-294` renders `result.problem` itself), so that claim was doubly wrong.
- For the record: `POST /decisions` does relay the worker's non-200 verbatim, so *there* a reasonless 503 really
  is the worker's no-model answer — but a bare 502 on the same route is not.

Fix: decide the wording from the **body**, not the route. Read "no model" and the worker-shaped 502/504 titles
only when the body carries the orchestrator's or the worker's JSON (an `error_type`, or a `worker` field). A
bare or empty 5xx is "the orchestrator, or something in front of it, answered N". Correct the docstring and the
PR line. Test History and Templates with an empty 502 **and** a reasonless 503, and Decide with the worker's own
503 (where "no model" is genuinely the worker's answer).

## SHOULD-FIX

**2. The 10 s read deadline sits inside the gateway's own worst case for `/workers`.**
`/workers` probes each worker's `/health` and then `/info`, sequentially, each bounded by
`DefaultProbeTimeout = 5 s` (`services/dita-orchestrator/.../config.go:24`, not configurable). The reviewer
measured a gateway answer arriving at **9.91 s** — 90 ms of margin, before Caddy and the network. The cost is
already visible: an answer landing 0.2 s after the deadline is discarded and shown as "the latest refresh
failed", and that happens exactly when a slow worker makes the gateway's honest report (`ready`, `info` null,
resident unknown) the thing worth reading.
Fix: the deadline must be at least 2 × the probe timeout plus margin (15 s is a fine choice), and the age budget
should follow it. Put the relationship where both sides can see it rather than as a magic number, and test it.

**3. The hidden-tab half of the staleness rule has no test.**
The reviewer's mutant (`interval === false` → a 20 s budget, so the budget runs while hidden) survived with 146
passed. Without that guard a hidden tab claims "the latest refresh has not answered" for a refresh that was
never issued. Cover it.

## NIT

- **A hung read surfaces as "the latest refresh failed" although nothing failed** — the console abandoned it.
  `Staleness` already has the fitting "has not answered" wording; use it, and update the Fleet deadline test
  that currently pins "failed".
- **Disclose, do not fix** the metrics-parsing weaknesses the reviewer found: a duplicated page doubles every
  counter, an `le` label typo silently drops histogram buckets, and the truncation defence depends on the
  worker's emission order (the uptime line last), which is undocumented in the console. Record them in the PR's
  not-done list.
- **Correct three disproved claims in the PR body**, and add a short "Third review" section (what it found, what
  you did, what it disproved of yours, what you could not verify):
  1. "the 'no model' reading is kept only for pass-through routes (Try it, decide)" — false, see MUST-FIX 1;
  2. "the independent reviewer confirmed all of them fixed" — overstated: its own report says MUST 3 was "not
     fixed for a hung one" and SHOULD 4 was "fixed, but only partly";
  3. "in a real browser the deadline always wins … the age path is verified by unit tests only" — disproved:
     returning to a tab that was hidden longer than the budget shows the age path immediately, before any
     deadline can fire.
- The phone-label claim is slightly overstated ("each card value names its column" — the state badge and the
  service name carry no label, though both describe themselves). Narrow the wording or label them.

## Evidence

- Gates: `pnpm lint`, `pnpm vitest run`, `pnpm build`, `go vet`, `gofmt -l`, `go test -count=1 ./...`.
- Mutation proofs for MUST-FIX 1 and SHOULD-FIX 3 and 2 (the deadline relationship), with real red output.
- Push, update PR #31, leave it open.

## Constraints

Comments under ~10% of lines. Read and write only inside this worktree. Do not touch `main` or another worktree.
**Do not redeploy, reload or restart any service.** The deployed gateway on `127.0.0.1:2104` is older than
`main`; read it if useful, never change it.
