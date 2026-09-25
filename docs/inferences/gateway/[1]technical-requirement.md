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
| `INFERENCES_API_TOKEN` | unset: the write routes are open, as before (see [Writes: one guard on every POST](#writes-one-guard-on-every-post)) |

A URL without an `http(s)` scheme and a host, a timeout that does not parse or is not
positive, or a token shorter than 32 characters or with surrounding whitespace, stops
`serveRest` with the variable's name rather than being guessed at.

At start `serveRest` reads `inferences-system-one`'s `/info` and refuses to serve when
`INFERENCES_TIMEOUT` does not outlast its published `deadline_s` (100 s today): the worker would
otherwise hold its one slot for a caller the gateway has already dropped. A worker that is not up yet,
or publishes no deadline, is logged as unchecked, not fatal. Compose sets `GOMEMLIMIT=200MiB` under
the 256 MB container limit, so the collector works against the real ceiling.

When no worker answers, the gateway's body names the worker and the reason only; its address and the
transport error go to the log, not the caller.

### Writes: one guard on every POST

Every `POST` the orchestrator serves — the decisions store's routes, the retire route, `/api/v1/chat`
and the pass-through `/api/inferences/{embed,rerank,decide}` — goes through one guard, in this order:

| check | refused with |
| --- | --- |
| `Sec-Fetch-Site: cross-site` | `403`, `error_type: "Forbidden"` |
| `Content-Type` other than `application/json` (parameters such as `charset` allowed) | `415`, `error_type: "Validation"` |
| `INFERENCES_API_TOKEN` set and `X-Inferences-Token` missing or wrong | `401`, `error_type: "Unauthorized"` |

**The first two close cross-site request forgery.** A `text/plain` or form POST is a CORS "simple"
request: a browser sends it cross-site without asking, so any page a LAN user opens could otherwise write
the store or spend the chat key. `application/json` forces a preflight, and the orchestrator serves no
CORS headers, so the preflight fails and the request is never sent. `Sec-Fetch-Site` refuses outright
what a current browser marks as cross-site. The dashboard already sends `application/json`. A client of
the pass-through that sends no content type is refused too; TEI clients send `application/json`.

**The token is for programmatic clients**, and off unless configured. Unset, nothing changes. Set, a
missing header and a wrong one are both `401`, with a message saying which. The comparison is
constant-time over SHA-256 digests, so neither the value nor its length leaks through timing. Reads
(`GET`) stay open. Generate one with `openssl rand -hex 32` and give it to the orchestrator from an env
file that is not committed; `serveRest` logs when it is unset.

**A header added by a proxy is not authentication.** Do not have Caddy inject the token: a header set
by the proxy authenticates the proxy, and Caddy would attach it to every request that reaches the
origin, a cross-site one included. The deployment would look closed and not be. Keeping people on the
LAN out of the dashboard is Caddy's job, with `basic_auth` or `forward_auth` on the site block; that
configuration is Dhira's and Dita's to choose.

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
(`127.0.0.1:2104`), not on all interfaces as the deployment stack does.

**What is deployed, and what loopback does not do.** Loopback publishing keeps the port off the
LAN, not the gateway: the host's Caddy serves `orchestrator.api.home.arpa` as a bare
`reverse_proxy` to every route, on `0.0.0.0:443`, and about twenty-four containers on `proxy`
reach `dita-orchestrator-rest:2104` directly. So the gateway is reachable from the LAN. What guards
it is in the app: every POST refuses cross-site and non-JSON requests, and needs
`INFERENCES_API_TOKEN` when that is set ([Writes](#writes-one-guard-on-every-post)). Reads are open
to anyone who can reach it. A header Caddy injects is not authentication.

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

- **No auth by default.** Anyone who can reach `2104` can embed, rerank and read `/workers`, and
  write the decisions store unless `INFERENCES_API_TOKEN` is set ([Writes](#writes-one-guard-on-every-post)).
  Loopback binding limits that to this host and to containers on `proxy`. `deployment/docker-compose.yml`
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
- [ ] **Follow-up:** the box's Caddy block for `orchestrator.api.home.arpa` still proxies every path, including
  `/api/v1/chat`. Narrowing it to the inferences routes is a deployment change, not a code one, and until it is
  made the LAN can reach a route that spends the LLM key. The same block sets no `Permissions-Policy` header.
- [ ] **Follow-up:** `INFERENCES_API_TOKEN` is unset in the deployment, so the write routes are open to anyone who
  can reach the gateway and the worker routes answer without a token. The code path exists and is tested (`401`
  when set); the deployment does not set it.
- [ ] **Follow-up:** chat and the router's own `404`/`405` answer plain text while every handler answers the
  standard error shape. Nothing parses them yet; a client that does will notice.
- [ ] **Follow-up:** four mutations are known to survive the suite — removing the `fsync`, a failed write inside
  `Correct` being ignored, `Evaluate`'s own row cap, and the dashboard's `MAX_TEXT` — and chat no longer logging
  the reply body has no test that would notice its return.
- [ ] **Follow-up:** the workers' eight-slot HTTP cap is held by idle keep-alive connections.
  Hindsight's pooled client filled all eight on the embedding worker while this was being
  verified (`refusing a scrape: 8 already open`), and `/health` was refused with it. The gateway
  reports that as `busy`; the fix belongs in `packages/pylibs/worker`.
- [ ] **Q:** should the repo have CI? The dashboard's shared schema cases sat failing on `main` with nothing to
  catch them, which is how the two implementations drifted apart. — *owner:* Dhira
