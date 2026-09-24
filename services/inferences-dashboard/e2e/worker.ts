import { readFileSync } from "node:fs";

/**
 * Set to the real worker's URL to run the scenario against inferences-system-one instead of
 * the stub. The worker publishes no host port, so this is its address on the `proxy` network.
 * Set but unreachable is a failure: the run never falls back to the stub.
 */
export const realWorkerUrl = process.env.E2E_SYSTEM_ONE_URL;

export const STUB_URL = "http://127.0.0.1:18801";

/** What the stub and its screenshot label write. None may appear when the real worker answered. */
export const STUB_LABELS = ["stub-system-one", "STUB-not-a-model", "STUBBED"];

export interface PinnedModel {
  id: string;
  revision: string;
}

/**
 * The model and revision the worker's models.yaml pins: its `default_model`, or its only model.
 * A line-level read of the house layout rather than a YAML dependency; anything it cannot find
 * is a failure, not a guess.
 */
export function pinnedModel(
  path = process.env.E2E_SYSTEM_ONE_MODELS ?? "../inferences-system-one/models.yaml",
): PinnedModel {
  const text = readFileSync(path, "utf8");
  const scalar = (s: string | undefined) => s?.trim().replace(/^["']|["']$/g, "");
  const models = text
    .split(/^\s*- id:/m)
    .slice(1)
    .map((block) => ({
      id: scalar(block.split("\n")[0]) ?? "",
      revision: scalar(/^\s+revision:\s*(\S+)/m.exec(block)?.[1]) ?? "",
    }));
  const wanted = scalar(/^default_model:\s*(\S+)/m.exec(text)?.[1]);
  const model = wanted
    ? models.find((m) => m.id === wanted)
    : models.length === 1
      ? models[0]
      : undefined;
  if (!model?.id || !model.revision) {
    throw new Error(`${path}: no default_model (or single model) with an id and a revision`);
  }
  return model;
}
