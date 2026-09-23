import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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
