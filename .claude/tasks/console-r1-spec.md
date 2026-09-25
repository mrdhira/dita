# Console release 1 — the read-only shell that is actually useful

Work order for Claude Code. Read the authority before writing anything:

1. **The design**: `docs/inferences/dashboard/[2]internal-console-design.md`. It is on branch
   `docs/inferences-console-design`, **not merged** — read it with
   `git show docs/inferences-console-design:docs/inferences/dashboard/\[2\]internal-console-design.md`
   (quote or escape the brackets). Release 1 is §4, §5, §6, §7, §8, §9, §13 and §14 of it. Where this file and
   the design disagree, the design wins and you should say so in the PR.
2. **The existing dashboard**: `services/inferences-dashboard/src/{pages,components,api}` and its tests.
   Follow its patterns, its styling and its file layout. Its api client already targets `/api/inferences`
   (same origin, through Caddy).

## What release 1 is

Observe only. Nothing in it may load, unload, restart or reconfigure anything, and no button may exist where
the server capability does not.

### 1. Fleet page (the new landing, route `/`)

- Data: `GET /api/inferences/workers` →
  `{workers:[{name,url,state,health_status,info,error?}]}` with
  `state ∈ ready|no_model|unhealthy|not_running|busy|timeout|unreachable|client_gone`.
- One row per service: name, **state word**, resident model (`info.model_id`, short `model_sha`), the last
  error if there is one, and a link to `/services/:id`.
- Render every state as **word + colour token + shape**, never colour alone, with the honest sentence from
  design §6 (e.g. `no model` = "alive, not serving; requests fail until a model is loaded").
- The gateway reports exactly three services (`INFERENCES_{EMBEDDING,RERANKER,SYSTEM_ONE}_URL`). The intended
  fleet also has `inferences-ocr`, `inferences-stt`, `inferences-tts`: show them as rows with state
  **`not deployed`** and the reason — OCR "no HTTP surface: DIP only", STT/TTS "no code". This list is static
  frontend knowledge, not API data; label it as the intended fleet and give those rows no actions.
- Poll every 5 s **while the tab is visible**, stop when it is hidden (`visibilitychange`), and show
  `as of HH:MM:SS` next to the data. A stale screen must never read as live.

### 2. Service detail, route `/services/:id`, tabs Overview · Models · Metrics · Logs · Try it

- **Overview**: the state with its message; the resident model's identity from `info` (`model_id`, `model_sha`,
  `model_dtype`, `dimensions`/`max_input_length`, `max_concurrent_requests`, `max_batch_tokens`); one line of
  blast radius — embedding and reranker are what Hindsight recall and consolidation use, system-one serves
  `/decide`.
- **Models**: the resident model in full. A registry listing is **not available** (`GET /{service}/models`
  does not exist). Do not invent one and do not fake a list: show the resident model plus a clearly-labelled
  empty state saying the full registry arrives with the read route in a later layer (design §5, §10).
- **Metrics**: read `GET /api/inferences/{service}/metrics` (you add it, see 3). Parse only these names:
  `dita_worker_ops_total`, `dita_worker_errors_total`, `dita_worker_model_loads_total`,
  `dita_worker_model_evictions_total`, `dita_worker_model_resident`, `dita_worker_model_resident_seconds`,
  `dita_worker_model_loading`, `dita_worker_uptime_seconds`, `process_resident_memory_bytes`,
  `process_resident_memory_peak_bytes`, `process_cpu_seconds_total`, and the histograms
  `dita_worker_load_duration_seconds` and `dita_worker_infer_duration_seconds` (show their **bucket counts**
  and `_sum`/`_count`). **Never compute a percentile from buckets** (design §9). Label counters "since the last
  restart". Poll every 30 s.
- **Logs**: link out to Dozzle (`https://dozzle.home.arpa`) in a new tab, plus the `docker logs <container>`
  equivalent for a terminal. Build no log viewer and embed no iframe (design §8).
- **Try it**: a form per service posting the worker's real request through the gateway (`/embed`, `/rerank`,
  `/decide`) with the fields each accepts; show the raw response, `x-compute-time` when present, and any
  refusal **verbatim** with its `error_type`. This is the tab that earns the console (design §5).

### 3. Gateway — one read-only route, `services/dita-orchestrator`

- `GET /api/inferences/{service}/metrics` for `service ∈ {embedding, reranker, system-one}`: reverse-proxy the
  worker's `/metrics` body **verbatim** with `Content-Type: text/plain; version=0.0.4; charset=utf-8`.
  An unknown service answers `404` in the standard `{error, error_type}` shape; a `POST` answers `405` with
  `Allow: GET`. It must not become a generic path proxy (design §9, §15).
- Tests in the gateway's own style: the body comes through verbatim, the unknown service is the one error
  shape, and `POST` is refused.
- **Do not** add load/unload/models routes. That is release 2 and needs worker-side routes first (design §10).

### 4. Navigation shell

The five top-level entries of design §4 — Fleet, Models, Activity, Jobs, Settings — with **Fleet** implemented
and the other four as thin pages that say plainly what they will hold and what is missing. No fake content,
no invented numbers. Services are rows, never navigation entries.

### 5. Tests

Every new behaviour gets a test that fails when it is removed:

- Fleet: each state renders its word (not colour alone); a `not deployed` row shows its reason and has no
  action; polling stops when the document is hidden.
- Service page: the Metrics tab shows buckets and prints no percentile; the Models tab shows the honest empty
  state; a refusal is displayed verbatim.
- Go: the three cases in §3.
- **Prove at least one guard by mutation**: remove it, paste the real red output, restore it, paste the green.

### 6. Evidence for the PR

- `pnpm lint`, `pnpm vitest run`, `pnpm build` (paste the built chunk sizes) and `go test ./...` — clean, raw
  output, not a description of it.
- **Screenshots against real data**: build the SPA, serve it locally with the gateway at `127.0.0.1:2104` as
  the proxy target, and capture Fleet, one service Overview, the Metrics tab and a Try-it refusal. Use the
  repo's Playwright setup. Real screenshots only — if a state cannot be produced, say so instead of staging it.

## Constraints

- Comments under ~10% of lines: no restating the code, no banners, no "how we got here".
- No new runtime dependency unless the design demands one. It does not.
- Do not touch other services. Do not merge anything. Push the branch and open a PR against `main`, open, for
  Dhira to review.

## Deliverable in your reply

The PR URL, the gate outputs, the mutation proof, and what you could not verify.
