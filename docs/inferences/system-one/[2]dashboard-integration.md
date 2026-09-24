# Integration — the dashboard over the real worker

> The dashboard half already exists on `main` (PR #15): Decide, Decision, Eval, History and Templates pages,
> the API client, the zod contract, and an e2e that answers `/decide` from `e2e/stub-system-one.mjs`.
> What is missing is the real worker behind it.

## What this PR does

- **Proves the pages against real answers.** The stub derived probabilities from a hash of the text and
  labelled itself `model_id: stub-system-one`, `model_revision: STUB-not-a-model`. A real answer carries a real
  `model_id` and revision, probabilities that sum to 1 in the model's own order, and `act_probability`.
- **Keeps the stub for isolation, adds a real path.** The stub stays as the fast, hermetic e2e; a second e2e
  run against the running worker asserts the same pages, so a break in either is attributable.
- **Never lets a stub pass as a model.** Anywhere a person can see it, a stub answer says so — the rule the
  stub's own comment states, kept for the real path by asserting the revision in `models.yaml` appears.
- **Calibration panel tells the truth.** Every temperature in the checkpoint is 1.0, so the panel reports the
  identity transform and says so, rather than implying a fitted calibration that does not exist.
- **Corrections round-trip.** `CorrectionForm` posts a correction against a real prediction; the stored record
  keeps both, which is the label the future calibration needs.

## Acceptance

- Decide page: text in, real probabilities out, through Caddy → orchestrator → worker.
- `model_id` and `model_revision` shown on the answer match `models.yaml`.
- History shows the stored prediction; Eval scores it against a correction when one exists.
- No page renders stub labels when the real worker answered.
- The dashboard's own gates pass (`npm run test`, `npm run e2e`, lint, typecheck).

## Not in this PR

- The schema builder's taxonomy beyond what it already offers.
- Any calibration fit (no labels yet).
- Auth on the dashboard (the orchestrator owns that; out of scope here).
