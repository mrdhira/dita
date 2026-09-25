import { act, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { HistoryPage } from "./HistoryPage";

const base = {
  created_at: "2026-09-24T01:00:00Z",
  schema: { name: "alert-triage", version: 1 },
  input_text: "t",
  model_id: "m",
  model_revision: "r",
  answers: [],
};

describe("HistoryPage", () => {
  it("says which predictions a human has answered", async () => {
    stubApi({
      "GET /decisions?limit=50": () => ({
        status: 200,
        body: {
          decisions: [
            { ...base, id: "a", correction: { answers: {}, outcomes: {} } },
            { ...base, id: "b", correction: null },
          ],
        },
      }),
    });
    renderAt("/decisions", "/decisions", <HistoryPage />);
    expect(await screen.findByText("recorded")).toBeTruthy();
    expect(screen.getByText("awaiting a human")).toBeTruthy();
  });

  it("says so when nothing is recorded", async () => {
    stubApi({ "GET /decisions?limit=50": () => ({ status: 200, body: { decisions: [] } }) });
    renderAt("/decisions", "/decisions", <HistoryPage />);
    expect(await screen.findByText("No predictions recorded yet.")).toBeTruthy();
  });
});

describe("HistoryPage: an answer no server wrote is not a worker's", () => {
  for (const [what, status, body] of [
    ["Caddy's empty 502, the orchestrator down", 502, ""],
    ["a proxy's bare 503", 503, "Service Unavailable"],
  ] as const) {
    it(what, async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        stubApi({ "GET /decisions?limit=50": () => ({ status, body }) });
        renderAt("/decisions", "/decisions", <HistoryPage />);
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
