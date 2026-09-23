---
type: technical-design
status: in-review
owner: Dhira Wigata
product: dita
prd: n/a — internal infrastructure
date: 2026-09-24
tags:
  - dita
  - inferences
  - gateway
  - orchestrator
  - technical-design
---

# Technical Design — the inferences gateway in services/dita-orchestrator

> House style that applies here: **stdlib-first, CGO-free**, and the shape the inference
> workers set for their consumers. The gateway is `net/http/httputil.ReverseProxy` and the
> standard library; the orchestrator's existing w-tools server carries the routes. No new
> dependency.

## Context

Each inference worker owns a private data surface and publishes no port:

| worker | surface | reachable from |
| --- | --- | --- |
| `inferences-embedding` | `POST /embed`, `/info`, `/health`, `/metrics` on `:8080` | containers on `proxy`, by name |
| `inferences-reranker` | `POST /rerank`, `/info`, `/health`, `/metrics` on `:8080` | containers on `proxy`, by name |
| `inferences-ocr` | DIP on `/run/dita/inferences-ocr.sock` | processes sharing `run/` |
| `inferences-system-one` | `POST /decide`, `/info`, `/health`, `/metrics` | not merged; not running |

That is the rule working as intended — nothing is on the LAN — but it also means a host
process or a browser reaches none of them. The dashboard needs one stable surface. The
orchestrator already runs a REST server on `:2104`; the gateway is new routes on it.

What forced the design:

- **The consumers depend on the workers' exact contracts.** Hindsight speaks TEI to
  `/embed` and `/rerank`. A gateway that reshaped a body or a status would break it.
- **The reranker is slow on purpose.** Up to ~85 s for a 32-pair batch under load, so the
  usual 30 s client and server defaults are wrong here.
- **Name resolution is the crux.** The workers resolve only on the `proxy` Docker network.
  The orchestrator was not on it — nor, on this box, running at all.

## Goals and non-goals

**Goals**

- One prefix, `/api/inferences`, that a dashboard can proxy as a single path.
- Pass-through: the request body forwarded byte for byte, the worker's status, headers and
  body returned unchanged, 4xx included.
- A worker that does not answer yields a 503 (504 on timeout) whose JSON names the worker,
  its URL and why.
- `GET /workers`: every worker's `/health` and `/info` in one document, "not running"
  included.
- Worker URLs and the timeout from the environment, so compose can re-point them.

**Non-goals**

- **Auth.** The orchestrator has none today, and a definition of "user" on a single-account
  box is Dhira's to make. The gap is stated, not papered over: see Security.
- OCR. It speaks DIP and has no `/info` or `/health` over HTTP; fronting it means a DIP
  client in the orchestrator (`packages/golibs/dip` exists) and is a follow-up.
- Changing any worker, and publishing any worker port.

## Design

```
   host process / Caddy             dita-orchestrator-rest (Go)             proxy network
  ┌──────────────────┐   :2104    ┌────────────────────────────────┐
  │ curl, dashboard  │───────────►│ /api/v1/...        (unchanged) │
  └──────────────────┘ 127.0.0.1  │ /api/inferences/embed   ───────┼──► inferences-embedding:8080/embed
                                  │ /api/inferences/rerank  ───────┼──► inferences-reranker:8080/rerank
                                  │ /api/inferences/decide  ───────┼──► inferences-system-one:8080/decide
                                  │ /api/inferences/workers ───────┼──► each /health, /info
                                  │ /api/inferences/health  (self) │
                                  └────────────────────────────────┘
```

### Routes

| route | upstream | notes |
| --- | --- | --- |
| `POST /api/inferences/embed` | `inferences-embedding:8080/embed` | TEI `/embed`, unchanged |
| `POST /api/inferences/rerank` | `inferences-reranker:8080/rerank` | TEI `/rerank`, unchanged |
| `POST /api/inferences/decide` | `inferences-system-one:8080/decide` | 503 until that worker runs |
| `GET /api/inferences/workers` | each worker's `/health` and `/info` | always 200; the document is the report |
| `GET /api/inferences/health` | — | the gateway's own liveness, `{"status":"ok"}` |

Only the listed method is routed: `GET /api/inferences/embed` is not proxied.

### Pass-through

`httputil.ReverseProxy` does the forwarding: the request body streams through unread, the
response's status, headers and body come back as the worker wrote them, and only hop-by-hop
headers are dropped. The upstream path is fixed per route, never derived from the inbound
path. Two adjustments, both invisible to a caller:

- **A body without `Content-Length` is buffered first** (up to 8 MiB) so the worker gets
  one: the workers refuse such a body with 411. A body with a length streams untouched and
  the worker applies its own 2 MiB cap, and its own 413.
- **Headers pass both ways**: `x-model-id`, `x-compute-time` and `Retry-After` reach the
  caller, which Hindsight's retry logic reads.

### When no worker answers

The gateway speaks only then, in TEI's error shape plus three fields:

```json
{"error": "inferences-system-one is not running: nothing answers at http://inferences-system-one:8080 (dial tcp: lookup inferences-system-one on 127.0.0.11:53: server misbehaving)",
 "error_type": "Unhealthy", "worker": "inferences-system-one",
 "url": "http://inferences-system-one:8080", "reason": "not_running"}
```

| reason | status | what happened |
| --- | --- | --- |
| `not_running` | 503 | the name does not resolve, or nothing listens on the port |
| `busy` | 503 | the worker accepted the connection and closed it without answering |
| `timeout` | 504 | no response within `INFERENCES_TIMEOUT` |
| `unreachable` | 503 | anything else, with the error verbatim |

Two findings shaped that table, both from running it rather than reading about it:

- **Docker's resolver answers SERVFAIL, not NXDOMAIN, for a missing container.** On `proxy`,
  a lookup of `inferences-system-one` returns "server misbehaving" (the embedded resolver at
  `127.0.0.11` forwards the unknown name upstream). Treating only NXDOMAIN as "not running"
  reported it as `unreachable`. Every DNS failure is now `not_running`, with the resolver's
  words kept in the message.
- **A worker at its connection cap looks like "server closed idle connection".** The
  workers' HTTP port holds eight connections and closes the ninth with nothing sent. Go's
  transport reports that as `http: server closed idle connection`, an unexported error; it is
  matched by its text and reported as `busy`, because "not running" would send someone to
  restart a healthy worker.

### Timeouts

| setting | value | why |
| --- | --- | --- |
| `INFERENCES_TIMEOUT` | 120 s default | the reranker takes up to ~85 s under load |
| server write deadline | timeout + 15 s | w-tools' default of 30 s would cut off a slow answer first |
| dial | 5 s | a worker that is up accepts at once |
| `/workers` probes | 5 s each, in parallel | `/info` and `/health` never wait on a model lock |

### Connection hygiene

Every open connection holds one of a worker's eight HTTP slots, idle or not, until the
worker's 10 s idle timeout. The proxy keeps at most two idle connections per worker and
drops them after 5 s, before the worker would; the `/workers` probes do not keep connections
at all. That keeps the gateway's share of a worker's slots to its in-flight requests plus
at most two idle connections.

### Configuration

| env | default |
| --- | --- |
| `INFERENCES_EMBEDDING_URL` | `http://inferences-embedding:8080` |
| `INFERENCES_RERANKER_URL` | `http://inferences-reranker:8080` |
| `INFERENCES_SYSTEM_ONE_URL` | `http://inferences-system-one:8080` |
| `INFERENCES_TIMEOUT` | `120s` (a Go duration; `120` without a unit is refused) |

A URL without an `http(s)` scheme and a host, or a timeout that does not parse or is not
positive, stops `serveRest` with the variable's name rather than being guessed at.

## The network question

**Found:** the orchestrator was not running on this box. Its only definition,
`deployment/docker-compose.yml`, puts it on that project's default network and publishes
`2104` on all interfaces. From there, `inferences-embedding` does not resolve — and from the
host it does not either:

```
$ curl -sS http://inferences-embedding:8080/health
curl: (6) Could not resolve host: inferences-embedding
```

**Chosen: option 1**, `services/dita-orchestrator/compose.yaml` on the external `proxy`
network. It keeps the no-published-ports rule for the workers intact and is the shape the
embedding and reranker services already assume for their consumers. The loopback fallback
was not needed.

**One decision of this change's own:** `2104` is published on **loopback only**
(`127.0.0.1:2104`), not on all interfaces as the deployment stack does. The gateway has no
auth; a host process may reach it, the LAN may not, and Caddy reaches it by name on `proxy`.

The stacks are alternatives: both use the container name `dita-orchestrator-rest`, so
running both fails loudly rather than splitting the port.

## Verification

Live, against the containers running on this box, through the gateway on `127.0.0.1:2104`:

| check | result |
| --- | --- |
| `POST /api/inferences/embed` | 200, `x-model-id: qwen3-embedding-0.6b`, one vector of **1024** dimensions |
| the same request to `inferences-embedding:8080/embed` from a container on `proxy` | 1024 dimensions; **byte-identical** response (same sha256) |
| `POST /api/inferences/decide` | **503**, `reason: not_running`, naming `inferences-system-one` |
| `GET /api/inferences/workers` | embedding `ready` (1024 dims), reranker `ready`, system-one `not_running` |
| `POST /api/inferences/embed` with `{"inputs": []}` | the worker's own 400 `Empty`, unchanged |
| `POST /api/inferences/rerank` | 200, the worker's ranking unchanged |

Tests (`make test` in the unit, under `-race`): table-driven `httptest` cases for
byte-identical pass-through both ways, statuses 200 / 400 / 413 / 422 / 503 preserved,
forwarded headers, a chunked body arriving with a `Content-Length`, a closed port and an
unresolvable name as `not_running`, a hang-up as `busy`, a slow worker as a bounded 504, the
aggregated `/workers` with ready, no-model and not-running workers, the error classification
table (NXDOMAIN, SERVFAIL, refused, reset, EOF, deadline), configuration, and the real router:
routes mounted under the prefix, only POST proxied, and the server write deadline above the
timeout.

**Mutation**: 18 deliberate breakages of the gateway, all caught — among them re-encoding the
body, flattening statuses to 200, deriving the upstream path from the inbound one, reporting
SERVFAIL or a hang-up wrongly, dropping the transport timeout (the test fails at 10 s rather
than hanging), a 30 s default, and leaving the server write deadline at w-tools' default.

## Security and privacy

- **No auth.** Anyone who can reach `2104` can embed, rerank and read `/workers`. Loopback
  binding limits that to this host and to containers on `proxy`. `deployment/docker-compose.yml`
  still publishes `2104` on all interfaces; a deployment from it would expose the gateway —
  and the existing `/api/v1/chat` — to the LAN.
- **`DEEPSEEK_API_KEY`** is still required for `serveRest` to start, although only
  `/api/v1/chat` uses it. The gateway's compose file refuses to start without it rather than
  inventing a default.
- Bodies are not logged; a failed proxy logs the worker, the reason and the transport error.

## Open questions

- [ ] **Q:** What is a "user" on this single-account box, and what does the gateway check?
  — *owner:* Dhira
- [ ] **Q:** Should `serveRest` start without `DEEPSEEK_API_KEY`, disabling only `/chat`? Today
  the gateway cannot run without an LLM key it never uses.
- [ ] **Q:** OCR through the gateway: a DIP client over the shared socket (`packages/golibs/dip`)
  and an OCR row in `/workers`.
- [ ] **Follow-up:** the workers' eight-slot HTTP cap is held by idle keep-alive connections.
  Hindsight's pooled client filled all eight on the embedding worker while this was being
  verified (`refusing a scrape: 8 already open`), and `/health` was refused with it. The gateway
  reports that as `busy`; the fix belongs in `packages/pylibs/worker`.
