# Console release 1 (`.claude/tasks/console-r1-spec.md`), read-only

## Decisions up front
- **Route shape follows the design, not the spec**: `GET /api/inferences/metrics/{service}` (design §9).
  The spec's `/api/inferences/{service}/metrics` also overlaps `GET /api/inferences/decisions/{id}`
  on `/api/inferences/decisions/metrics`, which Go's ServeMux refuses at registration.
- Service ids are `embedding`, `reranker`, `system-one`; the intended-fleet list (ocr/stt/tts) is static.
- Existing Decide/History/Templates/Eval stay, as a secondary "decision workbench" row; the five
  top-level entries are the design's.
- Polling: fleet 5 s, detail 10 s, metrics 30 s, all gated on `document.visibilityState`.

## Steps
- [x] 1. Gateway: metrics pass-through + tests (verbatim, unknown 404 shape, POST 405 Allow: GET).
- [x] 2. SPA: status vocabulary, visibility hook, metrics parser (fixed names, no percentile).
- [x] 3. SPA: Fleet (`/`), service detail tabs, thin Models/Activity/Jobs/Settings, lazy routes.
- [x] 4. Tests for each behaviour; one guard proven by mutation.
- [x] 5. Gates: lint, vitest, build (chunk sizes), go test; screenshots against real data.
- [ ] 6. Push, open the PR against main, leave it open.

## Review
- Gates clean: lint, 210 vitest, build (landing chunk 113.38 kB gzip, from 155.04 on main), go test.
- Mutations: 5 mutants, all caught. The hidden-tab guard is held twice (ours and React Query's
  focus manager); the fetch-count assertion bites only when both are removed, shown on purpose.
- Found in the screenshots, not the tests: `dita_worker_ops_total` counts DIP socket ops (mostly
  `readyz` probes), not HTTP requests. Relabelled; the Overview counts inferences from the
  `infer_duration` histogram's `_count`.
- Not done: dark mode (§13), Lighthouse (§14), per-container Dozzle deep link.

# Addendum: audit findings (`/mnt/data/workspaces/hardening-lane-b-addendum.md`), in priority order
Supersedes the Caddy `header_up` design and the "same wording" pre-check below.

## Decisions up front
- **One write guard on every POST** (store routes, retire, chat, embed, rerank, decide): refuse
  `Sec-Fetch-Site: cross-site` (403), require `Content-Type: application/json` (415), then the token
  when set (401). A client of the pass-through that sends no JSON content type is refused: stated in the doc.
- **The pre-check reports structured faults**, `{path, message}` in the zod dotted style. The fixture
  pins `error_type` and the first fault's path; the worker's `Refusal` carries the same `path`. No prose match.
- **Predictions keep their parsed answers** (`answers` on the record, beside the raw reply). A record
  without them is re-read under today's rules and the view says `answers_reparsed: true`.
- **Container user stays root, written down**: the deployed named volume holds root-owned 0640 files,
  so `USER 65532` alone would crash-loop the gateway. It needs a one-time chown, which is Dhira's call.
- **Two error shapes become one**: `{error, error_type}`, since every route and every worker speaks it.

## Steps
- [ ] 0. Gateway doc: drop `header_up`; token = programmatic; people = Caddy `basic_auth`/`forward_auth`.
- [ ] 1. Store: `MaxRecord` shared by reader and writer; `SetEscapeHTML(false)`; truncate on a failed
      append, poison if that fails; bounded evaluation name and class keys; read-only test.
- [ ] 2. Write guard on every POST + table test; `WriteUnavailable` without URL or transport error;
      compose and doc comments state what is deployed.
- [ ] 3. `GOMEMLIMIT=200MiB`; one-slot `/evaluations`; streaming `rows` decode stopping at MaxRows+1; limit 50.
- [ ] 4. Replay refuses a prediction (and a correction) pointing at nothing; stored answers; view errors visible.
- [ ] 5. `429 -> busy`.
- [ ] 6. Listings carry `usable`, `faults`, `retired`, `authoring_issues`; Decide uses the same verdict.
- [ ] 7. Text boundary in the shared fixture; worker-cases by path + error_type; startup deadline check.
- [ ] 8. A test for each surviving mutation.
- [ ] 9. Chat logs status and size; one error shape; retention and root-user decisions written down.
- [ ] 10. All gates again, mutation pass again, tree clean but for the change.

---

# Task: hardening lane B — the contract (orchestrator + worker + shared fixtures)

Order: `/mnt/data/workspaces/hardening-lane-b.md`. Branch `feat/hardening-contract`. No commit, no push.

## Decisions up front
- **The pre-check mirrors the worker, not `ValidateDraft`.** `CheckQuestions` in `decisions/worker.go`
  applies the worker's `_check_question` rules with the worker's exact messages. `ValidateDraft`
  (now requiring criteria, numeric scores) is an authoring rule: applying it at decide time would
  refuse every deployed template without criteria.
- **`worker-cases.json` pins exact refusal messages**, so "same wording" is a test, not a claim.
- **Retired is `410 Gone`, `error_type: "Retired"`.** Checked before the pre-check.
- **`FORMAT` stays `dita-decisions/1`**: `retirements.jsonl` is additive; a rollback ignores it.
- **Score levels are plain decimals for authoring** (regex), so Go, zod and Python cannot read
  `0x10`, `1e3` or `Infinity` differently; the pre-check keeps the worker's own `float()` reading.
- **Token lives in `inferences.Config`** (`INFERENCES_API_TOKEN`), checked in the router around the
  five mutating routes only; the proxied worker routes are unchanged.

## Steps
- [x] 1. Go: `CheckQuestions` + Decide pre-check (400, worker's body shape, `schema_invalid`).
- [x] 2. Go: retirements (store, replay, route, `retired` on listings, Decide refusal).
- [x] 3. Go: `ValidateDraft` criteria required + `MaxCriteria`; score options numeric ascending in range.
- [x] 4. `schema-cases.json`: criteria on every valid case, the new cases.
- [x] 5. `worker-cases.json` + Go adapter test + Python test.
- [x] 6. Token guard + config + tests both modes; gateway doc (Caddy `header_up`).
- [x] 7. Docs: worker README (criteria fallback, Known behaviour), dashboard doc (layout, routes),
      gateway doc (token), system-one doc (act_probability is a constant).
- [x] 8. Verify: go test/vet/gofmt, make doctor/py-verify/dip-verify, worker test/coverage/parity.
- [x] 9. Mutation pass over the new guards.

## Review
- **Gates**: `go test -race ./...` ok; `go vet`, `gofmt -l` clean; orchestrator coverage 75.4% (floor 68);
  `make doctor`, `make py-verify`, `make dip-verify` pass; worker `test` 64 ran OK (5 parity skips),
  `coverage` 99% (floor 98), `parity` 5/5 OK, worst max|dp| 1.70e-06.
- **Parity weights** read-only from `dita-system-one`'s export: this worktree has none, and a second
  export is a 2 GB torch job on a shared box. The engine's provenance check guards the pairing.
- **Mutation**: 23 mutants over the new guards (pre-check, retirement, token, criteria, score rule,
  both sides' wording and reply shape), all caught by a named test. One first run was a compile
  error, not a catch; re-run as a real mutant and caught.
- **Not verified**: the deployed gateway (no call made); the README's disk-at-90% and 5-level-score
  numbers are from the order, not re-measured here; lane A's zod against the new schema cases.

---

# Task: `services/inferences-embedding`

Order: `~/.hermes/tmp/dita-order-embedding.md`. Legend: `[ ]` todo · `[x]` done.

## Deviations from the order, decided up front
- **Branch and PR.** Work lives on `feat/inferences-embedding`, branched from `main`; the PR
  is opened from it and never merged here. `main` is not committed to.
- **Gemma weights come from `onnx-community/embeddinggemma-300m-ONNX`.** `google/embeddinggemma-300m`
  is gated (`gated: manual`) and the fetcher has no auth by design (no tokens in the repo).
- **The TEI surface lives on the worker's existing HTTP port** (the metrics server in
  `packages/pylibs/worker`), extended with service-supplied routes. No second server.
- **DIP `infer` refuses on this worker.** `InferResponse` is closed to `text`/`lines`; vectors
  do not fit without a spec change, which the order forbids. DIP carries the lifecycle.

## Steps
- [x] 1. `packages/pylibs/worker`: `ModelManager.run(work)` (engine under the lock, metrics
      recorded) and `Worker.routes` served on the HTTP port. Tests, floor held.
- [x] 2. `services/inferences-embedding`: pyproject, models.yaml (digests from real downloads),
      `onnx_embedder` engine (mean / last-token / graph pooling, Matryoshka, prefixes, token
      budgeted batches), tests.
- [x] 3. TEI routes: `POST /embed`, `GET /info`, `GET /health`, TEI error shapes and codes.
- [x] 4. Dockerfile, .dockerignore, Makefile, .coveragerc; image builds.
- [x] 5. `compose.yaml`: `proxy` network, no host ports, fixed name, healthcheck, mem cap,
      weights on a persisted volume.
- [x] 6. Docs: `docs/inferences/embedding/[1]technical-requirement.md`, README, the Hindsight env.

## Verification
- [x] Every file's sha256 in models.yaml verified by the real fetcher.
- [x] Each model's vectors checked against a reference implementation (sentence-transformers)
      where one can be run; the EmbeddingGemma card's published similarities where not.
- [x] Container up on `proxy`, `/embed` answered by name from another container.
- [x] Peak memory per model measured; cap chosen from it.
- [x] `make doctor`, `make test`, `make coverage`, `make py-verify`.
- [x] Mutation pass over the new tests.

## Review

- **Reference agreement** (max abs diff, fp32): nomic 1.7e-7 / 2.1e-7 / 2.7e-7 (query, document,
  256-d) against sentence-transformers 3.4.1; Qwen3 6.6e-7 / 4.8e-7 / 9.7e-7 against
  sentence-transformers 5; EmbeddingGemma 1.8e-7 against its card's published similarities.
- **Memory**, peak RSS at the shipped limits: nomic 2.2 GB, EmbeddingGemma 1.3 GB, Qwen3 5.2 GB
  (5197 MiB in the 6g container, no OOM). Qwen3 at 8000 tokens: 12.3 GB, host OOM kill.
- **Rejected on measurement**: Qwen3 int8 (cosine 0.48-0.87 to fp32); the ONNX Runtime arena
  (3.7 GB held after one long batch).
- **Mutation**: 20 on the service, 11 on the worker seam; all caught after two weak tests were
  fixed (truncation state, float spelling). One equivalent mutant discarded.
- **Found on the way**: adding a second workspace member broke OCR's image build (`uv export
  --locked` saw a lock with a member the deps stage could not). Fixed in its Dockerfile, ordered
  before the service commit. OCR's `services/inferences-ocr/.dockerignore` is inert (BuildKit
  reads `<Dockerfile>.dockerignore` or the context root's), proven with a two-file experiment;
  left alone as out of scope.
- **Not verified**: model quality beyond the reference agreement; Hindsight actually cut over
  (its compose was not touched); the orchestrator driving this worker (it has no client yet).

---

# Task: `services/inferences-reranker`

Order: `~/.hermes/tmp/dita-order-reranker.md`. Branch `feat/inferences-reranker`, stacked on
`feat/inferences-embedding` (PR #11): it needs the worker's route seam, not yet on `main`.

## Steps
- [x] 1. Verify TEI's `/rerank` schema in its source (commit `29ccc53`) and Hindsight's client.
- [x] 2. Find an ONNX export; prove it against the official model in fp32 before trusting it.
- [x] 3. Extract what two services share into `packages/pylibs/textinfer`; embedding composes it.
- [x] 4. `services/inferences-reranker`: models.yaml, `onnx_cross_encoder`, TEI routes, tests.
- [x] 5. Dockerfile and `compose.yaml` on `proxy`; mem cap from measurement.
- [x] 6. Docs under `docs/inferences/reranker/`, the two-stack Hindsight cutover.

## Review
- **Export**: `shawnw3i/…-seq-cls-ONNX` (fp16): ids identical to the reference, scores within
  4.94e-4, every ranking equal. `n24q02m/…-ONNX` int8: up to 0.96 off, rejected.
- **Found on the way**: a reload OOM-killed at 4 GB because glibc kept the unloaded model's
  1.9 GB of freed heap. Fixed in the worker package (`malloc_trim` after release; 1900 → 86 MB),
  which every worker gets. Cap set at 5g: at 4g the cgroup sat at its ceiling.
- **Mutation**: 23 on the service, 15 on `textinfer`, 4 on the trim; all caught after three
  weak tests were fixed (session options, refuse-not-wait, and one meaningless test deleted).
- **Not verified**: Hindsight actually cut over; the orchestrator driving it; ranking quality
  beyond agreement with the official model.

---

# Task: the inferences gateway in `services/dita-orchestrator`

Order: `~/.hermes/tmp/dita-order-inferences-gateway.md`. Branch `feat/inferences-gateway` from
`origin/main`. Not pushed: the run said do NOT push, which overrides the order's push-and-PR.

## Steps
- [x] 1. Verify how the orchestrator runs: it did not; its only stack is off `proxy`.
- [x] 2. `handler/inferences`: embed, rerank, decide, workers, health; ReverseProxy pass-through.
- [x] 3. Wire it into `router.go` and `serveRest.go`; server write deadline above the timeout.
- [x] 4. Unit Makefile (test, coverage, build, image) and a Go allowance in `go-verify`.
- [x] 5. `compose.yaml` on `proxy`, 2104 on loopback only.
- [x] 6. Docs under `docs/inferences/gateway/`.

## Verification
- [x] Live: 1024-dimension embedding through the gateway, byte-identical to a direct call.
- [x] Live: `decide` answers 503 naming `inferences-system-one`; `/workers` reports all three.
- [x] Mutation pass over the gateway tests.
- [x] `make doctor`, `make test`, `make coverage`, `make py-verify`, `make build`, in a clean worktree.

## Review
- **Network**: option 1, the orchestrator on `proxy`. Proven: the names do not resolve from the
  host, and do from the gateway.
- **Found on the way**: w-tools' 30 s server write deadline would have cut off the reranker; a
  missing container is SERVFAIL, not NXDOMAIN, on Docker's resolver; a worker at its connection
  cap reads as "server closed idle connection"; Hindsight's keep-alive pool filled all eight of
  the embedding worker's HTTP slots; the reranker's DIP socket file had vanished from `run/`,
  which is why its healthcheck said unhealthy while its HTTP answered.
- **Not ours, but it fails `make test`**: the uncommitted `inferences-embedding/models.yaml`
  change makes Qwen the default, and `test_the_default_is_the_english_model_and_says_so` fails.
- **Not verified**: `/decide` against a real system-one worker; the dashboard; auth.

---

# Task: `services/inferences-dashboard`

Order: `~/.hermes/tmp/dita-order-inferences-dashboard.md`. Requirement:
`~/second-brain/ai/dita/specs/inferences-dashboard-requirements.md` (followed, not redesigned).
Branch `feat/inferences-dashboard`, stacked on `feat/inferences-gateway` (PR #14). Not pushed: the
run said do NOT push, which overrides the order's push-and-PR.

## Decisions taken up front
- **The worker's answer shape is unsettled and Rin's branch has no commits**, so the orchestrator
  owns a normalised decision contract and one adapter maps the worker's reply into it. The stub
  speaks the documented shape; the raw reply is stored "as returned".
- **The store is append-only JSONL files** (stdlib, CGO-free): templates, predictions, corrections
  and evaluations, each written once. A second correction is refused by the store itself.
- **Validation twice, proven equal**: Go and zod each implement the rules; one shared case file
  (`specs/decisions/schema-cases.json`) runs through both suites.
- **Majors as the order pins them**: React Router 7 and ESLint 9, not the newer 8 and 10.
  TypeScript 6.0 because typescript-eslint supports `<6.1`.

## Steps
- [x] 1. Root plumbing: a marker-based JS unit class, nodejs + pnpm in `.tool-versions`, doctor.
- [x] 2. Orchestrator: store, schema templates, decisions, corrections, evaluations, stats routes.
- [x] 3. SPA: decide, templates, history, eval; the recommendation contract; failure states.
- [x] 4. Build guard: `dist/` secret grep that fails the build.
- [x] 5. Stub worker, Caddy block (loopback, full header set), e2e over the real store.
- [x] 6. Docs and evidence screenshots under `docs/inferences/dashboard/`.

## Review
- **Built against a labelled stub**: Rin's branch has no commits, so `decisions/worker.go` holds the
  documented stub contract and the e2e runs `e2e/stub-system-one.mjs`. Every screenshot says STUBBED.
- **The pair holds**: the e2e reloads and reads the correction back from the store; the Go suite
  reopens the directory and finds exactly one correction line after a refused second one.
- **Mutation**: 15 of 16 on the store and routes (the survivor is the fsync, unobservable), 18 on the
  gateway earlier. Two weak tests found and fixed (ECE binning, a non-compiling mutant rerun).
- **Found on the way**: pnpm 12's minimum release age refused `prettier@3.9.9`; pinned 3.9.8 rather
  than keep the exemption pnpm wrote. ESLint 9 is reported deprecated; kept, as the order pins it.
  Playwright's teardown left the Caddy container running; a global teardown removes it.
- **Not verified**: the real `inferences-system-one`; auth (it does not exist); latency p50/p95 and a
  calibration drift line (need a metrics backend).

# Task: hardening lane A — `services/inferences-dashboard`
Order: `/mnt/data/workspaces/hardening-lane-a.md`. Branch `feat/hardening-dashboard` off `c8b6d04`.
No commit, no push. Legend: `[ ]` todo · `[x]` done.

Baseline before any change: `pnpm vitest run --maxWorkers=2` → 12 files, 96 tests, all green.

## Decisions, up front
- **`criteria` is enforced in the `superRefine`, not by `z.string()`**, so a missing one is reported
  at `questions.N.criteria` beside every other fault (zod 4 would otherwise skip the rules).
- ~~Usable on Decide = passes `draftSchema`.~~ Superseded by the addendum: usability is the server's
  verdict (`usable`, `faults`, `authoring_issues`); `draftSchema` is the fallback for an older orchestrator.
- **Score rule paths:** non-numeric or not rising → `questions.N.options`; outside a valid range →
  `questions.N.range` (the runtime reports the contradiction on the range). Only checked on a valid
  range. Lane B owns the shared cases; if its paths differ, this is the line to move.
- **Retire** is behind `api.retire`; `retired` absent is read as `false` until lane B lands.

## Addendum (`hardening-lane-a-addendum.md`)
- [x] A1. `src/lib/usability.ts`: the server's `usable`/`faults`/`authoring_issues`, editor rules as fallback.
- [x] A2. Decide: usable-but-outdated selectable and marked; Templates marks authoring issues on load.
- [x] A3. 401 / 403 / 415 as sentences; `problem+json` and `{error}` both read (`toProblem`).
- [x] A4. Caddyfile comment and the doc state the LAN deployment; `limit=50` already at the ceiling.
- [x] A5. Audit finding 4, dashboard side: `answers: null` shown as unreadable, no correction form.
