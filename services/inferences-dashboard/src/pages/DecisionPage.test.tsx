import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Decision } from "../api/client";
import { renderAt, stubApi } from "../test/render";
import { DecisionPage } from "./DecisionPage";

const decision: Decision = {
  id: "p1",
  created_at: "2026-09-24T01:00:00Z",
  schema: { name: "alert-triage", version: 1 },
  input_text: "three failed logins then a transfer",
  model_id: "stub-system-one",
  model_revision: "STUB-not-a-model",
  answers: [
    {
      question: "severity",
      type: "choice",
      confidence: 0.6,
      options: [
        { option: "high", probability: 0.6 },
        { option: "medium", probability: 0.25 },
        { option: "low", probability: 0.15 },
      ],
    },
    {
      question: "fraud",
      type: "noul",
      confidence: 0.7,
      options: [
        { option: "no", probability: 0.7 },
        { option: "yes", probability: 0.3 },
      ],
    },
  ],
  correction: null,
};

const corrected: Decision = {
  ...decision,
  correction: {
    prediction_id: "p1",
    corrected_at: "2026-09-24T01:01:00Z",
    answers: { severity: "high", fraud: "yes" },
    outcomes: { severity: "accepted", fraud: "corrected" },
  },
};

describe("the correction on a decision page", () => {
  it("starts with nothing chosen, so recording needs every question answered", async () => {
    stubApi({ "GET /decisions/p1": () => ({ status: 200, body: decision }) });
    renderAt("/decisions/p1", "/decisions/:id", <DecisionPage />);
    const record = await screen.findByRole("button", { name: "Record answer" });
    expect((record as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByRole("radio").every((r) => !(r as HTMLInputElement).checked)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "use suggestion (high)" }));
    expect((record as HTMLButtonElement).disabled).toBe(true);
  });

  it("records the pair and then shows it, in place of the form", async () => {
    const calls = stubApi({
      "GET /decisions/p1": () => ({ status: 200, body: decision }),
      "POST /decisions/p1/correction": () => ({ status: 201, body: corrected }),
    });
    renderAt("/decisions/p1", "/decisions/:id", <DecisionPage />);
    await userEvent.click(await screen.findByRole("button", { name: "use suggestion (high)" }));
    await userEvent.click(screen.getByRole("radio", { name: "yes" }));
    await userEvent.click(screen.getByRole("button", { name: "Record answer" }));

    await screen.findByRole("region", { name: "recorded answer" });
    expect(screen.getByTestId("recorded-severity").textContent).toContain("high");
    expect(screen.getByTestId("recorded-fraud").textContent).toContain("(corrected)");
    expect(screen.queryByRole("button", { name: "Record answer" })).toBeNull();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      answers: { severity: "high", fraud: "yes" },
    });
  });

  it("shows a refused second correction without retrying it", async () => {
    const calls = stubApi({
      "GET /decisions/p1": () => ({ status: 200, body: decision }),
      "POST /decisions/p1/correction": () => ({
        status: 409,
        body: {
          error: "a correction is already recorded for this prediction; the first stands",
          error_type: "Conflict",
        },
      }),
    });
    renderAt("/decisions/p1", "/decisions/:id", <DecisionPage />);
    await userEvent.click(await screen.findByRole("button", { name: "use suggestion (high)" }));
    await userEvent.click(screen.getByRole("button", { name: "use suggestion (no)" }));
    await userEvent.click(screen.getByRole("button", { name: "Record answer" }));

    expect((await screen.findByRole("alert")).textContent).toContain("already corrected");
    await waitFor(() => {
      expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    });
  });

  it("shows a stored correction straight away, as a reload does", async () => {
    stubApi({ "GET /decisions/p1": () => ({ status: 200, body: corrected }) });
    renderAt("/decisions/p1", "/decisions/:id", <DecisionPage />);
    expect((await screen.findByTestId("recorded-fraud")).textContent).toContain("yes");
  });
});
