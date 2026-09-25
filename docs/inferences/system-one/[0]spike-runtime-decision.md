# Spike: can Laya System 1 be served torch-free?

## Answer

Yes. `convaiinnovations/laya-multilingual` runs on onnxruntime, `tokenizers` and numpy, with no torch at
runtime. The encoder is exported to ONNX in fp32. The decision head runs in numpy. On six inputs, including the
real alert, this path returns JSON identical to the upstream torch reference. The largest per-option probability
difference is 2.0e-6.

The torch-free path meets the house rule stated in `services/inferences-embedding/README.md`. It uses the
versions the repo already locks: onnxruntime 1.30.0, tokenizers 0.23.2 and numpy 2.5.3 on Python 3.14.6. It adds
no new dependency; safetensors is parsed with the standard library.

## The HF files disagree with the task brief

The brief described the English checkpoint, not the multilingual one. The facts below were read from the files at
`laya-multilingual` commit `b4a904d1a2a54c822b829e24291d4b8f280fe43e`.

`laya-multilingual` ships no code. `rl_agent_api.py` and `rl_common.py` exist only in `convaiinnovations/laya`
(commit `5e7b2b1b8ca2ecdd3f2322d94069c9b6ce7e844b`). That repo also carries a `multilingual/` directory whose
config, encoder config, tokenizer and weights are byte-identical to `laya-multilingual` (same LFS oids). The
reference run therefore used the English repo's code with the multilingual weights.

| field | brief | `laya-multilingual` |
|---|---|---|
| encoder | ModernBERT, hidden 1024 | `jhu-clsp/mmBERT-base`, hidden 768, 22 layers, vocab 256000 |
| `max_len` | 512 | 1024 |
| `head_max_len` | 192 | 256 |
| `temperature_by_options` | per-bucket values | `{}` |
| `temperature` | not stated | `[1.0, 1.0, 1.0]`; the stored `temperature` buffer is also `[1, 1, 1]` |
| weight dtype | fp32 (`encoder/config.json` says `float32`) | 169 tensors F16, one F32 (`temperature`) |

`head_layers: 2` matches the checkpoint: `head.layers.0` and `head.layers.1` are present, and the runtime
refuses to start if the counts differ. `needs_human: noul` in the brief is correct as written. `noul` is the
model's name for its boolean question type, and it is not a typo.

The checkpoint ships uncalibrated. Every temperature is 1.0, so the temperature code path is exercised but is
the identity for this checkpoint. The model card says the same and recommends fitting per-bucket temperatures on
our own data.

## What the head computes

`DecisionModel.forward` runs the encoder over the whole sequence and takes `last_hidden_state`, after ModernBERT's
final norm. It adds a learned type embedding (choice, score or noul) to every position. It then runs two
`nn.TransformerEncoderLayer` blocks over the full sequence: d=768, 12 heads, FFN 3072, pre-norm, ReLU, with padded
keys masked out. This is a real transformer over every token, not a linear head. After the head layers it
gathers one row per option at that option's `[MASK]` marker. A scorer (LayerNorm, Linear 768 to 768, exact-erf
GELU, Linear 768 to 1) turns each gathered row into one logit. Unused option slots are filled with -1e4.

The marker positions exist so the answer space can be chosen per request. `build_sequence` lays out
`[CLS] "<type> question: <instructions>" [SEP] [MASK] opt0 [MASK] opt1 ... [SEP] state [SEP]`. Each marker is the
index of the `[MASK]` that opens an option's text. The markers are used for exactly one thing, the gather that
selects the rows the scorer sees. Softmax over those logits gives the option distribution. For `noul`, the
options are always `[false, true]`, and the answer is `p[1]`.

A second output, `act_head`, feeds the `[CLS]` row plus four features of the answer distribution (top-1, top-1
minus top-2, normalised entropy, k/255) through Linear 772 to 256, GELU, Linear 256 to 2. `system_one` reports
its softmax index 0 as `act_probability`.

Nothing in the head is exotic. It uses matmuls, layer norms, masked softmax attention, ReLU, erf-GELU, a gather
and a sort. All of it is exportable and all of it is straightforward in numpy. The numpy version computes the
last head layer only for the rows that are read afterwards (CLS and the markers). This is exact, because each
query row of attention is independent of the others. At 1024 tokens it roughly halves the head's cost.

## Reference run (torch)

The reference is `RLAgent.system_one` from `rl_agent_api.py`, unmodified, on CPU in fp32. The fp16 checkpoint is
upcast on load. The real input was:

```
state: "alert: service immich stopped responding; container exited with code 137 (OOM) after 3 restarts in 10 minutes"
severity:    choice, criteria [info, warning, critical],
             "This is an alert from a homelab monitoring system. How severe is it for the person who runs the homelab?"
needs_human: noul, "Does this homelab alert need a human to intervene, rather than resolving on its own?"
```

The answer, verbatim from both runtimes:

```json
{"model": "rl-agent",
 "answers": {
  "severity": {"type": "choice", "choice": "warning",
               "probabilities": {"info": 0.0112, "warning": 0.8507, "critical": 0.1381},
               "confidence": 0.5801, "rl_agent": {"act_probability": 1.0}},
  "needs_human": {"type": "noul", "noul": 0.0172, "rl_agent": {"act_probability": 1.0}}},
 "usage": {"input_tokens": 139, "output_tokens": 0}}
```

The probability matrix. Temperature is 1.0, so these are the raw logits softmaxed:

| question | option | logit | probability |
|---|---|---|---|
| severity | info | -4.5823 | 0.0112 |
| severity | warning | -0.2536 | 0.8507 |
| severity | critical | -2.0716 | 0.1381 |
| needs_human | false | 3.4783 | 0.9828 |
| needs_human | true | -0.5650 | 0.0172 |

The model calls an OOM crash loop a warning that needs no human. That is a quality finding for the use case, not
a runtime finding. It agrees with the model card, which warns that `noul` under-reports true on this checkpoint.

## Parity

Parity was checked on six inputs. They cover a string state and a dict state, English, German and Japanese,
choice with 3, 4 and 5 options, `score`, `noul` with and without criteria, and a single-question call. They also
cover a 1024-token input that is truncated and exceeds the 128-token sliding window. In every case the token ids
and marker positions built by `tokenizers` were identical to the ones built by `transformers.AutoTokenizer`.

Columns: max |dp| is the maximum absolute difference in per-option probability, after temperature. |d act| is
the same for `act_probability`. The last column records whether the emitted answer JSON was byte-identical.

| input | tokens (per question) | max \|d logit\| | max \|dp\| | \|d act\| | JSON identical |
|---|---|---|---|---|---|
| real-immich-oom | 66, 73 | 6.9e-6 | 2.4e-7 | 0 | yes |
| disk-info-dict-state | 80, 76 | 5.0e-6 | 1.3e-6 | 0 | yes |
| de-ticket-5way-score | 65, 83, 67 | 1.1e-5 | 4.8e-7 | 0 | yes |
| long-journal-over-window | 1024, 1024 | 5.7e-6 | 3.3e-7 | 0 | yes |
| ja-backup-ok | 55, 64 | 7.6e-6 | 1.7e-6 | 0 | yes |
| single-noul | 57 | 3.8e-6 | 7.2e-7 | 0 | yes |

These figures are for `encoder_dynamo.onnx`. The TorchScript export also passed. Its worst max |dp| was 2.0e-6 on
`ja-backup-ok`, and all six answers were identical.

I would accept a max |dp| of 1e-4, with the argmax unchanged on every question. The API rounds probabilities to
four decimals, so a difference below half the last digit (5e-5) almost never shows. At 1e-4 a value can flip by
at most one unit in the last place, and only when it sits on a rounding boundary. fp32 reduction-order noise is
about 1e-6, which is where we measured. A difference above 1e-4 means a real computational divergence, such as a
wrong mask, a wrong activation or a dropped layer, not float noise.

## Latency and memory

The host was shared and heavily loaded during every run. The load average was 8.7 at the start and 14 to 18
during the benchmark, on a 6-core i5-8400T with 15 GB of RAM and swap already full. The absolute latencies below
are noisy and pessimistic. The interleaved comparison is the fairest number, because both runtimes ran
alternately in one process on the same inputs under the same contention.

The real input, 5 timed runs after one warm-up. Each runtime ran in its own process, several minutes apart:

| runtime | runs (ms) | median (ms) |
|---|---|---|
| torch reference | 1767, 1011, 808, 849, 1284 | 1011 |
| onnxruntime + numpy | 486, 557, 539, 419, 414 | 486 |

Interleaved, 7 alternating runs per input in one process, median / min in ms:

| input | torch | onnxruntime + numpy |
|---|---|---|
| real-immich-oom | 8367 / 5670 | 3229 / 1789 |
| disk-info-dict-state | 8418 / 5400 | 3937 / 2933 |
| de-ticket-5way-score | 6858 / 5441 | 3615 / 2916 |
| long-journal-over-window (2 x 1024 tokens) | 13921 / 3904 | 13630 / 7234 |
| ja-backup-ok | 372 / 292 | 389 / 320 |
| single-noul | 242 / 229 | 222 / 201 |

The load average fell from about 18 to 11 during the last two rows, which explains their low figures. On short
inputs the two runtimes are level, or onnxruntime is ahead. On the 1024-token input, onnxruntime is slower: the
best run was 7.2 s against 3.9 s for torch. A split profile of that input, from a separate process, put 14.4 s in
the ONNX encoder and 2.9 s in the numpy head. Torch runs the encoder's attention through fused SDPA. The exported
graph has 22 unfused `MatMul`/`Softmax` attention blocks and no fused `Attention` op. This is the likely cause,
but I did not isolate it from the load.

Resident memory, from `ru_maxrss` and `/proc/self/status`:

| runtime | RSS after load | peak RSS | load time |
|---|---|---|---|
| torch reference (`/usr/bin/time` max RSS) | 1658 MB | 4051 MB (4.15 GB) | 154 s |
| onnxruntime + numpy, `encoder_dynamo.onnx` | 640 MB | 1739 MB | 8.9 s |
| onnxruntime + numpy, `encoder_torchscript.onnx` | 1624 MB | 2445 MB | 47.8 s |

The torch peak comes from load. `build_model` materialises a randomly initialised fp32 model, and then the state
dict is copied in. The dynamo export keeps its weights in an external `.onnx.data` file, which onnxruntime maps
rather than reads, and its RSS grows to about 1.25 GB after the 1024-token call. I chose the dynamo export over
TorchScript for three reasons: it had equal parity, lower memory and faster load, and the TorchScript tracer
warned that it baked shape-dependent values into constants. That did not bite on these inputs, but it is a
standing risk. Load times include a cold page cache under memory pressure. The 154 s for torch is mostly I/O and
swap, not work.

## Versions

| component | version |
|---|---|
| Python | 3.14.6 (both venvs, matches `.tool-versions`) |
| torch | 2.14.0+cpu (spike venv only) |
| transformers | 5.17.0 (spike venv only) |
| safetensors | 0.8.0 (spike venv only; the torch-free path does not use it) |
| onnx | 1.23.0 |
| onnxscript | 0.7.2 (needed by the dynamo exporter) |
| onnxruntime | 1.30.0 |
| tokenizers | 0.23.2 |
| numpy | 2.5.3 |
| uv | 0.12.13 |
| ONNX opset | 18 (dynamo); 17 (TorchScript) |

The torch-free venv holds only numpy, onnxruntime and tokenizers, plus their transitive dependencies:
huggingface-hub (pulled in by tokenizers), protobuf, flatbuffers and packaging. `parity.py` asserts that neither
`torch` nor `transformers` is in `sys.modules`.

## Commands

All commands were run in a throwaway spike directory outside this repo, which is not committed. `git-lfs` is not installed on the host, so
the weights were fetched by URL at the pinned revision and checked against the LFS oids.

```sh
git clone --depth 1 https://huggingface.co/convaiinnovations/laya-multilingual hf-laya-multilingual
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://huggingface.co/convaiinnovations/laya hf-laya
cd hf-laya-multilingual
for f in model.safetensors tokenizer/tokenizer.json; do
  curl -sSL --fail -o $f.tmp https://huggingface.co/convaiinnovations/laya-multilingual/resolve/b4a904d1a2a54c822b829e24291d4b8f280fe43e/$f && mv $f.tmp $f
done
sha256sum model.safetensors tokenizer/tokenizer.json   # 9d628fd9...a8f204, 609d8f4c...ab5b6f: match the pointers
cd ..

uv venv --python 3.14.6 .venv-torch
VIRTUAL_ENV=.venv-torch uv pip install --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
  torch transformers safetensors numpy==2.5.3 onnx onnxruntime==1.30.0 tokenizers==0.23.2 onnxscript
uv venv --python 3.14.6 .venv-ort
VIRTUAL_ENV=.venv-ort uv pip install numpy==2.5.3 onnxruntime==1.30.0 tokenizers==0.23.2

cd work
/usr/bin/time -v ../.venv-torch/bin/python ref.py                 # reference answers, logits, latency, RSS -> ref_out.json
../.venv-torch/bin/python export.py dynamo 18                      # -> encoder_dynamo.onnx (+ .onnx.data)
../.venv-torch/bin/python export.py torchscript 17                 # -> encoder_torchscript.onnx
../.venv-ort/bin/python parity.py encoder_dynamo.onnx              # torch-free: parity, latency, RSS
../.venv-ort/bin/python parity.py encoder_torchscript.onnx
../.venv-ort/bin/python prof.py encoder_dynamo.onnx                # encoder vs head time split
../.venv-torch/bin/python bench.py encoder_dynamo.onnx             # interleaved torch vs onnxruntime
```

The scripts and inputs are in `work/` in the spike directory: `cases.json`, `ref.py`, `export.py`,
`ort_agent.py`, `parity.py`, `prof.py` and `bench.py`. `ort_agent.py` is the torch-free implementation. It
reproduces `build_sequence`, `render_options`, `temp_bucket`, `confidence_from_probs` and the `system_one`
response shape line for line.

## What I could not determine

I could not produce clean latency numbers. Every measurement ran on a host whose load average was two to three
times its core count. The one-process interleaved table gives the ratio, not the service's absolute latency. A
rerun on an idle host is needed before anyone sets a latency budget.

I did not isolate why the ONNX encoder is about twice as slow as torch at 1024 tokens. Fusing attention with
onnxruntime's transformer optimizer, or exporting through a newer opset with a native `Attention` op, may close
the gap. I did not try either. I also did not try fp16 or int8, because fp32 parity passed.

I did not check whether the `laya` PyPI package (0.3.7, the model card's quickstart) takes the same inference
path as `rl_agent_api.py`. The brief named `rl_agent_api.py` as the reference, and that is what was compared.

The ONNX file stores fp32 weights (1.2 GB), which are an upcast of an fp16 checkpoint (644 MB). Storing fp16 in
the ONNX and computing in fp32 would halve the image. I did not measure that.

I did not check how well the uncalibrated probabilities match reality on our alerts. Both runtimes agree that the
real alert is a `warning` with `needs_human` at 0.0172, and that looks wrong for an OOM crash loop.

## Decisions recorded after the spike (Dhira, 2026-09-25)

1. **The ONNX artifact is produced in the build stage, not vendored.** A multi-stage build runs the export
   with torch present in the builder stage; the runtime stage keeps the rule the embedding README states
   (onnxruntime and tokenizers, no torch). Nothing is committed and nothing is downloaded from a third party.
2. **Publishing the artifact is deferred tech debt, deliberately.** When it is picked up: re-export in fp16
   with fused attention (1.2 GB to about 644 MB), re-verify parity, publish to a HuggingFace repo
   `laya-multilingual-system-one-onnx` under a namespace Dhira chooses, with a model card naming the upstream
   repo `convaiinnovations/laya-multilingual` at revision `b4a904d1a2a54c822b829e24291d4b8f280fe43e`, its
   apache-2.0 licence, the changes made, the parity numbers and the artifact sha256. Then pin it in
   `models.yaml` and delete the export step. This needs a write token placed at
   `~/.hermes/credentials/hf-token` — never pasted into a chat.
3. **Item 2 is what closes item 1.** An export step is slower and weaker evidence than a pinned download
   with a sha256; the build stage is the interim, not the resting place.

Still open, and not a decision for this document: where the service is reachable from (loopback publish, a
Caddy hostname, or a route through the orchestrator's REST). That is Dita's call, since exposure is hers.
