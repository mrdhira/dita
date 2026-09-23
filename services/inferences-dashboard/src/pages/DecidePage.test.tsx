import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { DecidePage } from "./DecidePage";

const templates = {
  templates: [
    {
      name: "alert-triage",
      version: 2,
      description: "",
      created_at: "",
      questions: [{ name: "severity", type: "choice", options: ["low", "high"] }],
    },
  ],
};

describe("DecidePage", () => {
  it.each([
    [
      "no model resident",
      503,
      {
        error: "no model is loaded; the orchestrator has not loaded one yet",
        error_type: "Unhealthy",
      },
      "The worker has no model loaded",
    ],
    [
      "the worker not running",
      503,
      { error: "x", reason: "not_running", worker: "inferences-system-one" },
      "The decision worker is not running",
    ],
    ["an engine refusal", 422, { error: "refused" }, "The engine refused this request"],
    [
      "a malformed schema",
      400,
      { error: "x", issues: [{ path: "questions.0.options", message: "bad" }] },
      "malformed",
    ],
  ])("shows %s, and asks once", async (_, status, body, message) => {
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: templates }),
      "POST /decisions": () => ({ status, body }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    await screen.findByRole("option", { name: /alert-triage v2/ });
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect((await screen.findByRole("alert")).textContent).toContain(message);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      text: "an alert",
      schema: { name: "alert-triage", version: 2 },
    });
  });

  it("refuses blank text before any request", async () => {
    const calls = stubApi({ "GET /schemas": () => ({ status: 200, body: templates }) });
    renderAt("/decide", "/decide", <DecidePage />);
    await screen.findByRole("option", { name: /alert-triage v2/ });
    await userEvent.type(screen.getByRole("textbox"), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect((await screen.findByRole("alert")).textContent).toContain("text");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("goes to the new prediction once it is stored", async () => {
    stubApi({
      "GET /schemas": () => ({ status: 200, body: templates }),
      "POST /decisions": () => ({ status: 201, body: { id: "p9" } }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    await screen.findByRole("option", { name: /alert-triage v2/ });
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect(await screen.findByText("elsewhere")).toBeTruthy();
  });
});
