import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { makeQueryClient } from "../App";
import { stubApi } from "../test/render";
import { Layout } from "./Layout";

const workers = {
  workers: [
    { name: "inferences-embedding", url: "u", state: "ready", health_status: 200, info: {} },
    { name: "inferences-reranker", url: "u", state: "ready", health_status: 200, info: {} },
    {
      name: "inferences-system-one",
      url: "u",
      state: "not_running",
      health_status: null,
      info: null,
      error: "inferences-system-one is not running",
    },
  ],
};

function page() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={["/decide"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="decide" element={<p>the page itself</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the worker strip", () => {
  it("shows a worker that is not running as a state, and the page still renders", async () => {
    stubApi({ "GET /workers": () => ({ status: 200, body: workers }) });
    page();
    const strip = screen.getByRole("list", { name: "workers" });
    const items = (await within(strip).findAllByRole("listitem")).map((li) => li.textContent);
    expect(items).toEqual([
      "inferences-embedding: ● ready",
      "inferences-reranker: ● ready",
      "inferences-system-one: ■ stopped",
    ]);
    expect(screen.getByText("the page itself")).toBeTruthy();
  });

  it("survives the gateway itself failing", async () => {
    stubApi({ "GET /workers": () => ({ status: 400, body: { error: "no" } }) });
    page();
    expect(await screen.findByText("worker status unavailable")).toBeTruthy();
    expect(screen.getByText("the page itself")).toBeTruthy();
  });
});

describe("the navigation", () => {
  it("has the five sections, and no entry per service", async () => {
    stubApi({ "GET /workers": () => ({ status: 200, body: workers }) });
    page();
    const sections = screen.getByRole("navigation", { name: "sections" });
    expect(
      within(sections)
        .getAllByRole("link")
        .map((l) => l.textContent),
    ).toEqual(["Fleet", "Models", "Activity", "Jobs", "Settings"]);
    await screen.findAllByRole("listitem");
    for (const nav of screen.getAllByRole("navigation")) {
      expect(nav.textContent).not.toMatch(/embedding|reranker|system-one|ocr|stt|tts/);
    }
  });
});
