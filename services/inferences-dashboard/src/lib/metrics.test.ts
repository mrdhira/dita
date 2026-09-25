import { describe, expect, it } from "vitest";
import { parseExposition, readWorkerMetrics } from "./metrics";

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
