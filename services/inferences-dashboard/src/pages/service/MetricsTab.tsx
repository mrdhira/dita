import { AsOf } from "../../components/AsOf";
import { ErrorBanner } from "../../components/ErrorBanner";
import { formatValue, labelText, type Histogram } from "../../lib/metrics";
import { useWorkerMetrics } from "./useWorkerMetrics";

const le = (v: number) => (v === Infinity ? "+Inf" : `${v} s`);

function HistogramTable({ h }: { h: Histogram }) {
  const labels = labelText(h.labels);
  return (
    <div className="space-y-1">
      {labels && <p className="font-mono text-xs text-slate-600">{labels}</p>}
      <table className="text-sm" aria-label={`${h.name} ${labels}`.trim()}>
        <thead>
          <tr className="text-left text-xs text-slate-600">
            <th className="pr-6 font-normal">bucket (≤)</th>
            <th className="font-normal">cumulative count</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {h.buckets.map((b) => (
            <tr key={b.le}>
              <td className="pr-6">{le(b.le)}</td>
              <td>{b.count}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="font-mono text-xs">
        _sum {h.sum ?? "—"} s · _count {h.count ?? "—"}
        {h.sum !== null && h.count ? ` · average ${(h.sum / h.count).toFixed(3)} s` : ""}
      </p>
    </div>
  );
}

export function MetricsTab({ service }: { service: string }) {
  const { query, polling } = useWorkerMetrics(service);
  const m = query.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-xs text-slate-700">
          Read from the worker&apos;s /metrics every 30 s. Nothing stores these: counters are since
          the last restart, and there is no history to chart.
        </p>
        <AsOf at={query.dataUpdatedAt} polling={polling} failed={query.isError} />
      </div>
      {query.error && <ErrorBanner error={query.error} />}
      {m && (
        <>
          <table className="w-full text-sm" aria-label="series">
            <thead>
              <tr className="text-left text-xs text-slate-600">
                <th className="pr-4 font-normal">series</th>
                <th className="pr-4 font-normal">labels</th>
                <th className="font-normal">value</th>
              </tr>
            </thead>
            <tbody>
              {m.series.map(({ def, samples }) =>
                samples.length === 0 ? (
                  <tr key={def.name} className="border-t border-slate-100">
                    <td className="py-1 pr-4">
                      {def.title}
                      <span className="block font-mono text-xs text-slate-600">{def.name}</span>
                    </td>
                    <td />
                    <td className="text-slate-700">
                      {def.counter ? "none since the last restart" : "not reported"}
                    </td>
                  </tr>
                ) : (
                  samples.map((s) => (
                    <tr key={def.name + labelText(s.labels)} className="border-t border-slate-100">
                      <td className="py-1 pr-4">
                        {def.title}
                        {def.counter && (
                          <span className="text-xs text-slate-600"> · since the last restart</span>
                        )}
                        <span className="block font-mono text-xs text-slate-600">{def.name}</span>
                      </td>
                      <td className="pr-4 font-mono text-xs">{labelText(s.labels)}</td>
                      <td className="font-mono">{formatValue(s.value, def.unit)}</td>
                    </tr>
                  ))
                ),
              )}
            </tbody>
          </table>
          {m.histograms.map((h) => (
            <section key={h.name} aria-label={h.title} className="space-y-2">
              <h3 className="text-sm font-semibold">
                {h.title} <span className="font-mono text-xs font-normal">{h.name}</span>
              </h3>
              <p className="text-xs text-slate-700">
                Bucket counts as the worker reports them, since the last restart. The average is sum
                ÷ count; nothing finer is estimated from the buckets.
              </p>
              {h.series.length === 0 ? (
                <p className="text-sm text-slate-700">no observations since the last restart</p>
              ) : (
                h.series.map((s) => <HistogramTable key={labelText(s.labels)} h={s} />)
              )}
            </section>
          ))}
        </>
      )}
    </div>
  );
}
