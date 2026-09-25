import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { TemplatesPage } from "./TemplatesPage";

const saved = {
  name: "alert-triage",
  version: 1,
  description: "",
  created_at: "",
  questions: [
    { name: "severity", type: "choice", options: ["low", "high"], criteria: "How severe?" },
  ],
};
const criteriaLabel =
  "Instructions for the model: what this question asks, in the words the model reads (criteria)";

describe("TemplatesPage", () => {
  it("saves a valid draft as a new version", async () => {
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [] } }),
      "POST /schemas": () => ({ status: 201, body: saved }),
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "alert-triage");
    await userEvent.type(screen.getByRole("textbox", { name: "question 1 name" }), "severity");
    await userEvent.type(
      screen.getByRole("textbox", { name: "question 1 options" }),
      "low{enter}high",
    );
    await userEvent.type(
      within(screen.getByRole("group", { name: "question 1" })).getByRole("textbox", {
        name: criteriaLabel,
      }),
      "How severe?",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    expect(await screen.findByText("alert-triage v1 saved")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "alert-triage",
      description: "",
      questions: [
        { name: "severity", type: "choice", options: ["low", "high"], criteria: "How severe?" },
      ],
    });
  });

  it("refuses an invalid draft on its fields, before any request", async () => {
    const calls = stubApi({ "GET /schemas": () => ({ status: 200, body: { templates: [] } }) });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Bad Name");
    await userEvent.type(screen.getByRole("textbox", { name: "question 1 options" }), "only");
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    expect(await screen.findByText(/a template name is 2-64/)).toBeTruthy();
    expect(screen.getByText("between 2 and 20 options")).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("says that saving an existing template writes the next version", async () => {
    stubApi({ "GET /schemas": () => ({ status: 200, body: { templates: [saved] } }) });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "alert-triage v1" }));
    expect(screen.getByText("editing alert-triage: saving writes v2")).toBeTruthy();
    const options = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "question 1 options",
    });
    expect(options.value).toBe("low\nhigh");
  });

  it("keeps a loaded template's criteria when it is saved as the next version", async () => {
    const criteria = "How severe is this homelab alert?";
    const withCriteria = {
      ...saved,
      questions: [{ name: "severity", type: "choice", options: ["low", "high"], criteria }],
    };
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [withCriteria] } }),
      "POST /schemas": () => ({ status: 201, body: { ...withCriteria, version: 2 } }),
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "alert-triage v1" }));
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    expect(await screen.findByText("alert-triage v2 saved")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "alert-triage",
      description: "",
      questions: [{ name: "severity", type: "choice", options: ["low", "high"], criteria }],
    });
  });

  it("asks for criteria per question, explains them, and refuses a question without", async () => {
    const calls = stubApi({ "GET /schemas": () => ({ status: 200, body: { templates: [] } }) });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(screen.getByRole("button", { name: "add question" }));
    const second = within(screen.getByRole("group", { name: "question 2" }));
    const input = second.getByRole("textbox", { name: criteriaLabel });
    expect(input.getAttribute("aria-describedby")).toBe("question-1-criteria-help");
    expect(document.getElementById("question-1-criteria-help")?.textContent).toMatch(
      /^Required\. The model reads this, not the question's name/,
    );
    await userEvent.type(
      within(screen.getByRole("group", { name: "question 1" })).getByRole("textbox", {
        name: criteriaLabel,
      }),
      "How severe?",
    );
    await userEvent.type(input, "   ");
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    const message = "criteria are required: the model reads them as the question, not its name";
    expect(await second.findByText(message)).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "question 1" })).queryByText(message),
    ).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("refuses a score whose options are not rising numbers, before any request", async () => {
    const calls = stubApi({ "GET /schemas": () => ({ status: 200, body: { templates: [] } }) });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "alert-triage");
    await userEvent.type(screen.getByRole("textbox", { name: "question 1 name" }), "risk");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "question 1 type" }),
      "score",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "question 1 options" }),
      "3{enter}2{enter}1",
    );
    await userEvent.type(screen.getByRole("textbox", { name: criteriaLabel }), "How risky?");
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    expect(
      await screen.findByText("a score question's options are numeric levels that must rise"),
    ).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("retires a version only after it is confirmed, and shows it retired", async () => {
    const v2 = { ...saved, version: 2 };
    let retired = false;
    const calls = stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [{ ...v2, retired: false }] } }),
      "GET /schemas/alert-triage/versions": () => ({
        status: 200,
        body: {
          versions: [
            { ...saved, retired },
            { ...v2, retired: false },
          ],
        },
      }),
      "POST /schemas/alert-triage/versions/1/retire": () => {
        retired = true;
        return {
          status: 200,
          body: { name: "alert-triage", version: 1, retired: true, retired_at: "t" },
        };
      },
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "alert-triage v2" }));
    const versions = within(screen.getByRole("region", { name: "versions of alert-triage" }));
    await userEvent.click(await versions.findByRole("button", { name: "retire v1" }));
    expect(
      versions.getByText("Decide will no longer offer v1. Its predictions stay."),
    ).toBeTruthy();
    await userEvent.click(versions.getByRole("button", { name: "cancel" }));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);

    await userEvent.click(versions.getByRole("button", { name: "retire v1" }));
    await userEvent.click(versions.getByRole("button", { name: "Retire v1" }));
    expect(await versions.findByText("retired")).toBeTruthy();
    expect(versions.queryByRole("button", { name: "retire v1" })).toBeNull();
    expect(versions.getByRole("button", { name: "retire v2" })).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      "/schemas/alert-triage/versions/1/retire",
    ]);
  });

  it("marks a retired template in the list", async () => {
    stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [{ ...saved, retired: true }] } }),
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    const item = (await screen.findByRole("button", { name: "alert-triage v1" })).closest("li");
    expect(item?.textContent).toBe("alert-triage v1retired");
  });

  it("loads a version that runs but predates the editor's rules, and marks what to update", async () => {
    const old = {
      ...saved,
      version: 2,
      questions: [{ name: "severity", type: "choice", options: ["low", "high"] }],
      usable: true,
      faults: [],
      authoring_issues: [
        {
          path: "questions.0.criteria",
          message: "criteria are required: the model reads them as the question, not its name",
        },
      ],
    };
    stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [old] } }),
      "GET /schemas/alert-triage/versions": () => ({ status: 200, body: { versions: [old] } }),
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "alert-triage v2" }));
    const status = await screen.findByRole("status");
    expect(status.textContent).toBe(
      "This version runs on Decide, but needs updating before the next one can be saved:" +
        "questions.0.criteria: criteria are required: the model reads them as the question, not its name",
    );
    const question = within(screen.getByRole("group", { name: "question 1" }));
    expect(
      await question.findByText(
        "criteria are required: the model reads them as the question, not its name",
      ),
    ).toBeTruthy();
  });

  it("says a loaded version the server will not run cannot be used, not that it runs", async () => {
    const refused = {
      ...saved,
      usable: false,
      faults: [{ path: "questions.0.options", message: "the runtime's own reason" }],
      authoring_issues: [{ path: "questions.0.options", message: "the editor's own reason" }],
    };
    stubApi({
      "GET /schemas": () => ({ status: 200, body: { templates: [refused] } }),
      "GET /schemas/alert-triage/versions": () => ({ status: 200, body: { versions: [refused] } }),
    });
    renderAt("/templates", "/templates", <TemplatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "alert-triage v1" }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "This version cannot be used on Decide; the next one must fix:" +
        "questions.0.options: the editor's own reason",
    );
  });
});

describe("TemplatesPage: an answer no server wrote is not a worker's", () => {
  for (const [what, status, body] of [
    ["Caddy's empty 502, the orchestrator down", 502, ""],
    ["a proxy's bare 503", 503, "Service Unavailable"],
  ] as const) {
    it(what, async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        stubApi({ "GET /schemas": () => ({ status, body }) });
        renderAt("/templates", "/templates", <TemplatesPage />);
        await act(() => vi.advanceTimersByTimeAsync(5_000));
        const banner = screen.getByRole("alert").textContent;
        expect(banner).toContain(
          `The orchestrator, or something in front of it, answered ${status}`,
        );
        expect(banner).not.toMatch(/no model loaded|The worker/);
      } finally {
        vi.useRealTimers();
      }
    });
  }
});
