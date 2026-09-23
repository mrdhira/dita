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
