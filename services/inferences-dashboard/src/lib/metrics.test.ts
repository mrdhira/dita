import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { parseExposition, readWorkerMetrics, residentFor } from "./metrics";

describe("parseExposition", () => {
  const cases = [
    { name: "a bare sample", line: "up 1", want: [{ name: "up", labels: {}, value: 1 }] },
    {
      name: "labels, with an escaped quote",
      line: 'x{a="1",b="say \\"hi\\""} 2.5',
      want: [{ name: "x", labels: { a: "1", b: 'say "hi"' }, value: 2.5 }],
    },
    {
      name: "+Inf and a timestamp",
      line: 'h_bucket{le="+Inf"} 3 1700000000',
      want: [{ name: "h_bucket", labels: { le: "+Inf" }, value: 3 }],
    },
    { name: "a comment", line: "# TYPE x counter", want: [] },
    { name: "garbage", line: "not a sample at all", want: [] },
  ];
  for (const c of cases) {
    it(c.name, () => {
      expect(parseExposition(c.line)).toEqual(c.want);
    });
  }
});

describe("readWorkerMetrics", () => {
  it("reads only the named series, and groups histogram buckets by their labels", () => {
    const m = readWorkerMetrics(
      [
        'dita_worker_ops_total{op="infer"} 4',
        "dita_worker_fetched_bytes_total 99",
        'dita_worker_infer_duration_seconds_bucket{model="a",le="+Inf"} 2',
        'dita_worker_infer_duration_seconds_bucket{model="a",le="0.5"} 1',
        'dita_worker_infer_duration_seconds_sum{model="a"} 0.7',
        'dita_worker_infer_duration_seconds_count{model="a"} 2',
      ].join("\n"),
    );
    const names = m.series.flatMap((s) => s.samples.map((x) => x.name));
    expect(names).toEqual(["dita_worker_ops_total"]);
    const infer = m.histograms.find((h) => h.name === "dita_worker_infer_duration_seconds");
    expect(infer?.series).toEqual([
      {
        name: "dita_worker_infer_duration_seconds",
        labels: { model: "a" },
        buckets: [
          { le: 0.5, count: 1 },
          { le: Infinity, count: 2 },
        ],
        sum: 0.7,
        count: 2,
      },
    ]);
  });
});

describe("readWorkerMetrics refuses a page it cannot read", () => {
  it.each([
    ["an HTML page", "<!doctype html><html><body>app</body></html>"],
    ["an empty body", ""],
    ["another exporter's page", "go_goroutines 12\nhttp_requests_total 3\n"],
  ])("%s", (_, page) => {
    expect(() => readWorkerMetrics(page)).toThrow(ApiError);
  });

  it("accepts a page with one series it reads", () => {
    expect(readWorkerMetrics("dita_worker_uptime_seconds 3\n").series.length).toBeGreaterThan(0);
  });
});

describe("residentFor", () => {
  it.each([
    ["resident", "dita_worker_model_resident 1\ndita_worker_model_resident_seconds 42\n", 42],
    ["not resident", "dita_worker_model_resident 0\ndita_worker_model_resident_seconds 0\n", null],
    ["no gauge", "dita_worker_model_resident_seconds 42\n", null],
  ])("%s", (_, page, want) => {
    expect(residentFor(readWorkerMetrics(page))).toBe(want);
  });
});
