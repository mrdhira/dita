import type { WorkerReport } from "../../api/client";
import { ErrorBanner } from "../../components/ErrorBanner";
import type { DeployedService } from "../../lib/fleet";
import { first, formatBytes, formatDuration, observations, total } from "../../lib/metrics";
import { IDENTITY } from "./info";
import { InfoList } from "./InfoList";
import { useWorkerMetrics } from "./useWorkerMetrics";

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-200 p-3">
      <p className="text-xs text-slate-600">{label}</p>
      <p className="font-mono text-sm">{value}</p>
    </div>
  );
}

export function OverviewTab({
  service,
  report,
}: {
  service: DeployedService;
  report: WorkerReport | undefined;
}) {
  const { query } = useWorkerMetrics(service.id);
  const m = query.data;
  const or = (v: number | null, f: (n: number) => string) => (v === null ? "—" : f(v));
  return (
    <div className="space-y-4">
      <p className="text-sm">
        <span className="font-semibold">Blast radius: </span>
        {service.blastRadius}
      </p>
      <section aria-label="resident model" className="space-y-2">
        <h3 className="text-sm font-semibold">Resident model</h3>
        {report?.info ? (
          <InfoList info={report.info} keys={IDENTITY} />
        ) : (
          <p className="text-sm text-slate-700">
            No model identity: the worker&apos;s /info did not answer, which it does only while a
            model is resident.
          </p>
        )}
      </section>
      <section aria-label="since the last restart" className="space-y-2">
        <h3 className="text-sm font-semibold">From /metrics</h3>
        {query.error && <ErrorBanner error={query.error} />}
        {m && (
          <>
            {first(m, "dita_worker_model_loading") === 1 && (
              <p className="text-sm text-amber-900">loading a model right now</p>
            )}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              <Figure
                label="resident for"
                value={or(first(m, "dita_worker_model_resident_seconds"), formatDuration)}
              />
              <Figure
                label="RAM now"
                value={or(first(m, "process_resident_memory_bytes"), formatBytes)}
              />
              <Figure
                label="RAM peak"
                value={or(first(m, "process_resident_memory_peak_bytes"), formatBytes)}
              />
              <Figure
                label="inferences, since the last restart"
                value={or(observations(m, "dita_worker_infer_duration_seconds"), String)}
              />
              <Figure
                label="DIP failures, since the last restart"
                value={or(total(m, "dita_worker_errors_total"), String)}
              />
              <Figure
                label="uptime"
                value={or(first(m, "dita_worker_uptime_seconds"), formatDuration)}
              />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
