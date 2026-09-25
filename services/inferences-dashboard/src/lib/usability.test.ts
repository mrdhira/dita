import { describe, expect, it } from "vitest";
import type { Template } from "../api/client";
import { usability } from "./usability";

const criteriaMissing = {
  path: "questions.0.criteria",
  message: "criteria are required: the model reads them as the question, not its name",
};

// The live alert-triage v2: valid nouls and no criteria, so it runs and the editor would refuse it.
const v2: Template = {
  name: "alert-triage",
  version: 2,
  description: "",
  created_at: "",
  questions: [
    { name: "severity", type: "choice", options: ["low", "high"] },
    { name: "fraud", type: "noul", options: ["false", "true"] },
  ],
};
const serverFault = { path: "questions.1.options", message: "the server's own words" };

describe("usability: the server's verdict, displayed", () => {
  it.each<[string, Template, { usable: boolean; faults: unknown[]; authoring: string[] }]>([
    [
      "usable by the server, with the editor's issues from the server",
      { ...v2, usable: true, faults: [], authoring_issues: [criteriaMissing] },
      { usable: true, faults: [], authoring: ["questions.0.criteria"] },
    ],
    [
      "usable by the server, though the editor's rules would refuse it",
      { ...v2, usable: true, faults: [] },
      { usable: true, faults: [], authoring: ["questions.0.criteria", "questions.1.criteria"] },
    ],
    [
      "unusable by the server, though the editor's rules would pass it",
      {
        ...v2,
        questions: v2.questions.map((q) => ({ ...q, criteria: "c" })),
        usable: false,
        faults: [serverFault],
        authoring_issues: [],
      },
      { usable: false, faults: [serverFault], authoring: [] },
    ],
    [
      "unusable by the server with no fault given: still unusable",
      { ...v2, usable: false, faults: [], authoring_issues: [] },
      { usable: false, faults: [], authoring: [] },
    ],
    [
      "an older orchestrator: the editor's rules, as a stopgap",
      v2,
      {
        usable: false,
        faults: [criteriaMissing, { ...criteriaMissing, path: "questions.1.criteria" }],
        authoring: ["questions.0.criteria", "questions.1.criteria"],
      },
    ],
  ])("%s", (_, template, want) => {
    const u = usability(template);
    expect({ ...u, authoring: u.authoring.map((f) => f.path) }).toEqual(want);
  });
});
