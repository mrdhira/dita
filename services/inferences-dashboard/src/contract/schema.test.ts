// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  MAX_TEXT,
  chars,
  decisionRequestSchema,
  draftSchema,
  issuePaths,
  templateFaults,
} from "./schema";

interface Case {
  name: string;
  template: unknown;
  issues: string[];
}

interface TextCase {
  name: string;
  repeat: string;
  times: number;
  issues: string[];
}

// The same file the orchestrator's Go suite reads: both sides must reach every verdict.
const { cases, texts } = JSON.parse(
  readFileSync(new URL("../../../../specs/decisions/schema-cases.json", import.meta.url), "utf8"),
) as { cases: Case[]; texts: TextCase[] };

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

describe("the shared text cases", () => {
  it("hold a text of exactly MAX_TEXT characters and one of MAX_TEXT + 1", () => {
    const lengths = texts.map((c) => chars(c.repeat.repeat(c.times)));
    expect(lengths).toContain(MAX_TEXT);
    expect(lengths).toContain(MAX_TEXT + 1);
  });
  it.each(texts.map((c) => [c.name, c] as const))("%s", (_, c) => {
    const parsed = decisionRequestSchema.safeParse({
      text: c.repeat.repeat(c.times),
      schema: { name: "alert-triage", version: 1 },
    });
    expect(parsed.success ? [] : issuePaths(parsed.error)).toEqual(c.issues);
  });
});

// The worker refuses a noul asked with any other options, so a template that says yes/no
// would save and then fail at every /decide.
describe("a noul question's options are exactly false and true", () => {
  const draft = (options: string[]) => ({
    name: "alert-triage",
    description: "",
    questions: [{ name: "fraud", type: "noul", options, criteria: "Is this fraud?" }],
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
      questions: [{ name: "fraud", type: "choice", options: ["yes", "no"], criteria: "Fraud?" }],
    });
    expect(parsed.success).toBe(true);
  });
});

const faultsOf = (question: Record<string, unknown>) => {
  const parsed = draftSchema.safeParse({
    name: "alert-triage",
    description: "",
    questions: [
      { name: "q", type: "choice", options: ["low", "high"], criteria: "c", ...question },
    ],
  });
  return parsed.success
    ? []
    : parsed.error.issues.map((i) => [i.path.join("."), i.message] as const);
};

// Without criteria the model sees only the question's name, and the answer moves materially.
describe("a question's criteria are required", () => {
  it.each([
    ["present", { criteria: "How severe is this homelab alert?" }, []],
    ["missing", { criteria: undefined }, ["questions.0.criteria"]],
    ["empty", { criteria: "" }, ["questions.0.criteria"]],
    ["blank", { criteria: " \n\t" }, ["questions.0.criteria"]],
  ])("%s", (_, question, paths) => {
    expect(faultsOf(question).map(([p]) => p)).toEqual(paths);
  });
  it("says why, for the editor to show", () => {
    expect(faultsOf({ criteria: "" })).toEqual([
      [
        "questions.0.criteria",
        "criteria are required: the model reads them as the question, not its name",
      ],
    ]);
  });
});

// The worker reads a score's options as its levels in order and refuses a range that
// contradicts them, so a score the editor lets through must be one the runtime answers.
describe("a score question's options are numeric levels that rise", () => {
  const score = (options: string[], range?: { min: number; max: number }) =>
    faultsOf({ type: "score", options, ...(range ? { range } : {}) });
  const rising = "a score question's options are numeric levels that must rise";
  it.each([
    ["integers, rising", ["1", "2", "3"], undefined, []],
    ["decimals and negatives, rising", ["-1", "0.5", "20"], undefined, []],
    // An exponent is not a level the orchestrator's float() reads, so neither side may accept one:
    // the shared cases carry the same rule, and this file used to disagree with them.
    ["an exponent", ["1", "2e1"], undefined, [["questions.0.options", rising]]],
    ["rising within its range", ["1", "3", "5"], { min: 1, max: 5 }, []],
    ["a word", ["low", "high"], undefined, [["questions.0.options", rising]]],
    ["hex, which float() refuses", ["0x1", "0x2"], undefined, [["questions.0.options", rising]]],
    ["falling", ["3", "2", "1"], undefined, [["questions.0.options", rising]]],
    ["rising, then falling", ["1", "3", "2"], undefined, [["questions.0.options", rising]]],
    [
      "above its range",
      ["1", "2", "6"],
      { min: 1, max: 5 },
      [
        [
          "questions.0.range",
          "range 1..5 contradicts its options: numeric levels must rise within the range",
        ],
      ],
    ],
    [
      "below its range",
      ["0", "1"],
      { min: 1, max: 5 },
      [
        [
          "questions.0.range",
          "range 1..5 contradicts its options: numeric levels must rise within the range",
        ],
      ],
    ],
    [
      "a range that is itself wrong is reported once",
      ["1", "2"],
      { min: 2, max: 2 },
      [["questions.0.range", "min must be below max"]],
    ],
  ])("%s", (_, options, range, faults) => {
    expect(score(options, range)).toEqual(faults);
  });
  it("applies only to score: a choice may still be words in any order", () => {
    expect(faultsOf({ options: ["high", "low"] })).toEqual([]);
  });
});

describe("a stored template under today's rules", () => {
  const stored = {
    name: "alert-triage",
    version: 1,
    created_at: "2026-09-24T14:38:00Z",
    retired: false,
    description: "",
    questions: [
      { name: "severity", type: "choice", options: ["low", "high"], criteria: "How severe?" },
      { name: "fraud", type: "noul", options: ["yes", "no", "unknown"], criteria: "Fraud?" },
    ],
  };
  it("ignores the store's own fields and names the fault at its path", () => {
    expect(templateFaults(stored)).toEqual([
      {
        path: "questions.1.options",
        message: 'a noul question\'s options are exactly "false" and "true"',
      },
    ]);
  });
  it("finds nothing wrong with a template that passes", () => {
    const [severity] = stored.questions;
    const fraud = { name: "fraud", type: "noul", options: ["false", "true"], criteria: "Fraud?" };
    expect(severity && templateFaults({ ...stored, questions: [severity, fraud] })).toEqual([]);
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
