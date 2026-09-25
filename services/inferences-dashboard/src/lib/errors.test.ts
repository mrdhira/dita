import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { describeError, shouldRetry } from "./errors";

describe("describeError", () => {
  it.each([
    [
      "the gateway: the worker is not running",
      503,
      { error: "x", reason: "not_running", worker: "inferences-system-one" },
      "The decision worker is not running",
    ],
    ["the gateway: the worker is busy", 503, { error: "x", reason: "busy" }, "The worker is busy"],
    [
      "the worker's own 503: no model resident",
      503,
      { error: "no model is loaded", error_type: "Unhealthy" },
      "The worker has no model loaded",
    ],
    [
      "a malformed schema",
      400,
      { error: "x", issues: [{ path: "questions.0.name", message: "bad" }] },
      "The schema or input is malformed",
    ],
    ["an engine refusal", 422, { error: "refused" }, "The engine refused this request"],
    [
      "a second correction",
      409,
      { error: "the first stands" },
      "This prediction is already corrected",
    ],
    ["a timeout", 504, { error: "slow" }, "did not answer in time"],
    ["a write without the token", 401, { error: "unauthorized" }, "needs a token"],
    ["a cross-site write", 403, { error: "forbidden" }, "from another site"],
    ["a write not sent as JSON", 415, { error: "unsupported" }, "the request's format"],
  ])("%s", (_, status, problem, title) => {
    expect(describeError(new ApiError(status, problem)).title).toContain(title);
  });

  it("says nothing was recorded when the worker could not answer", () => {
    expect(describeError(new ApiError(503, { error: "no model is loaded" })).detail).toContain(
      "Nothing was recorded",
    );
  });

  it("names the issue paths of a malformed schema", () => {
    const detail = describeError(
      new ApiError(400, { error: "x", issues: [{ path: "questions.0.name", message: "bad" }] }),
    ).detail;
    expect(detail).toBe("questions.0.name: bad");
  });
});

describe("shouldRetry", () => {
  it.each([
    [400, 0, false],
    [404, 0, false],
    [409, 0, false],
    [422, 0, false],
    [503, 0, true],
    [503, 1, true],
    [503, 2, false],
  ])("status %i after %i failures: %s", (status, failures, retry) => {
    expect(shouldRetry(failures, new ApiError(status, { error: "x" }))).toBe(retry);
  });
});

describe("a refused write reads as a sentence, never the raw error", () => {
  it.each([401, 403, 415])("%i", (status) => {
    const raw = '{"error":"x","error_type":"Forbidden"}';
    const { title, detail } = describeError(new ApiError(status, { error: raw }));
    expect(detail).toMatch(/Nothing was changed\.$/);
    expect(title + detail).not.toContain(raw);
  });
});
