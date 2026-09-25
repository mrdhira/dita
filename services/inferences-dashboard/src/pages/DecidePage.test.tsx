import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { DecidePage } from "./DecidePage";

const usable = {
  name: "alert-triage",
  version: 2,
  description: "",
  created_at: "",
  questions: [{ name: "severity", type: "choice", options: ["low", "high"], criteria: "Severe?" }],
};
const templates = { templates: [usable] };

// The deployed store's first template: saved before the noul rule, refused at every /decide.
const unusable = {
  name: "alert-triage",
  version: 1,
  description: "",
  created_at: "2026-09-24T14:38:00Z",
  questions: [
    { name: "severity", type: "choice", options: ["low", "high"], criteria: "Severe?" },
    { name: "fraud", type: "noul", options: ["yes", "no", "unknown"], criteria: "Fraud?" },
  ],
};
const noulReason = 'a noul question\'s options are exactly "false" and "true"';
const other = { ...usable, name: "port-scan", version: 3 };

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
    [
      "the worker's own HTML 500, which the orchestrator relays as JSON naming the worker",
      500,
      {
        error: "inferences-system-one answered 500 with a body of type text/html, not a JSON error",
        error_type: "Backend",
        worker: "inferences-system-one",
      },
      "inferences-system-one answered 500",
    ],
    [
      "Caddy's empty 502, the orchestrator down: not the worker",
      502,
      "",
      "The orchestrator, or something in front of it, answered 502",
    ],
    [
      "a proxy's bare 503: not the worker's model",
      503,
      "Service Unavailable",
      "The orchestrator, or something in front of it, answered 503",
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
    const alert = await screen.findByRole("alert");
    const banner = alert.textContent;
    expect(banner).toContain(message);
    expect(banner).not.toContain("<");
    if (message === "inferences-system-one answered 500") {
      expect(alert.querySelector("p")?.textContent).toBe(message);
      expect(banner).not.toMatch(/nothing about any worker/);
    }
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

  it("never defaults to a stored template the rules refuse, and says why", async () => {
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [unusable, other] } }),
      "POST /decisions": () => ({ status: 201, body: { id: "p9" } }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    const refused = await screen.findByRole<HTMLOptionElement>("option", { name: /v1/ });
    expect(refused.textContent).toBe(`alert-triage v1 — cannot be used: ${noulReason}`);
    const select = screen.getByRole<HTMLSelectElement>("combobox", { name: "Schema template" });
    expect(select.value).toBe("port-scan@3");
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect(await screen.findByText("elsewhere")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      text: "an alert",
      schema: { name: "port-scan", version: 3 },
    });
  });

  it("refuses a chosen unusable template with the rule's reason, before any request", async () => {
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [unusable, other] } }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    await screen.findByRole("option", { name: /v1/ });
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Schema template" }),
      "alert-triage@1",
    );
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      `Fix before sending: alert-triage v1 cannot be used: questions.1.options: ${noulReason}`,
    );
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("selects nothing when no template can be used, and says so", async () => {
    stubApi({ "GET /schemas": () => ({ status: 200, body: { templates: [unusable] } }) });
    renderAt("/decide", "/decide", <DecidePage />);
    expect(await screen.findByText(/None of these templates can be used/)).toBeTruthy();
    const select = screen.getByRole<HTMLSelectElement>("combobox", { name: "Schema template" });
    expect(select.value).toBe("");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Decide" }).disabled).toBe(true);
  });

  it("lists a retired version as retired, and never selects it", async () => {
    stubApi({
      "GET /schemas": () => ({
        status: 200,
        body: {
          templates: [
            { ...usable, retired: true },
            { ...other, retired: false },
          ],
        },
      }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    const retired = await screen.findByRole<HTMLOptionElement>("option", {
      name: "alert-triage v2 — retired",
    });
    expect(retired.disabled).toBe(true);
    const select = screen.getByRole<HTMLSelectElement>("combobox", { name: "Schema template" });
    expect(select.value).toBe("port-scan@3");
  });

  // The live store's alert-triage v2 has no criteria: the runtime runs it, the editor would not.
  const liveV2 = {
    ...usable,
    questions: [
      { name: "severity", type: "choice", options: ["low", "high"] },
      { name: "fraud", type: "noul", options: ["false", "true"] },
    ],
    retired: false,
    usable: true,
    faults: [],
    authoring_issues: [
      {
        path: "questions.0.criteria",
        message: "criteria are required: the model reads them as the question, not its name",
      },
    ],
  };

  it("runs a version the server marks usable, and says what needs updating", async () => {
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [liveV2] } }),
      "POST /decisions": () => ({ status: 201, body: { id: "p9" } }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    expect(
      (await screen.findByRole<HTMLOptionElement>("option", { name: /alert-triage v2/ }))
        .textContent,
    ).toBe("alert-triage v2 (2 questions) — runs, but needs updating");
    const select = screen.getByRole<HTMLSelectElement>("combobox", { name: "Schema template" });
    expect(select.value).toBe("alert-triage@2");
    expect(
      screen.getByText(/runs, but was saved before today's editing rules/).textContent,
    ).toContain("questions.0.criteria: criteria are required");
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect(await screen.findByText("elsewhere")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      text: "an alert",
      schema: { name: "alert-triage", version: 2 },
    });
  });

  it("refuses a version the server marks unusable, in the server's words", async () => {
    const refused = {
      ...usable,
      version: 4,
      usable: false,
      faults: [{ path: "questions.0.options", message: "the runtime's own reason" }],
      authoring_issues: [],
    };
    const calls = stubApi({
      "GET /schemas": () => ({
        status: 200,
        body: { templates: [refused, { ...other, usable: true }] },
      }),
    });
    renderAt("/decide", "/decide", <DecidePage />);
    expect((await screen.findByRole<HTMLOptionElement>("option", { name: /v4/ })).textContent).toBe(
      "alert-triage v4 — cannot be used: the runtime's own reason",
    );
    const select = screen.getByRole<HTMLSelectElement>("combobox", { name: "Schema template" });
    expect(select.value).toBe("port-scan@3");
    await userEvent.selectOptions(select, "alert-triage@4");
    await userEvent.type(screen.getByRole("textbox"), "an alert");
    await userEvent.click(screen.getByRole("button", { name: "Decide" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Fix before sending: alert-triage v4 cannot be used: questions.0.options: the runtime's own reason",
    );
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });
});
