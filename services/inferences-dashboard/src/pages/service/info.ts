export type Info = Record<string, unknown>;

export const IDENTITY = [
  "model_id",
  "model_sha",
  "model_revision",
  "model_dtype",
  "dimensions",
  "max_input_length",
  "max_concurrent_requests",
  "max_batch_tokens",
] as const;

export function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}
