import { describe, expect, it } from "vitest";
import { fromDraft, templateResolver, toDraft, type TemplateForm } from "./templateForm";

const form: TemplateForm = {
  name: "alert-triage",
  description: "",
  questions: [{ name: "severity", type: "score", optionsText: "1\n2\n\n3 ", min: "1", max: "3" }],
};

describe("the template editor's resolver", () => {
  it("turns lines into options and a range into numbers", () => {
    expect(toDraft(form)).toEqual({
      name: "alert-triage",
      description: "",
      questions: [
        { name: "severity", type: "score", options: ["1", "2", "3"], range: { min: 1, max: 3 } },
      ],
    });
    expect(toDraft(fromDraft(toDraft(form)))).toEqual(toDraft(form));
  });

  it("carries criteria through the editor, since the model reads it as the instructions", () => {
    const criteria = "How severe is this homelab alert?";
    const draft = {
      name: "alert-triage",
      description: "",
      questions: [{ name: "severity", type: "choice", options: ["info", "warning"], criteria }],
    };
    expect(toDraft(fromDraft(draft)).questions[0]?.criteria).toBe(criteria);
    expect(toDraft(fromDraft(toDraft(form))).questions[0]).not.toHaveProperty("criteria");
  });

  it("puts an option fault on the options field and a range fault on min", async () => {
    const [question] = form.questions;
    if (!question) throw new Error("the fixture has a question");
    const bad = { ...form, questions: [{ ...question, optionsText: "only", min: "3", max: "1" }] };
    const result = await templateResolver(bad, undefined, {
      fields: {},
      shouldUseNativeValidation: false,
    });
    const q = (result.errors as { questions?: Record<string, unknown>[] }).questions?.[0];
    expect(Object.keys(q ?? {}).sort()).toEqual(["min", "optionsText"]);
  });

  it("passes a valid form through", async () => {
    const result = await templateResolver(form, undefined, {
      fields: {},
      shouldUseNativeValidation: false,
    });
    expect(result.errors).toEqual({});
  });
});
