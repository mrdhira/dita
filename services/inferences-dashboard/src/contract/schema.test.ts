// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { draftSchema, issuePaths } from "./schema";

interface Case {
  name: string;
  template: unknown;
  issues: string[];
}

// The same file the orchestrator's Go suite reads: both sides must reach every verdict.
const { cases } = JSON.parse(
  readFileSync(new URL("../../../../specs/decisions/schema-cases.json", import.meta.url), "utf8"),
) as { cases: Case[] };

describe("the shared schema cases", () => {
  it("loaded", () => {
    expect(cases.length).toBeGreaterThanOrEqual(20);
  });
  it.each(cases.map((c) => [c.name, c] as const))("%s", (_, c) => {
    const parsed = draftSchema.safeParse(c.template);
    const paths = parsed.success ? [] : [...new Set(issuePaths(parsed.error))].sort();
    expect(paths).toEqual(c.issues);
  });
});

// The worker refuses a noul asked with any other options, so a template that says yes/no
// would save and then fail at every /decide.
describe("a noul question's options are exactly false and true", () => {
  const draft = (options: string[]) => ({
    name: "alert-triage",
    description: "",
    questions: [{ name: "fraud", type: "noul", options }],
  });
  it.each([
    ["false, true", ["false", "true"], []],
    ["true, false", ["true", "false"], []],
    ["yes, no: accepted before this rule", ["yes", "no"], ["questions.0.options"]],
    ["yes, no, unknown", ["yes", "no", "unknown"], ["questions.0.options"]],
    ["false, true and a third", ["false", "true", "unknown"], ["questions.0.options"]],
    ["the right words, capitalised", ["False", "True"], ["questions.0.options"]],
    ["false twice", ["false", "false"], ["questions.0.options", "questions.0.options.1"]],
  ])("%s", (_, options, issues) => {
    const parsed = draftSchema.safeParse(draft(options));
    expect(parsed.success ? [] : [...new Set(issuePaths(parsed.error))].sort()).toEqual(issues);
  });
  it("names the rule, so the editor can say why", () => {
    const parsed = draftSchema.safeParse(draft(["yes", "no"]));
    expect(parsed.error?.issues.map((i) => i.message)).toEqual([
      'a noul question\'s options are exactly "false" and "true"',
    ]);
  });
  it("applies only to noul: a choice may still say yes and no", () => {
    const parsed = draftSchema.safeParse({
      ...draft([]),
      questions: [{ name: "fraud", type: "choice", options: ["yes", "no"] }],
    });
    expect(parsed.success).toBe(true);
  });
});

describe("what zod refuses before the rules run", () => {
  it.each([
    ["an unknown field", { name: "ab", description: "", questions: [], extra: 1 }],
    [
      "options that are not a list",
      { name: "ab", description: "", questions: [{ name: "a", type: "choice", options: "x" }] },
    ],
  ])("%s", (_, value) => {
    expect(draftSchema.safeParse(value).success).toBe(false);
  });
});
