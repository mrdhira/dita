import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { vi } from "vitest";
import { makeQueryClient } from "../App";

/** fetch, stubbed at the seam: each call is answered by the first matching route. */
export function stubApi(
  routes: Record<
    string,
    (body: unknown) => { status: number; body: unknown; headers?: Record<string, string> }
  >,
) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchStub = vi.fn((input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = input.replace("/api/inferences", "");
    const raw = init?.body as string | undefined;
    let body: unknown = raw;
    try {
      body = raw ? JSON.parse(raw) : undefined;
    } catch {
      // Try-it sends what the person typed; a body that is not JSON is recorded as sent.
    }
    calls.push({ method, path, body });
    const handler = routes[`${method} ${path}`];
    const answer = handler
      ? handler(body)
      : { status: 404, body: { error: `no stub for ${method} ${path}` } };
    const text = typeof answer.body === "string" ? answer.body : JSON.stringify(answer.body);
    return Promise.resolve(
      new Response(text, { status: answer.status, headers: answer.headers ?? {} }),
    );
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
