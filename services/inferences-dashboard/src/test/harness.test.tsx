import { render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { describe, expect, it } from "vitest";

function Late({ after }: { after: number }) {
  const [shown, show] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      show(true);
    }, after);
    return () => {
      clearTimeout(timer);
    };
  }, [after]);
  return shown ? <p>ready</p> : null;
}

describe("the test harness", () => {
  it("waits past Testing Library's 1 s default, as a slow first render on a busy machine needs", async () => {
    const after = 1_500;
    expect(after).toBeGreaterThan(1_000);
    render(<Late after={after} />);
    expect(await screen.findByText("ready")).toBeTruthy();
  });
});
