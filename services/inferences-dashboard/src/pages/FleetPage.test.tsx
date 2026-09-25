import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkerReport } from "../api/client";
import { renderAt, stubApi } from "../test/render";
import { FleetPage } from "./FleetPage";

const report = (name: string, state: string, extra: Partial<WorkerReport> = {}): WorkerReport => ({
  name,
  url: "http://x:8080",
  state,
  health_status: state === "ready" ? 200 : null,
  info: null,
  ...extra,
});

function fleet(...workers: WorkerReport[]) {
  return stubApi({ "GET /workers": () => ({ status: 200, body: { workers } }) });
}

/** The rows exist before the answer does, so wait for the answer first. */
async function row(name: string) {
  const time = await screen.findByText(/^\d\d:\d\d:\d\d$/, { selector: "time" });
  expect(time.parentElement?.textContent).toMatch(/^as of \d\d:\d\d:\d\d/);
  return screen.getByRole("row", { name });
}

describe("FleetPage", () => {
  const cases = [
    { state: "ready", word: "ready", shape: "●", says: "serving qwen3-embedding-0.6b" },
    { state: "busy", word: "busy", shape: "◐", says: "requests are refused, not queued" },
    {
      state: "no_model",
      word: "no model",
      shape: "○",
      says: "requests fail until a model is loaded",
    },
    { state: "not_running", word: "stopped", shape: "■", says: "a host action" },
    { state: "unhealthy", word: "unhealthy", shape: "▲", says: "its own /health answered 500" },
    { state: "unreachable", word: "unreachable", shape: "◆", says: "could not be reached" },
    { state: "timeout", word: "timeout", shape: "◆", says: "did not answer within 5s" },
    { state: "client_gone", word: "degraded", shape: "◇", says: "the caller went away" },
  ];
  for (const c of cases) {
    it(`shows ${c.state} as the word "${c.word}", a shape and its sentence`, async () => {
      const errors: Record<string, string> = {
        unreachable: "inferences-embedding could not be reached",
        timeout: "inferences-embedding did not answer within 5s",
        client_gone: "the caller went away",
      };
      fleet(
        report("inferences-embedding", c.state, {
          ...(c.state === "unhealthy" && { health_status: 500 }),
          ...(c.state === "ready" && { info: { model_id: "qwen3-embedding-0.6b" } }),
          ...(errors[c.state] !== undefined && { error: errors[c.state] }),
        }),
      );
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-embedding");
      const badge = within(r).getByText(c.word, { exact: false, selector: "span[data-tone]" });
      expect(badge.textContent).toBe(`${c.shape}${c.word}`);
      expect(within(r).getAllByText(c.says, { exact: false }).length).toBeGreaterThan(0);
    });
  }

  it("keeps an unknown state's own word rather than colouring it", async () => {
    fleet(report("inferences-embedding", "draining"));
    renderAt("/", "/", <FleetPage />);
    const r = await row("inferences-embedding");
    expect(within(r).getByText("draining", { exact: false }).textContent).toBe("?draining");
  });

  it("shows the resident model and its short sha", async () => {
    fleet(
      report("inferences-reranker", "ready", {
        info: { model_id: "jina-reranker-v1-turbo-en", model_sha: "b8c14f4e723d9e0aab" },
      }),
    );
    renderAt("/", "/", <FleetPage />);
    const r = await row("inferences-reranker");
    expect(within(r).getByText("@b8c14f4")).toBeTruthy();
    expect(within(r).getByRole("link", { name: "Open reranker" }).getAttribute("href")).toBe(
      "/services/reranker",
    );
  });

  it.each([
    ["inferences-ocr", "no HTTP surface: DIP only"],
    ["inferences-stt", "no code"],
    ["inferences-tts", "no code"],
  ])("lists %s as not deployed, with its reason and no action", async (name, reason) => {
    fleet(report("inferences-embedding", "ready"));
    renderAt("/", "/", <FleetPage />);
    const deployed = await row("inferences-embedding");
    expect(within(deployed).getAllByRole("link")).toHaveLength(1);
    const r = screen.getByRole("row", { name });
    expect(within(r).getByText("not deployed", { exact: false }).textContent).toBe("–not deployed");
    expect(within(r).getByText(reason)).toBeTruthy();
    expect(within(r).queryAllByRole("link")).toHaveLength(0);
    expect(within(r).queryAllByRole("button")).toHaveLength(0);
  });

  describe("polling", () => {
    let visibility: DocumentVisibilityState = "visible";
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibility,
      });
    });
    afterEach(() => {
      vi.useRealTimers();
      visibility = "visible";
    });
    const turn = async (to: DocumentVisibilityState) => {
      visibility = to;
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await Promise.resolve();
      });
    };
    const polls = (calls: { path: string }[]) => calls.filter((c) => c.path === "/workers").length;

    it("polls every 5 s while visible, stops while hidden, and says it is paused", async () => {
      const calls = fleet(report("inferences-embedding", "ready"));
      renderAt("/", "/", <FleetPage />);
      await row("inferences-embedding");
      expect(polls(calls)).toBe(1);

      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(polls(calls)).toBe(2);

      await turn("hidden");
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(polls(calls)).toBe(2);
      expect(screen.getByText(/paused while this tab is hidden/)).toBeTruthy();

      await turn("visible");
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(polls(calls)).toBeGreaterThan(2);
      expect(screen.queryByText(/paused while this tab is hidden/)).toBeNull();
    });
  });
});
