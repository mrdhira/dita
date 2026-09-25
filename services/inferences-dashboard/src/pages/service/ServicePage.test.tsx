import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
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

  it("a service that is not deployed has no tabs and no actions", async () => {
    open("/services/ocr");
    expect(await screen.findByText("not deployed", { exact: false })).toBeTruthy();
    expect(screen.getByText(/no HTTP surface: DIP only/)).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "service tabs" })).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getAllByRole("link").map((l) => l.textContent)).toEqual(["Back to Fleet"]);
  });
});
