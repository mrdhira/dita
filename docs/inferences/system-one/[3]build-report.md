# Build report: services/inferences-system-one

Branch `feat/system-one-worker`, not committed. This report records what was built, the commands that
were run, and their real output. The host was shared and loaded throughout. Load averages are given next
to every timing, on a 6-core i5-8400T with 15 GB of RAM.

## Fix round 1

This round answers `system-one-review.md` as `system-one-fix-round-1.md` directed. Where it contradicts the
first-build sections below, it supersedes them. Those sections are kept as they were measured. Superseded:
`confidence` as the top probability, two requests in flight, the `deployment/docker-compose.yml` entry, the
DIP `readyz` healthcheck, the README's 422 claim, and the export's absolute 1e-3 bound.

The noul rule (item A) refused the `yes`/`no` noul draft in
`services/dita-orchestrator/handler/decisions/handler_test.go`. On Dhira's instruction the fixture moved to
`true`/`false`: the draft's options, the stub reply's keys and the two correction answers, five lines, with
no handler code and no `ParseReply` change. `go test -count=1 ./decisions/... ./handler/decisions/...` then
passes both packages. The dashboard's `src/contract/schema.test.ts` also fails 3 of its 29 shared cases
until PR #19 mirrors the noul rule and `criteria` in zod.

### A. `criteria` reaches the worker; nouls and ranges are refused where they are written

`decisions.Question` gained `Criteria string` (`omitempty`). `DecideRequest` carries `[]Question`, so a
template's criteria reach the worker unchanged. `TestCriteriaReachTheWorkerAsWritten` asserts that criteria
are sent when set and omitted when empty. `ValidateDraft` refuses a noul whose options are not exactly
`false` and `true`, in either order. `specs/decisions/schema-cases.json` has four new cases: a `yes`/`no`
noul refused, a three-option noul refused, `true`/`false` accepted, and criteria accepted. The existing
"every type" case's noul moved to `false`/`true`. `decisions_test.go` moved its `fraud` noul to
`true`/`false`. The worker refuses `range` with a 400 that says why. The spec states all three rules and
records the `criteria` naming mismatch, with the proposed rename, under open questions.

```
$ go test -run 'Criteria|SharedSchema' -v ./decisions/
--- PASS: TestTheSharedSchemaCases/a_noul_in_its_own_labels_is_refused
--- PASS: TestTheSharedSchemaCases/a_noul_with_a_third_option_is_refused
--- PASS: TestTheSharedSchemaCases/a_noul_answers_false_and_true_in_either_order
--- PASS: TestTheSharedSchemaCases/criteria_are_the_model's_instructions
--- PASS: TestCriteriaReachTheWorkerAsWritten
```

With the noul rule mutated out, both refusal cases fail.

### B. `confidence` is the model's own measure

`confidence_from_probs` is ported from upstream: 1 minus the entropy over log(k), on the probabilities after
temperature. Upstream computes it the same way. It is computed for a noul too, with k = 2. The spec's rule
and its 0.5801 example now agree. The parity guard asserts every reference confidence within 1e-4, for 7
choice and score questions across the fixtures. The contract-path test asserts the real alert's served
confidence rounds to 0.5801.

### C. The work is bounded

- **One request at a time.** `MAX_CONCURRENT_REQUESTS = 1`, so a second caller gets 429 at once. The
  comment claiming the cap kept `/info` answerable is gone.
- **A 100 s deadline**, checked before every encoder batch. A request that passes it answers 504
  (`Timeout`), with how many batches and questions were done and no partial reply.
- **A 413 before the encoder runs** when the planned work, the padded tokens of the batches, exceeds 8192.
  The derivation is in the spec and `/info` states all three bounds.

```
test_planned_work_over_the_budget_is_a_413_before_the_model_runs ... ok
test_a_request_past_its_deadline_is_a_504_with_nothing_returned ... ok
test_the_deadline_is_checked_before_every_batch_not_only_the_first ... ok   (stops after 2 of 3 batches)
test_past_the_concurrency_cap_the_answer_is_429_not_a_queue ... ok
```

I did not re-run the 20x1024 case against the orchestrator; the numbers behind the 8192 limit are the ones
measured in the first build.

### D. The build context and the image

The re-include of `services/inferences-system-one/` is now followed by explicit exclusions of its `models/`,
`**/__pycache__/`, `**/*.pyc` and `.coverage`. I proved it with the reviewer's method, a synthetic
`FROM scratch; COPY . /` build using this ignore file:

| | context | files | models, pycache, .coverage, venv, weights |
|---|---|---|---|
| before | 2.9 GB | 25,024 | all present, including `models/.export-venv` |
| after | 672 KB | 77 | none |

The real build's context transfer was 147.57 kB. In the rebuilt image, `grep -rl /mnt/data /app
/opt/system-one` finds nothing, and there is no `.pyc` or `__pycache__` under `/app`. Across the whole
image, the one file containing `/mnt/data` is fsspec's `utils.py`, whose docstring reads
`/mnt/datasets/test.csv`. The image is 2.41 GB.

### E. No deployment change

`git checkout -- deployment/docker-compose.yml`, so `deployment/` now has no diff. The orchestrator's own
`compose.yaml` is on `proxy` with `INFERENCES_SYSTEM_ONE_URL: http://inferences-system-one:8080`. The first
build's end-to-end request went exactly that way, so nothing in `deployment/` is needed.

### F. The healthcheck means a model is resident

`compose.yaml` and the Dockerfile's `HEALTHCHECK` probe HTTP `/health`. The image now sets
`METRICS_ADDR=0.0.0.0:8080` itself, so its own probe reaches the port the routes are on. I proved it on the
rebuilt image:

| container | old DIP `readyz` | HTTP `/health` | health status |
|---|---|---|---|
| preload `no-such-model` (fails) | `readyz: pass resident=none`, exit 0 | 503 | unhealthy |
| preload `laya-multilingual` | `readyz: pass resident=laya-multilingual` | 200 | healthy |

### G. The parity guard

The changes:

- **Act logits.** They are bounded absolutely at 3e-3; the old bound, 1e-4 of the magnitude, allowed about
  0.13.
- **The contract path.** A new test sends the real alert in contract form through `parse_decide`,
  `to_model`, the engine and `reply`, and gets the reference's numbers back.
- **Temperature.** The expected probabilities come from `rl_agent_config.json`, read directly, not from the
  engine's lookup. The test fails if per-bucket temperatures ever appear, so it is extended rather than
  silently wrong.
- **Fixture checks.** The anti-vacuity test now also asserts a dict state, a `score` question, a
  single-question call and the act logits.

`make parity` gives 5 tests OK, worst max|dp| 1.70e-06. Of the act-feature mutations, entropy zeroed and
top-1 swapped with the margin are now caught by parity. `k/256` still survives parity: on the real model
its effect is below float noise. The offline feature table (item H) catches it.

### H. The offline suite guards the head's arithmetic at a fake width

The fake width is now 128, with two heads, and the act head is upstream's 256 wide. `export/golden_head.py`
(`make golden`) builds upstream's head in torch, line for line from `DecisionModel.forward`. It runs on the
suite's seeded weights and writes `tests/fixtures/golden_head.npz` (22 KB). `test_the_numpy_head_is_torchs_arithmetic`
holds the numpy head to it. The act head's four input features are one function, checked against a table
worked by hand.

The head count is no longer hard-coded. It is upstream's `d // 64`, and the engine refuses to load if that
disagrees with the checkpoint's `num_attention_heads`; `encoder/config.json` is now read at runtime for
this. The README states what `make test` does and does not guard.

Mutation, each applied alone to `make test`, then restored:

| mutation | caught by |
|---|---|
| multi-head split without the transpose | golden head, padding invariance, row equivalence |
| type embedding dropped | golden head |
| scorer GELU to ReLU | golden head |
| entropy feature zeroed | golden head |
| `k/255` to `k/256` | act feature table (survived the golden alone) |
| gather one past the marker | golden head |
| confidence as the top probability | three tests |
| token budget, deadline, temperature buffer, head count, `range`, two slots, 504 mapped to 424 | their own tests |

All 14 were caught. Before this round, the reviewer found all six head mutations surviving `make test`.

### I. The temperature source is upstream's

`rl_agent_api.py` reads `temperature` and `temperature_by_options` from `rl_agent_config.json` and never
touches the checkpoint's `temperature` buffer. The engine does the same. It also refuses to load when the
buffer and the config disagree (`check_temperature`, tested both ways).

### J. The export check and the reference

The export's hidden-state check is relative to the RMS (about 1.75), with a bar of 1e-2 of it. In the
rebuild it measured 4.97e-4 of RMS at 2x1024. It is a gross-error trip; precision is the parity stage's job.

`export/reference.py` (`make reference`) regenerates `ref_out.json`. It fetches upstream's
`rl_agent_api.py` and `rl_common.py` at revision `5e7b2b1b…`, refuses them unless their sha256 matches, and
runs `RLAgent.system_one` unmodified. Run from this repo, it reproduced the committed fixture exactly:
answers, option logits, act logits and act softmax equal, and ids and markers equal. The committed fixture
is now its output. The only dropped field is the spike's timing, `latency_ms`.

### K. The docs and the stamp

The README no longer promises a 422 for options that do not fit. With at most 20 options, upstream's layout
always shrinks them to fit, so the engine's refusal is unreachable. The README also says the framework's 404,
411, 413 and 500 are HTML from `http.server`, and it documents the new bounds and the healthcheck.
`encoder.json` now stamps the graph's own sha256:

```
"graph_sha256": {"encoder.onnx": "33f15677…09ce", "encoder.onnx.data": "f1e49a02…9834"}
```

### Gates after this round

| command | result |
|---|---|
| `make -C services/inferences-system-one test` | 59 tests OK, 5 skipped (the parity guard, no weights) |
| `make -C services/inferences-system-one coverage` | 99%, floor 98 |
| `make -C services/inferences-system-one parity` | 5 tests OK, worst max\|dp\| 1.70e-06 |
| `make -C services/inferences-system-one image` | exit 0 in 3:59; context 147.57 kB; image 2.41 GB |
| `make doctor` | all PASS, lock in sync, `ready.` |
| `make dip-verify` | exit 0 |
| `go test ./decisions/...` | ok |
| `go test ./handler/decisions/...` | ok, after the fixture moved to a `true`/`false` noul |
| dashboard `schema.test.ts` | 3 of 29 fail until PR #19 mirrors the rule and `criteria` |

## Deferred, with reasons

**A golden reply shared by Go and Python.** One file, `specs/system-one/reply-cases.json`, would hold
request/reply pairs. The Python suite would assert that `reply()` produces them, and the Go suite would
assert that `ParseReply` accepts them, with the same answers best first. That needs a new directory under
`specs/`, one reader on each side, and a regeneration rule: the replies come from the fake engine's
deterministic output, so they change when the fake does. Until then the only cross-check against real bytes
is the throwaway test from the first build.

**Options truncated into the same text.** With many long options sharing a prefix, `build_sequence` cuts
each option to about 11 tokens, faithfully to upstream. Two options can then become identical to the model,
and nothing tells the caller. Detecting it is cheap: compare the cut option id lists and refuse with 422 when
two are equal. It is left because upstream does not refuse, so doing it here is a product decision, not a
port fix.

**Renaming `criteria` to `instructions`.** In this contract, `criteria` means the question's instructions;
in upstream it means per-option descriptions. The rename would also add optional option descriptions, which
the model supports. It moves stored templates, the template editor and the dashboard's schema, so it is
recorded in the spec's open questions, not done.

**Sum-to-1 in `ParseReply`.** The worker guarantees it (softmax in float64, within 1e-12), and the Go
adapter does not check it. The spec now says so instead of implying the adapter enforces it. Enforcing it
would be one loop in `ParseReply` with a 1e-6 tolerance and one table row. It is left because the
orchestrator's fixtures would need re-checking, and the user said not to change the parse contract.

**The reranker's and embedding's `Dockerfile.dockerignore`.** Both have this service's ordering bug:
`!services/inferences-reranker/` and `!services/inferences-embedding/` come after `**/models/`,
`**/__pycache__/` and `**/.coverage`, so those exclusions do not apply to their own service directory. In
this checkout their `models/` directories are empty, so the synthetic context measured 640 KB and 652 KB,
each carrying 2 `__pycache__` directories. In a checkout where their weights are fetched, the context would
also carry their whole `models/` directory, up to the 2.4 GB their README lists for embedding. The fix is
the same lines this service now has, after each re-include, for example
`services/inferences-reranker/models/` plus the `__pycache__`, `.pyc` and `.coverage` lines. It belongs in
its own change, raised with Dita.

**No length limit on `criteria`.** `ValidateDraft` bounds descriptions (500) and options (100) but not
`criteria`. The request body bound (`maxBody`) and the head's 256-token budget limit its effect, but a
template can store a long one. Adding a limit belongs with the zod mirror in PR #19, so the two sides do not
drift.

## What was built

`services/inferences-system-one/` is a worker in the pattern of `inferences-embedding` and
`inferences-reranker`. Its lifecycle runs through `packages/pylibs/worker` over DIP. Its data plane is
`POST /decide`, with `GET /info`, `GET /health` and `GET /metrics` on the same port. No code under
`packages/pylibs/` changed. `textinfer` already had what the route and the batching needed:
`Admission`, `plan_batches`, `pad` and `open_session`.

The engine, `onnx_decision` in `system_one_worker/engines/decision.py`, is the spike's `ort_agent.py`
ported. `build_sequence`, `render_options`, `temp_bucket`, the numpy head (including the last layer computed
only for the CLS row and the markers), `gelu`, `layer_norm` and the safetensors reader are the prototype's
code. The port changed four things, each for a stated reason:

- The class is split into `Decider` (logic) and `OnnxDecision` (opening files), as `Reranker` and
  `OnnxCrossEncoder` are. This lets the tests hand it a fake graph.
- Questions go to the encoder in batches bounded by `max_batch_tokens: 2048`, through `textinfer.plan_batches`,
  rather than one padded batch. Twenty questions at 1024 tokens would otherwise be one 20x1024 attention.
  Padding is masked, so the grouping cannot change an answer, and a test proves it.
- The final softmax is in float64, so the probabilities sum to 1 within 1e-12, not within float32 rounding.
- `Decision` carries the act head's raw logits. The reason is under "The parity guard".

`system_one_worker/decide.py` implements the contract as the requirement now states it. The reply is the
shape `decisions/worker.go` already parses, `probabilities` keyed by option, plus `act_probability`. A
contract question maps to the model like this: `criteria` becomes its instructions (the question's name
when absent), choice options become its keys, and score options become its levels. A `noul` is answered
under the keys `false` and `true`.

`models.yaml` pins five files at revision `b4a904d1a2a54c822b829e24291d4b8f280fe43e`, each with the
sha256 and byte count of the spike's real download:

| file | sha256 | bytes |
|---|---|---|
| `model.safetensors` | `9d628fd9…a8f204` | 643835514 |
| `tokenizer/tokenizer.json` | `609d8f4c…ab5b6f` | 34363188 |
| `tokenizer/tokenizer_config.json` | `424b6944…205e7` | 502 |
| `rl_agent_config.json` | `25061739…fcc30c6` | 472 |
| `encoder/config.json` | `83f6916d…a5bb4` | 1938 |

The brief named the first two files. The other three are also needed: the special tokens, the
checkpoint's own budgets and temperatures, and the encoder's architecture for the export. So they are
pinned the same way.

The encoder graph and the head weights are tied together. The export writes `encoder.json` beside the
graph, recording the sha256 of the `model.safetensors` it was built from. The engine refuses to load a graph
whose stamp differs from the `model.safetensors` digest in `models.yaml`. That digest is a YAML anchor, so the
file states it once. A revision bump without an image rebuild therefore fails at load instead of pairing an
old encoder with a new head.

The `Dockerfile` has five stages. `deps` exports the runtime requirements from the lock. `export` runs
`uv sync --group export` from the lock, which is the only place torch is installed. It fetches the pinned
inputs through the worker's own fetcher, exports the encoder with the dynamo exporter at opset 18 in fp32,
and refuses to finish unless onnxruntime reproduces torch's hidden states within 1e-3 on three inputs:
1x40, 2x300 with padding, and 2x1024 with padding. `runtime-deps` installs numpy, onnxruntime, tokenizers
and their dependencies from hashes. `parity` runs the parity guard torch-free against the exported graph,
and fails the build on a failure or a skip. The runtime stage copies the graph out of `parity`, so an image
cannot exist unless the guard passed. The graph lives at `/opt/system-one/encoder`. The first build put it
at `/app/onnx`, where Python resolved `import onnx` to it as a namespace package, so it was moved out of
`/app`.

torch reaches the lock only through a dependency group, `export`, and comes from the PyTorch CPU index. The
lock gained 466 lines and changed none. No other member's resolution moved, which is proven under
"Gates" below.

`decisions/worker.go` gained one optional field, `act_probability`, which is checked to lie in [0, 1] when
present. Nothing else in `ParseReply` changed. `handler/decisions/handler_test.go` is untouched and passes.

`deployment/docker-compose.yml` has the worker: container `inferences-system-one`, `expose: 8080`, no
`ports`, on `proxy`, and its socket in `../run`. The orchestrator entry in that file also joined `proxy`
(alongside `default`). Without that it could not resolve the worker, whose network is `proxy` only.

## Commands and their output

The local export ran in its own environment inside the gitignored `models/`, so torch never entered the
workspace venv the tests use.

```
$ make -C services/inferences-system-one export
parity 1x40 (row 0 padded from 40): max |dh| 1.45e-05
parity 2x300 (row 1 padded from 90): max |dh| 1.98e-04
parity 2x1024 (row 1 padded from 700): max |dh| 8.57e-04
{"repo": "convaiinnovations/laya-multilingual", "revision": "b4a904d1…", "weights_sha256": "9d628fd9…",
 "opset": 18, "torch": "2.14.0+cpu", "transformers": "5.17.0", "max_hidden_diff": 0.0008573532104492188}
Elapsed (wall clock) time: 4:49.26   Maximum resident set size (kbytes): 4044280   Exit status: 0

$ make -C services/inferences-system-one parity
test_neither_torch_nor_transformers_was_imported ... ok
test_the_fixtures_exercise_what_they_claim ... ok
test_the_real_alert_answers_as_the_reference_did ... ok
test_the_torch_free_path_reproduces_the_reference ...
parity: worst max|dp| 1.70e-06 over 6 cases
Ran 4 tests in 22.490s
OK
```

The same guard ran inside the image build, in the `parity` stage, and printed the same worst max|dp|:
1.70e-06. `make -C services/inferences-system-one image` exited 0. The cold build took 6:48 and the
rebuild after moving the graph took 1:47.

## The parity guard

The fixtures are the spike's torch reference run (`rl_agent_api.RLAgent`, fp32, CPU) over its six inputs.
The guard asserts five things:

- The token ids and marker positions are identical to the reference's.
- Every per-option probability is within 1e-4 of the reference, after temperature.
- The argmax is unchanged.
- `act_probability` is within 1e-4, and the raw act logits are within 1e-4 of their magnitude.
- Neither `torch` nor `transformers` is in `sys.modules`, checked both before and after inference.

Its anti-vacuity test asserts that there are six cases, that one fills the 1024-token limit and so crosses
the 128-token local window, and that the real alert is among them.

Mutation found one hole, now closed. With the CLS row dropped from the last head layer, the guard passed,
because the act head saturates. Its logits sit between roughly +1080 and +1560 for index 0 and between -1290
and -1850 for index 1 on every fixture, so `act_probability` is exactly 1.0 whatever the input. I
regenerated the reference with the spike's unmodified torch code, saving the raw act logits. The logits
were bit-identical to the original run. The guard now compares the act logits too, and it catches the
mutation.

## Mutation results

Each mutation was applied alone, run, and then restored. The tree was confirmed clean afterwards.

| mutation | caught by |
|---|---|
| padding mask dropped from head attention | `test_padding_and_grouping_do_not_move_any_answer` |
| ReLU removed from the head's FFN | parity guard (two tests) |
| CLS row dropped from the last layer | parity guard, after the act-logit check was added (it survived before) |
| temperature bucket ignored | `test_the_temperature_comes_from_the_bucket_then_the_type` |
| marker positions off by one | `test_the_layout_is_the_references`, `test_a_mask_token_in_the_input_cannot_forge_a_marker` |
| state not truncated to `max_len` | `test_the_state_is_cut_to_max_len_and_still_ends_in_sep` |
| provenance check removed | `test_it_refuses_before_opening_anything_it_cannot_trust` |
| confidence changed from the top probability | `test_the_reply_is_what_parse_reply_reads`, blackbox shape test |
| noul labels not enforced | `test_what_is_refused_before_the_model_is_touched`, blackbox refusal table |
| noul keyed by request order instead of false/true | `test_the_reply_is_what_parse_reply_reads` |
| duplicate question names allowed | `test_what_is_refused_before_the_model_is_touched` |
| tanh GELU in place of erf | `test_gelu_is_exact_erf_and_layer_norm_normalises` |
| Go: `act_probability` range check removed | `TestParseReplyRefusesAnAnswerToADifferentQuestion/an_act_probability_above_1` |

## The real request

I ran the image as `inferences-system-one` on `proxy`, with the repo's `models/` and `run/` mounted. The
request was the spike's alert, with its `severity` and `needs_human` questions and their criteria, plus a
`route` choice question.

Direct to the worker's `/decide`, six runs, load average 11.6 to 12.0:

```
200 2379 ms compute 2355.6 {"model_id": "laya-multilingual", "model_revision": "b4a904d1a2a54c822b829e24291d4b8f280fe43e",
 "answers": [{"name": "severity", "probabilities": {"info": 0.01121637873524426, "warning": 0.850673283927411,
   "critical": 0.1381103373373448}, "confidence": 0.850673283927411, "act_probability": 1.0},
  {"name": "needs_human", "probabilities": {"false": 0.9827628968742551, "true": 0.017237103125744814},
   "confidence": 0.9827628968742551, "act_probability": 1.0},
  {"name": "route", "probabilities": {"ignore": 0.09418304901154265, "auto_restart": 0.853883202116258,
   "notify_owner": 0.026082954637353016, "page_owner": 0.025850794234846267}, "confidence": 0.853883202116258,
   "act_probability": 1.0}]}
```

The six wall times were 2379, 1906, 1738, 1784, 2382 and 2471 ms, and the answers were identical every time.
Severity `warning` 0.8507 and needs_human `true` 0.0172 are the spike's reference values to four decimals.

Through the orchestrator already running on this host, `POST http://127.0.0.1:2104/api/inferences/decide`
returned the same body with HTTP 200 in 1.91 s. That orchestrator is someone else's build, from before this
branch. Its gateway forwards bytes and does not call `ParseReply`. So I ran this branch's `ParseReply` on
the exact bytes the worker returned, in a throwaway test (removed afterwards). It accepted them: severity
best `warning` 0.8507, needs_human best `false` 0.9828, route best `auto_restart` 0.8539, each with its
confidence equal to the top probability. I did not call the store-writing route, `POST
/api/inferences/decisions`, on that orchestrator, because its prediction store is not this branch's to write
to.

## Memory and latency

The table gives the cgroup `memory.peak` of the container, which includes the page cache charged to it:

| moment | peak |
|---|---|
| after the preload (cold, hashing 680 MB) | 1451 MB |
| 20 questions at the 1024-token limit | 1629 MB |
| restart at `mem_limit: 2g`, DIP unload then load, then the alert | 1285 MB, `oom_kill 0` |

`mem_limit` is `2g` in both compose files, set from the 1629 MB measured peak. Under that cap the worker
reloaded in 3.2 s, `readyz` passed, and the alert answered in 0.86 s.

The preload took 19.0 s cold and 11.4 s warm. The 20x1024 worst case took 169.5 s direct to the worker, at
load average 13.8. Through the orchestrator it returned 504 twice, because the orchestrator's
`INFERENCES_TIMEOUT` is 120 s. The worker does not cancel abandoned work: it finished both abandoned
requests, and the next three-question request waited 84.5 s behind them for 2.2 s of compute.

The image is 2.41 GB, against 432 MB for embedding and reranker. 1.2 GB of it is the fp32 graph.

## Gates

| command | result |
|---|---|
| `make doctor` | all PASS, `ready.` |
| `make test` | exit 2: `inferences-embedding` `test_the_default_is_the_english_model_and_says_so` fails |
| `make coverage` | exit 2: the same failure |
| `make dip-verify` | exit 0, every module stdlib only or on its named allowance |
| `make build` | exit 0: orchestrator, ocr and system-one images built |
| `make -C services/inferences-system-one image` | exit 0, parity stage passed |

The `make test` failure is not from this branch. The test expects `nomic-embed-text-v1.5` as the default.
Commit 5eaed99 ("default to qwen3-embedding-0.6b, pinned int8 export") changed the default to Qwen.
`services/inferences-embedding/` has no diff on this branch. `.claude/tasks/todo.md` already records this
failure.

Because the root loop stops at the first failing unit, I ran every later unit's own `test` and `coverage`:

| unit | test | coverage |
|---|---|---|
| `services/inferences-ocr` | 26 OK | floor 87 held |
| `services/inferences-reranker` | 33 OK | floor 93 held |
| `services/inferences-system-one` | 47 OK, 4 skipped (the parity guard, no weights) | 99%, floor 98 |
| `packages/golibs/dip` | ok | 81.5%, floor held |
| `services/dita-orchestrator` | ok, including `handler/decisions` untouched | 70.6%, floor 68 |
| `services/inferences-ocr/examples/go` | ok | 14.6%, floor 14 |
| `services/inferences-dashboard` | 80 passed | 94.08% statements |

The earlier units, dip (30), textinfer (11) and worker (95), passed inside the root run. Their floors held.

Adding a workspace member has broken other images before. I built the `deps` stage of the OCR, embedding
and reranker Dockerfiles against the new lock. All three succeeded. Their exported requirement files are
byte-identical to the ones HEAD's lock produces: 22, 21 and 21 pins.

## Findings for Dhira and Dita

The requirement's contract section contradicts itself on `confidence`. Its rule says the top probability
after temperature. Its example shows 0.5801 beside a top probability of 0.8507, and 0.5801 is upstream's
entropy-based `confidence_from_probs`. I implemented the rule, which is also what the dashboard's fixtures
assume. The example should read 0.8507.

Templates whose `noul` options are not `false`/`true` cannot be served. The model answers a noul only as
false/true, and the contract keys it that way. So the worker refuses `yes`/`no` with a 400, which the
orchestrator counts as `schema_invalid`. The orchestrator's own tests and the shared schema cases use
`yes`/`no` nouls. The template rules and the dashboard's zod schema do not say this yet. Changing them is
outside this branch.

`act_probability` is 1.0 for every input measured, because the act head's logits are in the thousands. On
this checkpoint it carries no signal. That answers the requirement's open question for now: showing it would
show a constant.

The orchestrator's templates carry no per-question instructions. When `criteria` is absent the worker uses
the question's name. Answer quality will depend on adding instructions to templates.

Abandoned requests are not cancelled, and the worst case outlasts the orchestrator's 120 s timeout on this
host. Both are bounded by `MAX_CONCURRENT_REQUESTS = 2`, but a caller that times out still occupies the model.

## What I could not verify

- Latency on an idle host. Every number above ran at load average 11 to 14.
- The dashboard's Decide page rendering these probabilities. The dashboard was not running here, and I did
  not start it.
- The orchestrator's store-writing route, `POST /api/inferences/decisions`, end to end against this worker.
  `ParseReply` was proven on the worker's real bytes instead.
- An export on a different CPU or thread count. The export's hidden-state check passed at 8.57e-4 against
  a 1e-3 bar, and the margin is thin. A build that fails there needs a relative bound, not a looser absolute
  one.
- Model quality. On the real alert the model still calls an OOM crash loop a `warning` that needs no human,
  as the spike found.
