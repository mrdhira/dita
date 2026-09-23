import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { vi } from "vitest";
import { makeQueryClient } from "../App";

/** fetch, stubbed at the seam: each call is answered by the first matching route. */
export function stubApi(
  routes: Record<string, (body: unknown) => { status: number; body: unknown }>,
) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchStub = vi.fn((input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = input.replace("/api/inferences", "");
    const body: unknown = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method, path, body });
    const handler = routes[`${method} ${path}`];
    const answer = handler
      ? handler(body)
      : { status: 404, body: { error: `no stub for ${method} ${path}` } };
    return Promise.resolve(new Response(JSON.stringify(answer.body), { status: answer.status }));
  });
  vi.stubGlobal("fetch", fetchStub);
  return calls;
}

export function renderAt(path: string, pattern: string, element: ReactElement) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={pattern} element={element} />
          <Route path="*" element={<p>elsewhere</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
