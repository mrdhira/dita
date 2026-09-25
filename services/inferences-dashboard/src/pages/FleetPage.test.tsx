import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkerReport } from "../api/client";
import rerankerMetrics from "../test/reranker.metrics.txt?raw";
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

const cellText = (r: HTMLElement, i: number) => r.querySelectorAll("td")[i]?.textContent;

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

  describe("the resident model is claimed only when the worker reports it", () => {
    const cases = [
      {
        name: "ready, but the second probe (/info) failed",
        report: report("inferences-embedding", "ready"),
        want: "unknown",
      },
      {
        name: "busy: a model may well be resident",
        report: report("inferences-embedding", "busy"),
        want: "unknown",
      },
      { name: "timeout", report: report("inferences-embedding", "timeout"), want: "unknown" },
      {
        name: "no_model: the worker says so itself",
        report: report("inferences-embedding", "no_model", { health_status: 503 }),
        want: "none resident",
      },
      {
        name: "ready with /info",
        report: report("inferences-embedding", "ready", {
          info: { model_id: "qwen3-embedding-0.6b" },
        }),
        want: "qwen3-embedding-0.6b",
      },
    ];
    for (const c of cases) {
      it(c.name, async () => {
        fleet(c.report);
        renderAt("/", "/", <FleetPage />);
        const r = await row("inferences-embedding");
        expect(cellText(r, 2)).toBe(c.want);
      });
    }

    it("says unknown, never none resident, while /workers has not answered", () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => new Promise<Response>(() => undefined)),
      );
      renderAt("/", "/", <FleetPage />);
      const r = screen.getByRole("row", { name: "inferences-embedding" });
      expect(cellText(r, 1)).toBe("?not reported");
      expect(cellText(r, 2)).toBe("unknown");
      expect(document.body.textContent).not.toContain("none resident");
    });

    it("says unknown, never none resident, when /workers failed", async () => {
      stubApi({ "GET /workers": () => ({ status: 400, body: { error: "gateway says no" } }) });
      renderAt("/", "/", <FleetPage />);
      expect((await screen.findByRole("alert")).textContent).toContain("gateway says no");
      const r = screen.getByRole("row", { name: "inferences-embedding" });
      expect(cellText(r, 1)).toBe("?not reported");
      expect(cellText(r, 2)).toBe("unknown");
    });
  });

  describe("resident for", () => {
    const notResident = rerankerMetrics.replace(
      /^(dita_worker_model_resident\{[^}]*\}) 1$/m,
      "$1 0",
    );
    it.each([
      ["a resident model", "ready", rerankerMetrics, "1 h 12 min"],
      ["a worker whose gauge says nothing is resident", "no_model", notResident, "—"],
    ])("%s", async (_, state, page, want) => {
      expect(notResident).not.toBe(rerankerMetrics);
      stubApi({
        "GET /workers": () => ({
          status: 200,
          body: { workers: [report("inferences-reranker", state)] },
        }),
        "GET /metrics/reranker": () => ({ status: 200, body: page }),
      });
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-reranker");
      await vi.waitFor(() => {
        expect(cellText(r, 3)).toBe(want);
      });
      if (want === "—") {
        expect(document.body.textContent).not.toContain("0.0 s");
      }
    });
  });

  it("lists a worker it does not know after the intended fleet, as reported by the gateway", async () => {
    fleet(report("inferences-embedding", "ready"), report("inferences-extra", "ready"));
    renderAt("/", "/", <FleetPage />);
    const r = await row("inferences-extra");
    expect(within(r).getByText("reported by the gateway")).toBeTruthy();
    expect(within(r).queryAllByRole("link")).toHaveLength(0);
  });

  it("explains the not-deployed rows only while the gateway does not report them", async () => {
    const note =
      /OCR speaks only DIP, which the gateway does not probe, and STT and TTS have no code yet/;
    fleet(report("inferences-embedding", "ready"));
    const first = renderAt("/", "/", <FleetPage />);
    await row("inferences-embedding");
    expect(screen.getByText(note)).toBeTruthy();
    first.unmount();

    fleet(report("inferences-embedding", "ready"), report("inferences-ocr", "ready"));
    renderAt("/", "/", <FleetPage />);
    await row("inferences-ocr");
    expect(screen.queryByText(note)).toBeNull();
  });

  it("announces the paused and failed notes, never the clock", async () => {
    fleet(report("inferences-embedding", "ready"));
    renderAt("/", "/", <FleetPage />);
    const time = await screen.findByText(/^\d\d:\d\d:\d\d$/, { selector: "time" });
    expect(time.closest("[aria-live], [role=status]")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("");
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

    it("marks every reported row stale when the latest refresh failed, and keeps the last values", async () => {
      let failing = false;
      stubApi({
        "GET /workers": () =>
          failing
            ? { status: 502, body: { error: "the gateway is down" } }
            : {
                status: 200,
                body: {
                  workers: [
                    report("inferences-embedding", "ready", {
                      info: { model_id: "qwen3-embedding-0.6b" },
                    }),
                    report("inferences-extra", "ready"),
                  ],
                },
              },
      });
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-embedding");
      const extra = screen.getByRole("row", { name: "inferences-extra" });
      const badge = () =>
        within(r).getByText("ready", { exact: false, selector: "span[data-tone]" });
      expect(badge().dataset.stale).toBeUndefined();
      expect(badge().textContent).toBe("●ready");
      expect(within(r).queryByText(/^stale: not refreshed since/)).toBeNull();
      expect(screen.getByRole("status").textContent).toBe("");

      failing = true;
      await act(() => vi.advanceTimersByTimeAsync(12_000));
      expect(screen.getByRole("status").textContent).toBe(
        " · the latest refresh failed; this is the last good answer",
      );
      expect(badge().dataset.stale).toBe("true");
      expect(badge().textContent).toBe("●ready · stale");
      expect(badge().className).not.toMatch(/emerald/);
      expect(within(r).getByText(/^stale: not refreshed since \d\d:\d\d:\d\d$/)).toBeTruthy();
      expect(within(r).getByText("qwen3-embedding-0.6b")).toBeTruthy();
      const extraBadge = within(extra).getByText("ready", {
        exact: false,
        selector: "span[data-tone]",
      });
      expect(extraBadge.dataset.stale).toBe("true");
      expect(within(extra).getByText(/^stale: not refreshed since/)).toBeTruthy();
    });

    const answered = () =>
      new Response(
        JSON.stringify({
          workers: [
            report("inferences-embedding", "ready", { info: { model_id: "qwen3-embedding-0.6b" } }),
          ],
        }),
        { status: 200 },
      );

    it("marks the rows stale when a refresh hangs, even one that ignores its deadline", async () => {
      let n = 0;
      vi.stubGlobal("fetch", (input: string) => {
        if (input.endsWith("/workers") && ++n === 1) return Promise.resolve(answered());
        return new Promise<Response>(() => undefined);
      });
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-embedding");
      const badge = () =>
        within(r).getByText("ready", { exact: false, selector: "span[data-tone]" });

      await act(() => vi.advanceTimersByTimeAsync(19_000));
      expect(n).toBe(2);
      expect(badge().dataset.stale).toBeUndefined();
      expect(screen.getByRole("status").textContent).toBe("");

      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(badge().dataset.stale).toBe("true");
      expect(badge().className).not.toMatch(/emerald/);
      expect(within(r).getByText(/^stale: not refreshed since/)).toBeTruthy();
      expect(screen.getByRole("status").textContent).toBe(
        " · the latest refresh has not answered; this is the last good answer",
      );
    });

    it("ends a hung refresh at its deadline, reports it failed, and polls again", async () => {
      const signals: AbortSignal[] = [];
      vi.stubGlobal("fetch", (input: string, init?: RequestInit) => {
        if (!input.endsWith("/workers")) return new Promise<Response>(() => undefined);
        if (signals.push(init?.signal as AbortSignal) === 1) return Promise.resolve(answered());
        return new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(init.signal?.reason as Error);
          });
        });
      });
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-embedding");

      await act(() => vi.advanceTimersByTimeAsync(14_500));
      expect(signals).toHaveLength(2);
      expect(signals[1]?.aborted).toBe(false);

      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(signals[1]?.aborted).toBe(true);
      expect(screen.getByRole("status").textContent).toBe(
        " · the latest refresh failed; this is the last good answer",
      );
      expect(
        within(r).getByText("ready", { exact: false, selector: "span[data-tone]" }).dataset.stale,
      ).toBe("true");

      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(signals.length).toBeGreaterThan(2);
    });

    for (const [what, status, body] of [
      ["Caddy's empty 502, the orchestrator down", 502, ""],
      ["a proxy's reasonless 503", 503, "Service Unavailable"],
    ] as const) {
      it(`blames the gateway, never a worker or its model, for ${what}`, async () => {
        stubApi({ "GET /workers": () => ({ status, body }) });
        renderAt("/", "/", <FleetPage />);
        await act(() => vi.advanceTimersByTimeAsync(5_000));
        const banner = screen.getByRole("alert").textContent;
        expect(banner).toContain(`The gateway, or something in front of it, answered ${status}`);
        expect(banner).toContain("/workers");
        expect(banner).not.toMatch(/no model|The worker answered/);
      });
    }

    it("drops resident-for to — when the worker's /metrics stops answering", async () => {
      let failing = false;
      stubApi({
        "GET /workers": () => ({
          status: 200,
          body: { workers: [report("inferences-reranker", "ready")] },
        }),
        "GET /metrics/reranker": () =>
          failing
            ? { status: 502, body: { error: "gone" } }
            : { status: 200, body: rerankerMetrics },
      });
      renderAt("/", "/", <FleetPage />);
      const r = await row("inferences-reranker");
      await vi.waitFor(() => {
        expect(cellText(r, 3)).toBe("1 h 12 min");
      });

      failing = true;
      await act(() => vi.advanceTimersByTimeAsync(35_000));
      expect(cellText(r, 3)).toBe("—");
    });

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
