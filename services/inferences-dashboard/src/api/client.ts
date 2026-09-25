import type { Draft } from "../contract/schema";

/** A failure body as the gateway and the orchestrator both write it. */
export interface Problem {
  error: string;
  error_type?: string;
  reason?: string;
  worker?: string;
  issues?: { path: string; message: string }[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem,
  ) {
    super(problem.error);
    this.name = "ApiError";
  }
}

export interface OptionScore {
  option: string;
  probability: number;
}

export interface Answer {
  question: string;
  type: string;
  options: OptionScore[];
  confidence: number;
}

export interface Correction {
  prediction_id: string;
  corrected_at: string;
  answers: Record<string, string>;
  outcomes: Record<string, "accepted" | "corrected">;
}

export interface Decision {
  id: string;
  created_at: string;
  schema: { name: string; version: number };
  input_text: string;
  model_id: string;
  model_revision: string;
  /** null when the store holds a reply that no longer parses. */
  answers: Answer[] | null;
  correction: Correction | null;
}

export interface Fault {
  path: string;
  message: string;
}

/**
 * The verdict fields are the server's: `usable` and `faults` are the runtime rules, which keep a
 * template saved under older authoring rules running; `authoring_issues` are what the editor's
 * rules would refuse today. Each is absent from an orchestrator that predates it.
 */
export interface Template extends Draft {
  version: number;
  created_at: string;
  retired?: boolean;
  retired_at?: string;
  usable?: boolean;
  faults?: Fault[];
  authoring_issues?: Fault[];
}

export interface Retirement {
  name: string;
  version: number;
  retired: true;
  retired_at: string;
}

export interface ClassScore {
  class: string;
  support: number;
  predicted: number;
  correct: number;
  precision: number | null;
  recall: number | null;
}

export interface Evaluation {
  id: string;
  created_at: string;
  name: string;
  rows: number;
  accuracy: number;
  brier: number;
  ece: number;
  classes: ClassScore[];
  baseline: { class: string; accuracy: number; brier: number };
  beats_baseline: boolean;
}

export interface WorkerReport {
  name: string;
  url: string;
  state: string;
  health_status: number | null;
  info: Record<string, unknown> | null;
  error?: string;
}

const BASE = "/api/inferences";

/**
 * Every route answers `{error, error_type}` except a recovered panic, which answers RFC 9457
 * `problem+json`; both are read, so neither shows the reader raw JSON.
 */
export function toProblem(parsed: unknown, fallback: string): Problem {
  if (parsed === null || typeof parsed !== "object") return { error: fallback };
  if ("error" in parsed && typeof parsed.error === "string") return parsed as Problem;
  const { detail, title } = parsed as { detail?: unknown; title?: unknown };
  if (typeof detail === "string" && detail !== "") return { error: detail };
  if (typeof title === "string" && title !== "") return { error: title };
  return { error: fallback };
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  // Every mutating request declares a JSON content type, even with no body (retire): the
  // orchestrator refuses one that does not, because that refusal is what keeps a cross-site
  // form POST, which cannot set a JSON content type, away from the store.
  if (body !== undefined || method !== "GET") {
    headers["Content-Type"] = "application/json";
  }
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  if (!res.ok) throw new ApiError(res.status, toProblem(parsed, text || res.statusText));
  return parsed as T;
}

export const api = {
  workers: () => request<{ workers: WorkerReport[] }>("GET", "/workers"),
  templates: () => request<{ templates: Template[] }>("GET", "/schemas"),
  versions: (name: string) =>
    request<{ versions: Template[] }>("GET", `/schemas/${encodeURIComponent(name)}/versions`),
  saveTemplate: (draft: Draft) => request<Template>("POST", "/schemas", draft),
  retire: (name: string, version: number) =>
    request<Retirement>("POST", `/schemas/${encodeURIComponent(name)}/versions/${version}/retire`),
  decide: (text: string, schema: { name: string; version: number }) =>
    request<Decision>("POST", "/decisions", { text, schema }),
  decision: (id: string) => request<Decision>("GET", `/decisions/${encodeURIComponent(id)}`),
  recent: () => request<{ decisions: Decision[] }>("GET", "/decisions?limit=50"),
  correct: (id: string, answers: Record<string, string>) =>
    request<Decision>("POST", `/decisions/${encodeURIComponent(id)}/correction`, { answers }),
  evaluate: (name: string, rows: { label: string; probabilities: Record<string, number> }[]) =>
    request<Evaluation>("POST", "/evaluations", { name, rows }),
  evaluations: () => request<{ evaluations: Evaluation[] }>("GET", "/evaluations"),
};
