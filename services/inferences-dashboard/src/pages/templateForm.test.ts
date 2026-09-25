import { describe, expect, it } from "vitest";
import { fromDraft, templateResolver, toDraft, type TemplateForm } from "./templateForm";

const form: TemplateForm = {
  name: "alert-triage",
  description: "",
  questions: [
    {
      name: "severity",
      type: "score",
      optionsText: "1\n2\n\n3 ",
      min: "1",
      max: "3",
      criteria: " How severe is this homelab alert? ",
    },
  ],
};

describe("the template editor's resolver", () => {
  it("turns lines into options and a range into numbers", () => {
    expect(toDraft(form)).toEqual({
      name: "alert-triage",
      description: "",
      questions: [
        {
          name: "severity",
          type: "score",
          options: ["1", "2", "3"],
          range: { min: 1, max: 3 },
          criteria: "How severe is this homelab alert?",
        },
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
  });

  it("gives a stored question without criteria an empty field, which the rules refuse", async () => {
    const draft = {
      name: "alert-triage",
      description: "",
      questions: [{ name: "severity", type: "choice", options: ["info", "warning"] }],
    };
    const loaded = fromDraft(draft);
    expect(loaded.questions[0]?.criteria).toBe("");
    const result = await templateResolver(loaded, undefined, {
      fields: {},
      shouldUseNativeValidation: false,
    });
    const q = (result.errors as { questions?: Record<string, { message: string }>[] })
      .questions?.[0];
    expect(q?.criteria?.message).toBe(
      "criteria are required: the model reads them as the question, not its name",
    );
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
