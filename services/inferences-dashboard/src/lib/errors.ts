import { ApiError } from "../api/client";

/**
 * What a person should read for a failed request. Each status is measured behaviour, not a
 * generic failure: a 503 with no gateway reason is the worker itself saying it has no model.
 */
export function describeError(error: unknown): { title: string; detail: string } {
  if (!(error instanceof ApiError)) {
    return { title: "The request did not complete", detail: String(error) };
  }
  const { status, problem } = error;
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
    case status === 503 && problem.reason !== undefined:
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

/** Never retry a 4xx: the request is the user's to fix. A 5xx gets two more tries. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}
