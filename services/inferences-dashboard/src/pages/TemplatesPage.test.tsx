import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { TemplatesPage } from "./TemplatesPage";

const saved = {
  name: "alert-triage",
  version: 1,
  description: "",
  created_at: "",
  questions: [{ name: "severity", type: "choice", options: ["low", "high"] }],
};

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
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

    expect(await screen.findByText("alert-triage v1 saved")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "alert-triage",
      description: "",
      questions: [{ name: "severity", type: "choice", options: ["low", "high"] }],
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
});
