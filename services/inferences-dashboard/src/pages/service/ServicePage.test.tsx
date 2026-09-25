import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import rerankerMetrics from "../../test/reranker.metrics.txt?raw";
import { renderAt, stubApi } from "../../test/render";
import { ServicePage } from "./ServicePage";

const reranker = {
  name: "inferences-reranker",
  url: "http://inferences-reranker:8080",
  state: "ready",
  health_status: 200,
  info: {
    model_id: "jina-reranker-v1-turbo-en",
    model_sha: "b8c14f4e723d9e0aab4732a7b7b93741eeeb77c2",
    model_dtype: "float16",
    max_input_length: 1024,
    max_concurrent_requests: 4,
    max_batch_tokens: 1024,
    auto_truncate: true,
  },
};

const embedding = {
  name: "inferences-embedding",
  url: "http://inferences-embedding:8080",
  state: "ready",
  health_status: 200,
  info: { model_id: "qwen3-embedding-0.6b", dimensions: 1024, prompt_names: ["document", "query"] },
};

function open(path: string, extra: Parameters<typeof stubApi>[0] = {}) {
  const calls = stubApi({
    "GET /workers": () => ({ status: 200, body: { workers: [reranker, embedding] } }),
    "GET /metrics/reranker": () => ({ status: 200, body: rerankerMetrics }),
    "GET /metrics/embedding": () => ({ status: 200, body: rerankerMetrics }),
    ...extra,
  });
  renderAt(path, "/services/:id/:tab?", <ServicePage />);
  return calls;
}

describe("ServicePage", () => {
  it("overview: the state, the model's identity and the blast radius", async () => {
    open("/services/reranker");
    expect(await screen.findByText("serving jina-reranker-v1-turbo-en")).toBeTruthy();
    const identity = screen.getByRole("region", { name: "resident model" });
    expect(within(identity).getByText("b8c14f4e723d9e0aab4732a7b7b93741eeeb77c2")).toBeTruthy();
    expect(within(identity).getByText("max_batch_tokens")).toBeTruthy();
    expect(screen.getByText(/Hindsight recall and consolidation use this/)).toBeTruthy();
    expect(await screen.findByText("318 MiB")).toBeTruthy();
    const inferences = screen.getByText("inferences, since the last restart").nextSibling;
    expect(inferences?.textContent).toBe("24");
  });

  it("metrics: shows the buckets, sum and count, and prints no percentile", async () => {
    expect(rerankerMetrics).toContain("dita_worker_infer_duration_seconds_bucket");
    open("/services/reranker/metrics");
    const infer = await screen.findByRole("region", { name: "inference duration" });
    const buckets = within(infer)
      .getAllByRole("row")
      .map((r) => r.textContent);
    expect(buckets).toEqual([
      "bucket (≤)cumulative count",
      "0.05 s2",
      "0.1 s3",
      "0.25 s4",
      "0.5 s4",
      "1 s11",
      "2.5 s16",
      "5 s21",
      "10 s24",
      "30 s24",
      "60 s24",
      "+Inf24",
    ]);
    expect(within(infer).getByText(/_sum 49\.3837 s · _count 24 · average 2\.058 s/)).toBeTruthy();
    const page = document.body.textContent;
    expect(page).not.toMatch(/percentile|quantile|median|\bp(50|90|95|99)\b/i);
    expect(screen.getAllByText("· since the last restart").length).toBeGreaterThan(0);
    expect(page).not.toContain("dita_worker_fetched_bytes_total");
  });

  it("models: the resident model, and an honest empty state for the registry", async () => {
    const calls = open("/services/reranker/models");
    const resident = await screen.findByRole("region", { name: "resident" });
    expect(await within(resident).findByText("jina-reranker-v1-turbo-en")).toBeTruthy();
    const registry = screen.getByRole("region", { name: "registry" });
    expect(within(registry).getByText("Registered models: not available yet")).toBeTruthy();
    expect(within(registry).queryAllByRole("listitem")).toHaveLength(0);
    expect(calls.map((c) => c.path).filter((p) => p.includes("models"))).toEqual([]);
  });

  it("logs: links out to Dozzle in a new tab and embeds nothing", async () => {
    open("/services/reranker/logs");
    const link = await screen.findByRole("link", { name: "open Dozzle in a new tab" });
    expect(link.getAttribute("href")).toBe("https://dozzle.home.arpa");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.getByText("docker logs --tail 200 --follow inferences-reranker")).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("try it: shows a refusal verbatim with its error_type", async () => {
    const refusal = '{"error":"`inputs` cannot be empty","error_type":"Empty"}';
    const calls = open("/services/embedding/try", {
      "POST /embed": () => ({ status: 400, body: refusal }),
    });
    await userEvent.click(
      await screen.findByRole("button", { name: "POST /api/inferences/embed" }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("refused · error_type Empty`inputs` cannot be empty");
    const response = screen.getByRole("region", { name: "response" });
    expect(within(response).getByText("HTTP 400")).toBeTruthy();
    expect(within(response).getByText(refusal)).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      inputs: [],
      normalize: true,
      truncate: false,
    });
  });

  it("try it: sends the fields the worker accepts and shows x-compute-time", async () => {
    const calls = open("/services/embedding/try", {
      "POST /embed": () => ({
        status: 200,
        body: "[[0.1,-0.2]]",
        headers: { "x-compute-time": "38", "x-model-id": "qwen3-embedding-0.6b" },
      }),
    });
    await userEvent.type(await screen.findByLabelText("inputs, one per line"), "a{enter}b");
    await userEvent.selectOptions(screen.getByLabelText("prompt_name"), "query");
    await userEvent.click(screen.getByRole("button", { name: "POST /api/inferences/embed" }));
    expect(
      await screen.findByText("HTTP 200 · x-compute-time 38 · x-model-id qwen3-embedding-0.6b"),
    ).toBeTruthy();
    expect(screen.getByText("[[0.1,-0.2]]")).toBeTruthy();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      inputs: ["a", "b"],
      normalize: true,
      truncate: false,
      prompt_name: "query",
    });
  });

  it("try it on /rerank: leaves truncate to the model unless it is chosen", async () => {
    const calls = open("/services/reranker/try", {
      "POST /rerank": () => ({ status: 200, body: '[{"index":0,"score":0.9}]' }),
    });
    await userEvent.type(await screen.findByLabelText("query"), "q");
    await userEvent.type(screen.getByLabelText("texts, one per line"), "x{enter}{enter}y");
    await userEvent.click(screen.getByLabelText("return_text"));
    await userEvent.click(screen.getByRole("button", { name: "POST /api/inferences/rerank" }));
    await screen.findByText('[{"index":0,"score":0.9}]');
    await userEvent.selectOptions(screen.getByLabelText("truncate"), "false");
    await userEvent.click(screen.getByRole("button", { name: "POST /api/inferences/rerank" }));
    await screen.findByText('[{"index":0,"score":0.9}]');
    const posts = calls.filter((c) => c.method === "POST").map((c) => c.body);
    expect(posts).toEqual([
      { query: "q", texts: ["x", "y"], raw_scores: false, return_text: true },
      { query: "q", texts: ["x", "y"], raw_scores: false, return_text: true, truncate: false },
    ]);
  });

  it("try it on /decide: malformed questions reach the worker as typed", async () => {
    const calls = open("/services/system-one/try", {
      "GET /workers": () => ({ status: 200, body: { workers: [] } }),
      "POST /decide": () => ({
        status: 400,
        body: '{"error":"the body is not JSON","error_type":"Validation"}',
      }),
    });
    const questions = await screen.findByLabelText("questions (JSON)");
    await userEvent.clear(questions);
    await userEvent.type(questions, "not json");
    await userEvent.click(screen.getByRole("button", { name: "POST /api/inferences/decide" }));
    expect((await screen.findByRole("alert")).textContent).toContain("the body is not JSON");
    expect(calls.find((c) => c.method === "POST")?.body).toBe('{"text":"","questions":not json}');
  });

  it("models: identity inline, everything else only behind the expander", async () => {
    open("/services/reranker/models");
    const resident = await screen.findByRole("region", { name: "resident" });
    await within(resident).findByText("jina-reranker-v1-turbo-en");
    const inline = resident.querySelector("dl")?.textContent ?? "";
    expect(inline).toContain("max_batch_tokens");
    expect(inline).not.toContain("auto_truncate");
    expect(resident.querySelector("details pre")?.textContent).toContain('"auto_truncate": true');
  });

  describe("the resident model is claimed only when the worker reports it", () => {
    const withState = (state: string, info: unknown = null) => ({
      "GET /workers": () => ({
        status: 200,
        body: {
          workers: [{ ...reranker, state, info, health_status: state === "no_model" ? 503 : 200 }],
        },
      }),
    });
    const unknown = "The resident model is unknown: the gateway has no /info from this worker";
    const cases = [
      { name: "ready, but /info failed", routes: withState("ready"), want: unknown },
      { name: "busy", routes: withState("busy"), want: unknown },
      { name: "unreachable", routes: withState("unreachable"), want: unknown },
      {
        name: "/workers failed",
        routes: { "GET /workers": () => ({ status: 400, body: { error: "no" } }) },
        want: "The resident model is unknown: the gateway has not reported this worker",
      },
      {
        name: "no_model",
        routes: withState("no_model"),
        want: "No model is resident: the worker says so itself",
      },
      {
        name: "no_model, even if /info answered",
        routes: withState("no_model", { model_id: "configured-not-resident" }),
        want: "No model is resident: the worker says so itself",
      },
    ];
    for (const c of cases) {
      for (const tab of ["models", ""]) {
        it(`${tab || "overview"}: ${c.name}`, async () => {
          open(`/services/reranker${tab && `/${tab}`}`, c.routes);
          const region = await screen.findByRole("region", {
            name: tab ? "resident" : "resident model",
          });
          await vi.waitFor(() => {
            expect(region.textContent).toContain(c.want);
          });
          if (c.want.startsWith("The resident model is unknown")) {
            expect(document.body.textContent).not.toMatch(
              /No model is resident|requests fail until one is loaded/,
            );
          }
          expect(region.textContent).not.toContain("configured-not-resident");
        });
      }
    }

    it("while /workers is still pending", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => new Promise<Response>(() => undefined)),
      );
      renderAt("/services/reranker/models", "/services/:id/:tab?", <ServicePage />);
      const region = await screen.findByRole("region", { name: "resident" });
      expect(region.textContent).toContain(
        "The resident model is unknown: the gateway has not reported this worker",
      );
      expect(document.body.textContent).not.toContain("No model is resident");
    });
  });

  it("overview: resident for is a dash, never 0.0 s, when the gauge says nothing is resident", async () => {
    const page = rerankerMetrics.replace(/^(dita_worker_model_resident\{[^}]*\}) 1$/m, "$1 0");
    expect(page).not.toBe(rerankerMetrics);
    open("/services/reranker", { "GET /metrics/reranker": () => ({ status: 200, body: page }) });
    expect(await screen.findByText("318 MiB")).toBeTruthy();
    expect(screen.getByText("resident for").nextSibling?.textContent).toBe("—");
  });

  it("overview: the /metrics figures carry their own as of, and say a load in flight", async () => {
    const loading = rerankerMetrics.replace(/^(dita_worker_model_loading\{[^}]*\}) 0$/m, "$1 1");
    expect(loading).not.toBe(rerankerMetrics);
    open("/services/reranker", { "GET /metrics/reranker": () => ({ status: 200, body: loading }) });
    const figures = await screen.findByRole("region", { name: "since the last restart" });
    expect(await within(figures).findByText("loading a model right now")).toBeTruthy();
    expect(within(figures).getByText(/^\d\d:\d\d:\d\d$/, { selector: "time" })).toBeTruthy();
  });

  describe("metrics: an answer that is not a /metrics page is an error, not an idle worker", () => {
    const cases = [
      {
        name: "an HTML page served as 200",
        answer: {
          status: 200,
          body: "<!doctype html><title>dashboard</title>",
          headers: { "content-type": "text/html" },
        },
        says: "it came as text/html",
      },
      {
        name: "plain text with none of the series",
        answer: { status: 200, body: "some_other_exporter_total 3\n" },
        says: "none of the series this console reads",
      },
      {
        name: "the worker's own 404",
        answer: {
          status: 404,
          body: "404 page not found",
          headers: { "content-type": "text/plain" },
        },
        says: "404 page not found",
      },
    ];
    for (const c of cases) {
      it(c.name, async () => {
        open("/services/reranker/metrics", { "GET /metrics/reranker": () => c.answer });
        expect((await screen.findByRole("alert")).textContent).toContain(c.says);
        expect(document.body.textContent).not.toContain("none since the last restart");
        expect(screen.queryByRole("table", { name: "series" })).toBeNull();
      });
    }
  });

  it("says when the gateway does not report this service", async () => {
    open("/services/reranker", { "GET /workers": () => ({ status: 200, body: { workers: [] } }) });
    expect(await screen.findByText("The gateway does not report this service.")).toBeTruthy();
  });

  it("says there is no such service", async () => {
    open("/services/nope");
    expect(await screen.findByText("There is no service called nope")).toBeTruthy();
  });

  it("try it: shows how long it has been waiting for the worker", async () => {
    let answer: (r: Response) => void = () => undefined;
    const reply = new Promise<Response>((resolve) => {
      answer = resolve;
    });
    open("/services/embedding/try");
    const base = globalThis.fetch;
    vi.stubGlobal("fetch", (input: string, init?: RequestInit) =>
      init?.method === "POST" ? reply : base(input, init),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "POST /api/inferences/embed" }),
    );
    const waiting = await screen.findByRole("button", {
      name: /^Waiting for the worker… \d+\.\d s$/,
    });
    await vi.waitFor(
      () => {
        expect(waiting.textContent).not.toBe("Waiting for the worker… 0.0 s");
      },
      { timeout: 2_000 },
    );
    answer(new Response("[[0.1]]", { status: 200 }));
    expect(await screen.findByText("[[0.1]]")).toBeTruthy();
    expect(screen.queryByText(/Waiting for the worker/)).toBeNull();
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
    const count = (calls: { path: string }[], path: string) =>
      calls.filter((c) => c.path === path).length;
    const hide = async () => {
      visibility = "hidden";
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await Promise.resolve();
      });
    };

    it("reads /workers every 10 s and /metrics every 30 s, and neither while hidden", async () => {
      const calls = open("/services/reranker/metrics");
      await screen.findByRole("region", { name: "inference duration" });
      expect(count(calls, "/workers")).toBe(1);
      expect(count(calls, "/metrics/reranker")).toBe(1);

      await act(() => vi.advanceTimersByTimeAsync(9_900));
      expect(count(calls, "/workers")).toBe(1);
      await act(() => vi.advanceTimersByTimeAsync(200));
      expect(count(calls, "/workers")).toBe(2);

      await act(() => vi.advanceTimersByTimeAsync(19_700));
      expect(count(calls, "/metrics/reranker")).toBe(1);
      await act(() => vi.advanceTimersByTimeAsync(200));
      expect(count(calls, "/metrics/reranker")).toBe(2);
      expect(count(calls, "/workers")).toBe(4);

      await hide();
      await act(() => vi.advanceTimersByTimeAsync(120_000));
      expect(count(calls, "/workers")).toBe(4);
      expect(count(calls, "/metrics/reranker")).toBe(2);
      expect(screen.getAllByText(/paused while this tab is hidden/)).toHaveLength(2);
    });

    it("marks the header stale when a /workers refresh hangs", async () => {
      let n = 0;
      vi.stubGlobal("fetch", (input: string) => {
        if (input.endsWith("/workers") && ++n === 1) {
          return Promise.resolve(
            new Response(JSON.stringify({ workers: [reranker] }), { status: 200 }),
          );
        }
        return new Promise<Response>(() => undefined);
      });
      renderAt("/services/reranker/models", "/services/:id/:tab?", <ServicePage />);
      const badge = await screen.findByText("ready", { exact: false, selector: "span[data-tone]" });
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(badge.dataset.stale).toBeUndefined();
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(badge.dataset.stale).toBe("true");
      expect(screen.getByText(/^stale: not refreshed since \d\d:\d\d:\d\d$/)).toBeTruthy();
      expect(
        screen.getByText(/the latest refresh has not answered; this is the last good answer/),
      ).toBeTruthy();
    });

    it("overview: the /metrics as of admits a failed refresh", async () => {
      let failing = false;
      open("/services/reranker", {
        "GET /metrics/reranker": () =>
          failing
            ? { status: 502, body: { error: "gone" } }
            : { status: 200, body: rerankerMetrics },
      });
      const figures = await screen.findByRole("region", { name: "since the last restart" });
      await within(figures).findByText("318 MiB");
      expect(within(figures).getByRole("status").textContent).toBe("");
      failing = true;
      await act(() => vi.advanceTimersByTimeAsync(35_000));
      expect(within(figures).getByRole("status").textContent).toBe(
        " · the latest refresh failed; this is the last good answer",
      );
    });

    it("marks the header stale when the latest /workers refresh failed", async () => {
      let failing = false;
      open("/services/reranker/metrics", {
        "GET /workers": () =>
          failing
            ? { status: 502, body: { error: "down" } }
            : { status: 200, body: { workers: [reranker] } },
      });
      const badge = await screen.findByText("ready", { exact: false, selector: "span[data-tone]" });
      expect(badge.dataset.stale).toBeUndefined();
      failing = true;
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      expect(badge.dataset.stale).toBe("true");
      expect(badge.textContent).toBe("●ready · stale");
      expect(screen.getByText(/^stale: not refreshed since \d\d:\d\d:\d\d$/)).toBeTruthy();
      expect(
        screen.getByText(/the latest refresh failed; this is the last good answer/),
      ).toBeTruthy();
    });
  });

  it("a service that is not deployed has no tabs and no actions", async () => {
    open("/services/ocr");
    expect(await screen.findByText("not deployed", { exact: false })).toBeTruthy();
    expect(screen.getByText(/no HTTP surface: DIP only/)).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "service tabs" })).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getAllByRole("link").map((l) => l.textContent)).toEqual(["Back to Fleet"]);
  });
});
