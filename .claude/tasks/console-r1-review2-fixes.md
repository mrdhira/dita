# Console release 1 — round two: what the re-verification found

The independent reviewer re-checked its own findings at `3fa6a80` (its report is copied to
`.claude/review/report.md` — read it, it has the probes). **Every original finding is fixed and it proved that
itself.** It also disproved three of your claims and found two new MUST-FIX problems. This round is those.

Fix everything below, and correct the PR body's disproved claims — a PR that still contains a statement a
reviewer disproved is worse than one that admits it. Keep working in this worktree on
`feat/inferences-console-r1`. **Do not merge.**

## MUST-FIX

**A. A refresh that HANGS never goes stale; the row stays green `● ready` indefinitely.**
The stale treatment keys only on `workers.isError` (`FleetPage.tsx:118`, `ServicePage.tsx:64`), while
`request()` (`api/client.ts:126-148`) passes no `signal` and sets no timeout — so a request that never answers
leaves the query "fetching" forever, `isError` never becomes true, and the badge stays emerald with no `· stale`
and no note. The reviewer held a hung `/workers` for 180 s and the row stayed green. Realistic: `docker pause`,
a wedged orchestrator (Caddy's `reverse_proxy` sets no response timeout), or a laptop's network dropping without
a RST.
Fix: make staleness a function of **age as well as failure** (stale when `Date.now() - dataUpdatedAt` exceeds
your interval budget with slack, while polling), and pass React Query's `signal` combined with
`AbortSignal.timeout(...)` into `fetch` so a hung request actually ends. Test with a never-resolving fetch after
one good answer, and prove the test by mutation.

**B. The fifth "no model" path: a reasonless 503 on `/workers` or `/metrics` still reads "The worker has no
model loaded".**
`lib/errors.ts:28-31` keeps that mapping and its docstring (`errors.ts:3-5`) justifies it — but the justification
is only true for the pass-through inference routes (Try-it, decide). The Fleet banner uses the same function for
`/workers`, which is the gateway's own route and always answers 200, and the Metrics banner uses it for
`/metrics`, where a worker only ever answers 200 (`worker/metrics.py:329-339`). So on those routes a reasonless
503 comes from something in front, and the console blames the model. Latent today (the deployed Caddy answers
502, and Vite's proxy answers 502) but it fires the moment any hop answers 503.
Fix: only a pass-through inference route may read a reasonless 503 as "no model"; `/workers` and `/metrics` need
their own honest wording (the gateway, or something in front of it, answered 503). Correct the docstring so it
is true for every route that uses it, and test both routes.

## SHOULD-FIX

**C. The non-exposition guard is defeated by a real Python `prometheus_client` page and by a cut page.** The PR
body currently claims "Either one is an error banner, never 'none since the last restart'" — the reviewer
disproved that: a default-registry page (only `process_resident_memory_bytes`, `process_cpu_seconds_total`, …)
gives four "none since the last restart" with no banner, and a page cut after its first series gives five. Your
"another exporter's page" fixture avoided the `process_*` names, so its test passed for a narrower reason than
its name claims.
Fix: `RECOGNISED` must require `dita_worker_uptime_seconds`, which the worker page always carries, and a family
with no `# TYPE` line must read as **unknown**, not as "none since the last restart" — §13 applied to metrics.
Test with a `process_*`-only page and with a cut page.

**D. A 502 on `/workers` is blamed on the worker, and that one is reachable today.** With the orchestrator down,
Caddy and Vite both answer an empty 502, and Fleet says "The worker answered in a shape it was not asked for" —
no worker was involved. The new `NotMetrics` refusal sits in the same branch.
Fix: `/workers` and `/metrics` get their own 502 title; keep the worker-shaped title only for a 502 that carries
the gateway's JSON with a `worker` field. Test both.

**E. Three stale-treatment mutants survive — close them with tests.** (1) Fleet keeps showing a failed
`/metrics` read's `resident-for` (`FleetPage.tsx:49`), so the PR's claim that the figures turn to `—` after 30 s
without an answer has no test. (2) Nothing tests that a gateway-reported unknown worker's row goes stale
(`extra` rows get `staleSince={null}`). (3) `OverviewTab.tsx:55` `failed={query.isError}` → `failed={false}`
survives: the new `/metrics` "as of" never admits its refresh failed.

## NIT

- **Remove the disproved claims from the PR body** and replace them with what is true, including a short
  "second review" section: what it found, what you did, what it disproved of yours. Keep the honest
  not-verified list and extend it.
- At 320 px the resident-for value carries no label (screenshot 1b: `32 h 53 min`, `—`, and three bare dashes on
  not-deployed cards), so a reader cannot tell it from uptime. Add the label in the card layout.
- A "not done" line in the PR body is now out of date: it says `loading` has no source without scraping every
  worker's metrics on the 5 s tick, but Fleet now scrapes `/metrics` every 30 s and `dita_worker_model_loading`
  is in that page. Either implement `loading` or correct the line — say which you chose.
- Disclose, do not fix: `main`'s `classify()` maps a malformed `/metrics` body (a `Content-Length` that does not
  match) to `503 busy`, so a truncated page is described as a connection limit. It predates this PR; record it as
  a finding for a later change rather than touching the classifier now.

## Evidence

- Gates: `pnpm lint`, `pnpm vitest run`, `pnpm build`, `go vet`, `gofmt -l`, `go test -count=1 ./...`.
- Mutation proofs for A, B, C and D, with the real red output.
- New screenshots only where the UI changed (the hung-refresh stale treatment, the 320 px label). Real data, and
  say which gateway each came from.
- Push to `feat/inferences-console-r1` and update PR #31. It stays open for Dhira.

## Constraints

Comments under ~10% of lines. Read only files inside this worktree — do not read or write outside it, and do not
touch `main` or another worktree. **Do not redeploy, reload or restart any service.** The deployed gateway on
`127.0.0.1:2104` is older than `main`: read it if useful, never change it.
