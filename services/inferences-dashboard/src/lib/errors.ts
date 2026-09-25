import { ApiError, type Problem } from "../api/client";

const isGatewayRead = (route: string | undefined) =>
  route === "/workers" || route?.startsWith("/metrics/") === true;

/** On these reads the gateway names the worker it blames, so a 5xx naming none is not a worker's. */
function describeRead(route: string, status: number, problem: Problem) {
  if (problem.error_type === "NoAnswer") {
    return {
      title: "The gateway did not answer in time",
      detail: `${route}: ${problem.error}. What is shown is the last good answer, if any.`,
    };
  }
  if (problem.reason === "not_running" && problem.worker !== undefined) {
    return { title: `${problem.worker} is not running`, detail: `${problem.error}.` };
  }
  if (status >= 500 && problem.worker === undefined) {
    return {
      title: `The gateway, or something in front of it, answered ${status}`,
      detail: `${route}: ${problem.error || "no body"}. This says nothing about any worker or its model.`,
    };
  }
  return null;
}

/**
 * What a person should read for a failed request. A reasonless 503 means "no model" only on a
 * pass-through route (Try it, decide), never on the console's own reads of the gateway.
 */
export function describeError(error: unknown): { title: string; detail: string } {
  if (!(error instanceof ApiError)) {
    return { title: "The request did not complete", detail: String(error) };
  }
  const { status, problem, route } = error;
  if (problem.error_type === "NotMetrics") {
    return { title: "The answer is not a worker's /metrics page", detail: problem.error };
  }
  const read = isGatewayRead(route);
  if (read && route) {
    const described = describeRead(route, status, problem);
    if (described) return described;
  }
  switch (true) {
    case status === 503 && problem.reason === "not_running":
      return {
        title: `The decision worker is not running`,
        detail: `${problem.worker ?? "The worker"} does not answer. Nothing was recorded.`,
      };
    case status === 503 && problem.reason === "busy":
      return {
        title: "The worker is busy",
        detail: "It is at its connection limit. Nothing was recorded.",
      };
    case status === 503 && (problem.reason !== undefined || read):
      return {
        title: "The worker could not be reached",
        detail: `${problem.error}. Nothing was recorded.`,
      };
    case status === 503:
      return {
        title: "The worker has no model loaded",
        detail: `${problem.error}. Nothing was recorded.`,
      };
    case status === 504:
      return {
        title: "The worker did not answer in time",
        detail: `${problem.error}. Nothing was recorded.`,
      };
    case status === 400: {
      const issues = problem.issues?.map((i) => `${i.path}: ${i.message}`).join("; ");
      return { title: "The schema or input is malformed", detail: issues ?? problem.error };
    }
    case status === 422:
      return { title: "The engine refused this request", detail: problem.error };
    case status === 409:
      return { title: "This prediction is already corrected", detail: problem.error };
    case status === 401:
      return {
        title: "The orchestrator needs a token this page does not hold",
        detail:
          "Writes on this orchestrator are locked to a token, and the dashboard never holds one. Nothing was changed.",
      };
    // The orchestrator refuses a write from another site, and one not sent as JSON: from this
    // page either means a stale tab or something in between rewriting the request.
    case status === 403:
      return {
        title: "The orchestrator refused a request it judged to come from another site",
        detail:
          "It accepts changes only from the dashboard's own page. Reload the page and try again. Nothing was changed.",
      };
    case status === 415:
      return {
        title: "The orchestrator refused the request's format",
        detail:
          "Changes must be sent as JSON, and this one was not. Reload the page and try again. Nothing was changed.",
      };
    case status === 404:
      return { title: "Not found", detail: problem.error };
    case status === 502:
      return {
        title: "The worker answered in a shape it was not asked for",
        detail: `${problem.error}. Nothing was recorded.`,
      };
    default:
      return { title: `The request failed (${status})`, detail: problem.error };
  }
}

/** No retry for a 4xx, nor for a read with no answer (0): its next poll is the retry. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}
