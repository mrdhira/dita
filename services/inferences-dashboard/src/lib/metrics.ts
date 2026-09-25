import { ApiError } from "../api/client";

export type Unit = "count" | "seconds" | "bytes" | "flag";

export interface SeriesDef {
  name: string;
  title: string;
  unit: Unit;
  counter: boolean;
}

/** The fixed subset the console reads (design §9); anything else in the page is ignored. */
export const SERIES: readonly SeriesDef[] = [
  {
    name: "dita_worker_ops_total",
    title: "DIP socket operations, health probes included",
    unit: "count",
    counter: true,
  },
  {
    name: "dita_worker_errors_total",
    title: "failures, by DIP error code",
    unit: "count",
    counter: true,
  },
  { name: "dita_worker_model_loads_total", title: "model loads", unit: "count", counter: true },
  {
    name: "dita_worker_model_evictions_total",
    title: "loads that displaced a model",
    unit: "count",
    counter: true,
  },
  { name: "dita_worker_model_resident", title: "model resident", unit: "flag", counter: false },
  {
    name: "dita_worker_model_resident_seconds",
    title: "resident for",
    unit: "seconds",
    counter: false,
  },
  { name: "dita_worker_model_loading", title: "loading", unit: "flag", counter: false },
  { name: "dita_worker_uptime_seconds", title: "uptime", unit: "seconds", counter: false },
  { name: "process_resident_memory_bytes", title: "RAM now", unit: "bytes", counter: false },
  {
    name: "process_resident_memory_peak_bytes",
    title: "RAM peak since start",
    unit: "bytes",
    counter: false,
  },
  { name: "process_cpu_seconds_total", title: "CPU time", unit: "seconds", counter: true },
];

export const HISTOGRAMS = [
  { name: "dita_worker_load_duration_seconds", title: "model load duration" },
  { name: "dita_worker_infer_duration_seconds", title: "inference duration" },
] as const;

export type Labels = Record<string, string>;

export interface Sample {
  name: string;
  labels: Labels;
  value: number;
}

export interface Histogram {
  name: string;
  labels: Labels;
  buckets: { le: number; count: number }[];
  sum: number | null;
  count: number | null;
}

export interface WorkerMetrics {
  series: { def: SeriesDef; samples: Sample[] }[];
  histograms: { title: string; name: string; series: Histogram[] }[];
}

const LINE = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$/;
const LABEL = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g;

function number(raw: string): number {
  if (raw === "+Inf") return Infinity;
  if (raw === "-Inf") return -Infinity;
  return Number(raw);
}

const unescape = (v: string) => v.replace(/\\(.)/g, (_, c: string) => (c === "n" ? "\n" : c));

/** Prometheus text exposition, samples only; comments and malformed lines are skipped. */
export function parseExposition(text: string): Sample[] {
  const samples: Sample[] = [];
  for (const line of text.split("\n")) {
    const m = LINE.exec(line.trim());
    if (!m?.[1] || m[3] === undefined) continue;
    const labels: Labels = {};
    for (const l of (m[2] ?? "").matchAll(LABEL)) {
      if (l[1] && l[2] !== undefined) labels[l[1]] = unescape(l[2]);
    }
    samples.push({ name: m[1], labels, value: number(m[3]) });
  }
  return samples;
}

const key = (labels: Labels) =>
  JSON.stringify(Object.entries(labels).sort(([a], [b]) => a.localeCompare(b)));

function histogramsOf(name: string, samples: Sample[]): Histogram[] {
  const byLabels = new Map<string, Histogram>();
  const entry = (labels: Labels) => {
    const k = key(labels);
    let h = byLabels.get(k);
    if (!h) {
      h = { name, labels, buckets: [], sum: null, count: null };
      byLabels.set(k, h);
    }
    return h;
  };
  for (const s of samples) {
    if (s.name === `${name}_bucket`) {
      const { le, ...rest } = s.labels;
      if (le !== undefined) entry(rest).buckets.push({ le: number(le), count: s.value });
    } else if (s.name === `${name}_sum`) {
      entry(s.labels).sum = s.value;
    } else if (s.name === `${name}_count`) {
      entry(s.labels).count = s.value;
    }
  }
  for (const h of byLabels.values()) h.buckets.sort((a, b) => a.le - b.le);
  return [...byLabels.values()];
}

const RECOGNISED = new Set([
  ...SERIES.map((s) => s.name),
  ...HISTOGRAMS.flatMap((h) => [`${h.name}_bucket`, `${h.name}_sum`, `${h.name}_count`]),
]);

/** A page with none of the series this console reads is refused: it would read as an idle worker. */
export function readWorkerMetrics(text: string): WorkerMetrics {
  const samples = parseExposition(text);
  if (!samples.some((s) => RECOGNISED.has(s.name))) {
    throw new ApiError(502, {
      error:
        "the answer is not a worker's /metrics page: it has none of the series this console reads",
      error_type: "NotMetrics",
    });
  }
  return {
    series: SERIES.map((def) => ({ def, samples: samples.filter((s) => s.name === def.name) })),
    histograms: HISTOGRAMS.map((h) => ({ ...h, series: histogramsOf(h.name, samples) })),
  };
}

/** Seconds resident, only while the worker's own gauge says a model is resident. */
export function residentFor(metrics: WorkerMetrics): number | null {
  return first(metrics, "dita_worker_model_resident") === 1
    ? first(metrics, "dita_worker_model_resident_seconds")
    : null;
}

export function first(metrics: WorkerMetrics, name: string): number | null {
  return metrics.series.find((s) => s.def.name === name)?.samples[0]?.value ?? null;
}

export function total(metrics: WorkerMetrics, name: string): number | null {
  const samples = metrics.series.find((s) => s.def.name === name)?.samples;
  return samples ? samples.reduce((sum, s) => sum + s.value, 0) : null;
}

export function observations(metrics: WorkerMetrics, name: string): number | null {
  const series = metrics.histograms.find((h) => h.name === name)?.series ?? [];
  return series.length === 0 ? null : series.reduce((sum, h) => sum + (h.count ?? 0), 0);
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} d ${h % 24} h`;
}

export function formatBytes(bytes: number): string {
  const mib = bytes / 2 ** 20;
  return mib < 1024 ? `${mib.toFixed(0)} MiB` : `${(mib / 1024).toFixed(2)} GiB`;
}

export function formatValue(value: number, unit: Unit): string {
  switch (unit) {
    case "seconds":
      return formatDuration(value);
    case "bytes":
      return formatBytes(value);
    case "flag":
      return value === 1 ? "yes" : "no";
    case "count":
      return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
}

/** The labels worth showing: the worker's own name is the page it is on. */
export function labelText(labels: Labels): string {
  return Object.entries(labels)
    .filter(([k]) => k !== "worker")
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
}
