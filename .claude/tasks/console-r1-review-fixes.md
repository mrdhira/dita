# Console release 1 — fixes from the adversarial review

An independent reviewer attacked PR #31 read-only. Its full report is at `/tmp/console_review.md` (read it
first — it has the proof commands). It reproduced all five of your mutation claims, confirmed the bundle
numbers to ±0.01 kB, confirmed the route is read-only and that no control affordance exists. It also found
real problems, listed here as the work. **Fix all of MUST-FIX and SHOULD-FIX, and every NIT marked "do".**
Every fix needs a test that goes red when the fix is removed, and you must prove *these* guards by mutation
the same way you proved the first five — the reviewer showed that mutants E and F survived the current suite.

## MUST-FIX

**1. The console invents a "no model" state (the core rule of this design: §13).**
`ModelsTab.tsx:26` branches only on `report?.info`. `info` is null in many cases the tab then describes as
"no model is resident… requests fail until one is loaded": while `/workers` is still pending, when `/workers`
failed, when the gateway's second probe (`/info`) failed after `/health` said 200 (`workers.go:55` sets
`ready` from `/health` alone), and for every state with no probe answer — `busy`, `timeout`, `unreachable`.
A `busy` worker certainly has a model resident.
**Rule:** claim "no model resident" **only** when the state is `no_model`. Otherwise the resident model is
*unknown*, and the UI must say so — never the sentence about requests failing. Same fix in
`FleetPage.tsx:58` (`deployed ? "none resident" : "—"`), which today prints "none resident" beside
"ready · serving" (a self-contradicting row) and beside "not reported" when the gateway is down.
Add tests for: `ready` with null info, `/workers` pending, `/workers` failed, `busy`, and the honest
`no_model` case, and prove one by mutation.

**2. The only guard against "a stale screen reads as live" has no test.**
`AsOf.tsx:10` carries `· the latest refresh failed; this is the last good answer`. Mutant E deleted that line
and the suite still reported 210 passed. Add a test that fails without it. Then go further, because a green
`● ready` for a worker unseen for 60 s still reads as live: when the latest refresh failed, mark the data
visibly stale — keep the last values, but make the stale state unmissable on the row itself (a `stale`
treatment on the state badge, plus the message line), not only in the small grey line. Test the badge's
stale treatment too.

## SHOULD-FIX

**3. Any 200 that is not an exposition page renders as "an idle worker".**
`api/client.ts:188` accepts any 2xx body; `readWorkerMetrics` then finds nothing and `MetricsTab.tsx:70`
prints "none since the last restart" for every counter with no error banner. An SPA fallback or a misrouted
proxy would therefore look like a perfectly quiet worker. Refuse a 200 whose body yields zero recognised
series (or whose content type is not the exposition type) and surface it as an error instead. Test with an
HTML body and keep a green path beside it.

**4. `metrics.go` has four surviving mutants and one vacuous fixture.**
Applied one at a time, `go test -count=1 ./handler/...` passed for all four: forwarding a query string
upstream (`metrics.go:54`), removing the 1 MiB bound (`metrics.go:64`), always answering 200
(`metrics.go:48`), and stamping the exposition content type on error pages (`metrics.go:42`). The last two
together turn a worker's 404 or 500 into finding 3's quiet worker. The bound also truncates silently and still
serves a verbatim 200.
- The fake `upstream` records only `r.URL.Path` (`gateway_test.go:38`), so the `?path=/info` case reads as a
  "query is dropped" test but proves nothing. Record the method and `r.URL.RawQuery` and assert both.
- A page over the bound must not be served as a complete 200: refuse it (502, with the reason), and test that.
- Pin: a worker's non-200 stays non-200, and an error body never carries the exposition content type.

**5. Correct the screenshot provenance claim — it is false.** *(I corrected this in the PR body myself; verify
it reads true and keep it that way.)*
The PR body says the screenshots came from "the running gateway, 127.0.0.1:2104 (deployed from `main`,
untouched)". It was not built from `main`: `GET http://127.0.0.1:2104/api/inferences/nope` answers
`text/plain` "404 page not found", while `main`'s own unchanged test (`router_test.go:218`) requires the JSON
`{error, error_type:"NotFound"}` shape. The running image predates that work. So screenshots 2 and 3b show a
404 that `main` would not produce, and the rollout note describes a stale image rather than `main`. Re-label
them honestly. **Do not redeploy anything** — the deployment drift is Dhira's decision, and it belongs in the
PR as a finding.

**6. `aria-live="polite"` on a line that changes every 5 s** (`AsOf.tsx:7`): a screen reader announces
"as of HH:MM:SS" forever. The live region must carry only the paused and stale transitions, never the clock.

## NIT — all marked "do"

- One vocabulary per state on one screen: the header strip (`Layout.tsx:56`) prints `not running` where Fleet
  prints `stopped`; use `describeState` and update `Layout.test.tsx`.
- The "last three rows are the intended fleet" footnote is wrong once the gateway reports an unknown worker
  (extra rows render after them), and its reason is sloppy: OCR has no HTTP surface (DIP only), while STT and
  TTS have no code at all (`services/inferences-stt/.gitkeep`). Say it precisely, and only when true.
- `resident for` prints `0.0 s` for a worker with no model; show nothing instead.
- The Overview's `/metrics` figures (RAM, uptime, counts) have no "as of" of their own — they come from a
  separate 30 s query. Design §7 asks for one next to every timestamp.
- The Models tab lists every `/info` key inline and then repeats them as raw JSON behind the expander:
  identity inline, everything else behind the expander only (§13).
- Fleet has no `resident-for` column, which design §4 lists. Add it (or add it to the not-done list, and say
  which and why).
- Untested behaviour that the reviewer listed explicitly: "not reported", "none resident", "does not report
  this service", "There is no service", "No model is resident", "No model identity", "reported by the
  gateway", "Waiting for the worker", "latest refresh failed", and the service page's 10 s and metrics' 30 s
  polling plus their pause while hidden (mutant F: metrics at 1 s survived at 210 passed).

## Evidence

- Re-run every gate: `pnpm lint`, `pnpm vitest run`, `pnpm build`, `go vet`, `gofmt -l`, `go test -count=1 ./...`.
- Mutation proofs for the new guards — at least the stale warning, the non-exposition 200, and a Go mutant
  from finding 4 — with the real red and green output.
- **New screenshots** where the UI changed (the stale treatment, the dashes, the resident-for column). Real
  data only, and label each one with which gateway it came from and whether that gateway is `main`: the
  deployment on `:2104` is stale, so say so rather than implying otherwise. If a state cannot be produced,
  say so.
- Update the PR body: a short "what the independent review found and what I did" section, the corrected
  provenance, and an honest list of what is still not verified (keep the existing one and extend it).

## Constraints

Same branch `feat/inferences-console-r1`, same worktree. Do not merge. Do not touch another worktree or
`main`. Do not redeploy, reload or restart any service. Comments under ~10% of lines.
