import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerView } from "./AnswerView";

const answer = {
  question: "severity",
  type: "choice",
  options: [
    { option: "high", probability: 0.6 },
    { option: "medium", probability: 0.25 },
    { option: "low", probability: 0.15 },
  ],
  confidence: 0.6,
};

describe("AnswerView", () => {
  it("shows the top two alternatives and the confidence, never a bare value", () => {
    render(<AnswerView answer={answer} />);
    expect(screen.getByTestId("top-1").textContent).toBe("high · 60.0%");
    expect(screen.getByTestId("top-2").textContent).toBe("medium · 25.0%");
    expect(screen.getByTestId("confidence").textContent).toBe("60.0%");
  });

  it("marks it a suggestion", () => {
    render(<AnswerView answer={answer} />);
    expect(screen.getByRole("region", { name: "suggestion for severity" }).textContent).toContain(
      "suggestion",
    );
  });

  it("lists every option's probability", () => {
    render(<AnswerView answer={answer} />);
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual(["high60.0%", "medium25.0%", "low15.0%"]);
    expect(screen.getByRole("img", { name: "low: 15.0%" })).toBeTruthy();
  });

  it("says so when there is no alternative, rather than leaving a gap", () => {
    render(<AnswerView answer={{ ...answer, options: [{ option: "yes", probability: 1 }] }} />);
    expect(screen.getByTestId("top-2").textContent).toBe("none");
  });
});
