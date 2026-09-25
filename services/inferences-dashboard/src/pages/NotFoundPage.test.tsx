import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { makeQueryClient, routes } from "../App";
import { stubApi } from "../test/render";

// The app's own route table, so a path it does not know reaches the catch-all, not the
// router's error boundary.
function app(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("a path the dashboard does not have", () => {
  it.each(["/history", "/decide/extra", "/nope"])("%s says so, inside the layout", (path) => {
    stubApi({ "GET /workers": () => ({ status: 200, body: { workers: [] } }) });
    app(path);
    expect(screen.getByRole("heading", { name: `There is no page at ${path}` })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Inferences dashboard" })).toBeTruthy();
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
  });

  it("offers the way back to Decide", async () => {
    stubApi({
      "GET /workers": () => ({ status: 200, body: { workers: [] } }),
      "GET /schemas": () => ({ status: 200, body: { templates: [] } }),
    });
    const router = app("/history");
    await userEvent.click(screen.getByRole("link", { name: "Back to Decide" }));
    expect(router.state.location.pathname).toBe("/decide");
    expect(await screen.findByRole("form", { name: "run a decision" })).toBeTruthy();
  });
});
