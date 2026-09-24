import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Evaluation } from "../api/client";
import { EvaluationResult } from "./EvalPage";

const result: Evaluation = {
  id: "e1",
  created_at: "",
  name: "alerts.csv",
  rows: 86,
  accuracy: 0.362,
  brier: 0.81,
  ece: 0.12,
  classes: [
    { class: "high", support: 40, predicted: 20, correct: 10, precision: 0.5, recall: 0.25 },
    { class: "low", support: 46, predicted: 66, correct: 21, precision: 21 / 66, recall: null },
  ],
  baseline: { class: "low", accuracy: 0.461, brier: 0.5 },
  beats_baseline: false,
};

describe("EvaluationResult", () => {
  it("puts the model beside the majority-class baseline and says which wins", () => {
    render(<EvaluationResult result={result} />);
    expect(screen.getByText("majority baseline (low)")).toBeTruthy();
    expect(screen.getByText("0.461")).toBeTruthy();
    expect(screen.getByText("0.362")).toBeTruthy();
    expect(screen.getByTestId("verdict").textContent).toBe(
      "does not beat the majority-class baseline",
    );
  });

  it("breaks the result down per class, with an unknown recall shown as unknown", () => {
    render(<EvaluationResult result={result} />);
    expect(screen.getByRole("img", { name: "recall of high: 25.0%" })).toBeTruthy();
    expect(screen.getAllByText("—")).toHaveLength(1);
  });
});
