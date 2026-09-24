import type { FieldErrors, Resolver } from "react-hook-form";
import { draftSchema, type Draft, type QuestionType } from "../contract/schema";

/**
 * The editor's shape: options are typed one per line, a range as two optional fields.
 * Criteria has no input yet; it is carried so an edited template does not lose it.
 */
export interface TemplateForm {
  name: string;
  description: string;
  questions: {
    name: string;
    type: QuestionType;
    optionsText: string;
    min: string;
    max: string;
    criteria?: string;
  }[];
}

export function toDraft(form: TemplateForm): Draft {
  return {
    name: form.name.trim(),
    description: form.description,
    questions: form.questions.map((q) => {
      const options = q.optionsText
        .split("\n")
        .map((o) => o.trim())
        .filter((o) => o !== "");
      const hasRange = q.min.trim() !== "" || q.max.trim() !== "";
      return {
        name: q.name.trim(),
        type: q.type,
        options,
        ...(hasRange ? { range: { min: Number(q.min), max: Number(q.max) } } : {}),
        ...(q.criteria ? { criteria: q.criteria } : {}),
      };
    }),
  };
}

export function fromDraft(d: Draft): TemplateForm {
  return {
    name: d.name,
    description: d.description,
    questions: d.questions.map((q) => ({
      name: q.name,
      type: q.type as QuestionType,
      optionsText: q.options.join("\n"),
      min: q.range ? String(q.range.min) : "",
      max: q.range ? String(q.range.max) : "",
      ...(q.criteria === undefined ? {} : { criteria: q.criteria }),
    })),
  };
}

/**
 * The zod contract as a react-hook-form resolver. An issue on an option lands on that
 * question's options field, which is where the editor shows options.
 */
export const templateResolver: Resolver<TemplateForm> = (values) => {
  const parsed = draftSchema.safeParse(toDraft(values));
  if (parsed.success) return { values, errors: {} };
  const errors: Record<string, { type: string; message: string }> = {};
  for (const issue of parsed.error.issues) {
    const [head, index, field] = issue.path;
    const key =
      head === "questions" && typeof index === "number" && field !== undefined
        ? `questions.${index}.${field === "options" ? "optionsText" : field === "range" ? "min" : String(field)}`
        : issue.path.map(String).join(".");
    errors[key] ??= { type: "zod", message: issue.message };
  }
  return { values: {}, errors: nest(errors) as FieldErrors<TemplateForm> };
};

function nest(flat: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [path, value] of Object.entries(flat)) {
    const keys = path.split(".");
    let node: Record<string, unknown> = out;
    keys.slice(0, -1).forEach((k) => {
      node[k] ??= {};
      node = node[k] as Record<string, unknown>;
    });
    const last = keys[keys.length - 1] ?? "root";
    node[last === "" ? "root" : last] = value;
  }
  return out;
}
