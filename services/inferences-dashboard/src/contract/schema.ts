import { z } from "zod";

// The orchestrator's decisions/schema.go applies the same rules; specs/decisions/schema-cases.json
// holds both to the same verdicts, path for path. The rules live in one superRefine so every
// fault is reported at once, as the Go side does.

export const MAX_QUESTIONS = 20;
export const MIN_OPTIONS = 2;
export const MAX_OPTIONS = 20;
export const MAX_OPTION_LEN = 100;
export const MAX_DESCRIPTION = 500;
export const MAX_TEXT = 20000;
export const QUESTION_TYPES = ["noul", "choice", "score"] as const;
export type QuestionType = (typeof QUESTION_TYPES)[number];

const TEMPLATE_NAME = /^[a-z][a-z0-9-]{1,63}$/;
const QUESTION_NAME = /^[a-z][a-z0-9_]{0,63}$/;

/** Length in code points, as Go counts runes: `.length` would count UTF-16 units. */
export const chars = (s: string): number => Array.from(s).length;

const range = z.strictObject({ min: z.number(), max: z.number() });

export const questionSchema = z.strictObject({
  name: z.string(),
  type: z.string(),
  options: z.array(z.string()),
  range: range.optional(),
});

export const draftSchema = z
  .strictObject({
    name: z.string(),
    description: z.string(),
    questions: z.array(questionSchema),
  })
  .superRefine((d, ctx) => {
    const issue = (path: (string | number)[], message: string) => {
      ctx.addIssue({ code: "custom", path, message });
    };
    if (!TEMPLATE_NAME.test(d.name)) {
      issue(
        ["name"],
        "a template name is 2-64 lowercase letters, digits or dashes, starting with a letter",
      );
    }
    if (chars(d.description) > MAX_DESCRIPTION) {
      issue(["description"], `at most ${MAX_DESCRIPTION} characters`);
    }
    if (d.questions.length < 1 || d.questions.length > MAX_QUESTIONS) {
      issue(["questions"], `between 1 and ${MAX_QUESTIONS} questions`);
    }
    const seen = new Set<string>();
    d.questions.forEach((q, i) => {
      if (!QUESTION_NAME.test(q.name)) {
        issue(
          ["questions", i, "name"],
          "a question name is lowercase letters, digits or underscores, starting with a letter",
        );
      } else if (seen.has(q.name)) {
        issue(["questions", i, "name"], `question "${q.name}" appears twice`);
      }
      seen.add(q.name);
      if (!(QUESTION_TYPES as readonly string[]).includes(q.type)) {
        issue(["questions", i, "type"], `type must be one of ${QUESTION_TYPES.join(", ")}`);
      }
      if (q.options.length < MIN_OPTIONS || q.options.length > MAX_OPTIONS) {
        issue(["questions", i, "options"], `between ${MIN_OPTIONS} and ${MAX_OPTIONS} options`);
      }
      const options = new Set<string>();
      q.options.forEach((o, j) => {
        if (o.trim() === "" || chars(o) > MAX_OPTION_LEN) {
          issue(
            ["questions", i, "options", j],
            `an option is 1-${MAX_OPTION_LEN} characters and not blank`,
          );
        } else if (options.has(o)) {
          issue(["questions", i, "options", j], `option "${o}" appears twice`);
        }
        options.add(o);
      });
      if (q.range) {
        if (q.type !== "score")
          issue(["questions", i, "range"], "only a score question has a range");
        else if (!(q.range.min < q.range.max))
          issue(["questions", i, "range"], "min must be below max");
      }
    });
  });

export type Draft = z.infer<typeof draftSchema>;
export type Question = z.infer<typeof questionSchema>;

export const decisionRequestSchema = z.strictObject({
  text: z
    .string()
    .refine(
      (t) => t.trim() !== "" && chars(t) <= MAX_TEXT,
      `the text is 1-${MAX_TEXT} characters and not blank`,
    ),
  schema: z.strictObject({ name: z.string().min(1), version: z.number().int().positive() }),
});

export const evaluationRowSchema = z.strictObject({
  label: z.string().min(1),
  probabilities: z.record(z.string(), z.number().min(0).max(1)),
});

/** zod's issues as the orchestrator reports them: dotted paths. */
export function issuePaths(error: z.ZodError): string[] {
  return error.issues.map((i) => i.path.join("."));
}
