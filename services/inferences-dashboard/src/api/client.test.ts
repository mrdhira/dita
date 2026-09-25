import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./client";

async function failure(status: number, body: string, statusText = "Internal Server Error") {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(body, { status, statusText }))),
  );
  const error: unknown = await api.templates().catch((e: unknown) => e);
  if (!(error instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(error)}`);
  return error;
}

// A recovered panic answers problem+json while every other route answers {error, error_type};
// the reader gets a sentence from either, and the shape that lands later breaks neither.
describe("a failure body, in either shape the orchestrator writes", () => {
  it.each([
    [
      "{error, error_type}",
      '{"error":"the store could not complete the request","error_type":"Internal"}',
      "the store could not complete the request",
    ],
    [
      "problem+json with a detail",
      '{"type":"about:blank","title":"Internal Server Error","status":500,"detail":"a panic was recovered"}',
      "a panic was recovered",
    ],
    [
      "problem+json with only a title",
      '{"type":"about:blank","title":"Internal Server Error","status":500}',
      "Internal Server Error",
    ],
    ["not JSON at all", "upstream connect error", "upstream connect error"],
    ["an empty body", "", "Internal Server Error"],
    ["an error that is not a string", '{"error":{"nested":true}}', '{"error":{"nested":true}}'],
  ])("%s", async (_, body, message) => {
    const error = await failure(500, body);
    expect(error.status).toBe(500);
    expect(error.problem.error).toBe(message);
  });

  it("keeps the fields of the usual shape", async () => {
    const error = await failure(503, '{"error":"x","reason":"busy","worker":"w"}');
    expect(error.problem).toEqual({ error: "x", reason: "busy", worker: "w" });
  });
});

// The orchestrator refuses any mutating request that does not declare a JSON content type, and
// that refusal is what keeps a cross-site form POST out of the store. Retire is the one call
// with no body, so it used to send no content type and would have been refused in production.
describe("what every mutating request carries", () => {
  it("a bodyless POST still declares a JSON content type, and sends no body", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ name: "alert-triage", version: 1, retired: true, retired_at: "2026-09-25T00:00:00Z" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.retire("alert-triage", 1);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/schemas/alert-triage/versions/1/retire");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(init.body).toBeUndefined();
  });

  it("a GET declares no content type", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{"templates":[]}', { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);

    await api.templates();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });
});
