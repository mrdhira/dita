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
      "the gateway: the worker is unreachable, which says nothing of its model",
      503,
      { error: "x", reason: "unreachable" },
      "The worker could not be reached",
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
    [
      "the gateway: the worker timed out",
      504,
      {
        error: "slow",
        error_type: "Unhealthy",
        worker: "inferences-system-one",
        reason: "timeout",
      },
      "did not answer in time",
    ],
    ["a write without the token", 401, { error: "unauthorized" }, "needs a token"],
    ["a cross-site write", 403, { error: "forbidden" }, "from another site"],
    ["a write not sent as JSON", 415, { error: "unsupported" }, "the request's format"],
  ])("%s", (_, status, problem, title) => {
    expect(describeError(new ApiError(status, problem)).title).toContain(title);
  });

  describe("a 5xx is read from its body: one no server wrote is not a worker's", () => {
    it.each([
      [
        "/workers",
        503,
        { error: "Service Unavailable" },
        "The gateway, or something in front of it, answered 503",
      ],
      ["/workers", 502, { error: "" }, "The gateway, or something in front of it, answered 502"],
      [
        "/metrics/reranker",
        503,
        { error: "" },
        "The gateway, or something in front of it, answered 503",
      ],
      [
        "/metrics/reranker",
        502,
        { error: "Bad Gateway" },
        "The gateway, or something in front of it, answered 502",
      ],
      [
        "/workers",
        0,
        { error: "no answer within 10 s", error_type: "NoAnswer" },
        "The gateway did not answer in time",
      ],
      [
        "/metrics/reranker",
        502,
        { error: "x", reason: "too_large", worker: "inferences-reranker" },
        "The worker answered in a shape it was not asked for",
      ],
      [
        "/metrics/reranker",
        503,
        {
          error: "inferences-reranker is not running",
          reason: "not_running",
          worker: "inferences-reranker",
        },
        "inferences-reranker is not running",
      ],
      [
        "/metrics/reranker",
        503,
        { error: "x", worker: "inferences-reranker" },
        "The worker could not be reached",
      ],
      [
        undefined,
        503,
        { error: "no model is loaded", error_type: "Unhealthy" },
        "The worker has no model loaded",
      ],
      [
        undefined,
        500,
        {
          error:
            "inferences-system-one answered 500 with a body of type text/html, not a JSON error",
          error_type: "Backend",
          worker: "inferences-system-one",
        },
        "inferences-system-one answered 500",
      ],
      [
        "/metrics/reranker",
        404,
        {
          error: "inferences-reranker answered 404 with a body of type text/html, not a JSON error",
          error_type: "Backend",
          worker: "inferences-reranker",
        },
        "inferences-reranker answered 404",
      ],
      [
        undefined,
        502,
        { error: "" },
        "The orchestrator, or something in front of it, answered 502",
      ],
      [
        undefined,
        503,
        { error: "Service Unavailable" },
        "The orchestrator, or something in front of it, answered 503",
      ],
      [
        undefined,
        504,
        { error: "" },
        "The orchestrator, or something in front of it, answered 504",
      ],
      [
        undefined,
        502,
        { error: "reading the worker's answer: EOF", error_type: "Backend" },
        "The worker answered in a shape it was not asked for",
      ],
    ])("%s %i %j", (route, status, problem, title) => {
      const { title: got, detail } = describeError(new ApiError(status, problem, route));
      expect(got).toBe(title);
      if (got.includes("or something in front of it")) {
        if (route) expect(detail).toContain(route);
        expect(`${got} ${detail}`).not.toMatch(/no model loaded|The worker/);
      }
    });

    it("reads a page that is not a worker's /metrics as that, on any route", () => {
      const error = new ApiError(502, { error: "it came as text/html", error_type: "NotMetrics" });
      expect(describeError(error).title).toBe("The answer is not a worker's /metrics page");
    });
  });

  it("says nothing was recorded when the worker could not answer", () => {
    expect(
      describeError(new ApiError(503, { error: "no model is loaded", error_type: "Unhealthy" }))
        .detail,
    ).toContain("Nothing was recorded");
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
