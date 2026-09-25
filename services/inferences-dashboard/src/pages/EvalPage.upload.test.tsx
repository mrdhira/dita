import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderAt, stubApi } from "../test/render";
import { EvalPage } from "./EvalPage";

const result = {
  id: "e1",
  created_at: "2026-09-24T01:00:00Z",
  name: "set.csv",
  rows: 2,
  accuracy: 1,
  brier: 0.05,
  ece: 0.15,
  classes: [
    { class: "a", support: 1, predicted: 1, correct: 1, precision: 1, recall: 1 },
    { class: "b", support: 1, predicted: 1, correct: 1, precision: 1, recall: 1 },
  ],
  baseline: { class: "a", accuracy: 0.5, brier: 0.5 },
  beats_baseline: true,
};

describe("the eval upload", () => {
  it("parses the CSV, sends its rows and shows the run against the baseline", async () => {
    const calls = stubApi({
      "GET /evaluations": () => ({ status: 200, body: { evaluations: [] } }),
      "POST /evaluations": () => ({ status: 201, body: result }),
    });
    renderAt("/eval", "/eval", <EvalPage />);
    const file = new File(["label,p:a,p:b\na,0.9,0.1\nb,0.2,0.8\n"], "set.csv", {
      type: "text/csv",
    });
    await userEvent.upload(screen.getByLabelText(/Labelled CSV/), file);

    expect((await screen.findByTestId("verdict")).textContent).toBe(
      "beats the majority-class baseline",
    );
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "set.csv",
      rows: [
        { label: "a", probabilities: { a: 0.9, b: 0.1 } },
        { label: "b", probabilities: { a: 0.2, b: 0.8 } },
      ],
    });
  });

  it("refuses a file it cannot read, before any request", async () => {
    const calls = stubApi({
      "GET /evaluations": () => ({ status: 200, body: { evaluations: [] } }),
    });
    renderAt("/eval", "/eval", <EvalPage />);
    await userEvent.upload(
      screen.getByLabelText(/Labelled CSV/),
      new File(["p:a,p:b\n1,0\n"], "bad.csv"),
    );
    expect((await screen.findByRole("alert")).textContent).toContain("no `label` column");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("lists previous runs with their baseline", async () => {
    stubApi({ "GET /evaluations": () => ({ status: 200, body: { evaluations: [result] } }) });
    renderAt("/eval", "/eval", <EvalPage />);
    expect(
      (await screen.findByText(/set.csv: accuracy 1.000 vs baseline 0.500/)).textContent,
    ).toBeTruthy();
  });
});

describe("the calibration panel", () => {
  it("says the temperatures are the identity rather than implying a fitted calibration", () => {
    stubApi({ "GET /evaluations": () => ({ status: 200, body: { evaluations: [] } }) });
    renderAt("/eval", "/eval", <EvalPage />);
    const panel = screen.getByRole("region", { name: "calibration" });
    expect(panel.querySelector("h2")?.textContent).toBe("Calibration: none fitted");
    expect(screen.getByTestId("calibration").textContent).toMatch(
      /Every temperature in the checkpoint is 1\.0, so temperature scaling is the identity\s+transform/,
    );
  });
});
