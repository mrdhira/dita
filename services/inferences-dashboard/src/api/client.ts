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
  answers: Answer[];
  correction: Correction | null;
}

export interface Template extends Draft {
  version: number;
  created_at: string;
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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
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
  if (!res.ok) {
    const problem =
      parsed !== null && typeof parsed === "object" && "error" in parsed
        ? (parsed as Problem)
        : { error: text || res.statusText };
    throw new ApiError(res.status, problem);
  }
  return parsed as T;
}

export const api = {
  workers: () => request<{ workers: WorkerReport[] }>("GET", "/workers"),
  templates: () => request<{ templates: Template[] }>("GET", "/schemas"),
  versions: (name: string) =>
    request<{ versions: Template[] }>("GET", `/schemas/${encodeURIComponent(name)}/versions`),
  saveTemplate: (draft: Draft) => request<Template>("POST", "/schemas", draft),
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
