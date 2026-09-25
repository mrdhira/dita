import { useQuery } from "@tanstack/react-query";
import { memo } from "react";
import { Link } from "react-router";
import { api, type WorkerReport } from "../api/client";
import { AsOf } from "../components/AsOf";
import { ErrorBanner } from "../components/ErrorBanner";
import { StaleNote, StateBadge } from "../components/StateBadge";
import {
  FLEET,
  describeState,
  isDeployed,
  notDeployed,
  residentOf,
  type Service,
} from "../lib/fleet";
import { formatDuration, residentFor } from "../lib/metrics";
import { useStaleness } from "../lib/stale";
import { usePollInterval } from "../lib/visibility";
import { useWorkerMetrics } from "./service/useWorkerMetrics";

const cell = "py-2 pr-4 align-top max-sm:block max-sm:py-0.5";
/** Cards at phone width have no header row, so each value carries its column's name. */
const labelled = `${cell} max-sm:before:mr-2 max-sm:before:font-sans max-sm:before:text-slate-600 max-sm:before:content-[attr(data-label)]`;

function ResidentModel({
  report,
  deployed,
}: {
  report: WorkerReport | undefined;
  deployed: boolean;
}) {
  const resident = residentOf(report);
  if (!deployed) return <span className="text-slate-600">—</span>;
  if (resident.kind === "none") return <span className="text-slate-600">none resident</span>;
  const info = resident.kind === "known" ? resident.info : {};
  const id = typeof info.model_id === "string" ? info.model_id : null;
  const sha = typeof info.model_sha === "string" ? info.model_sha : null;
  const rev = typeof info.model_revision === "string" ? info.model_revision : null;
  const revision = (sha ?? rev)?.slice(0, 7);
  if (!id) return <span className="text-slate-600">unknown</span>;
  return (
    <>
      {id}
      {revision && <span className="block text-slate-600">@{revision}</span>}
    </>
  );
}

function ResidentFor({ service }: { service: string }) {
  const { query, stale } = useWorkerMetrics(service);
  const seconds = query.data && stale === null ? residentFor(query.data) : null;
  return <>{seconds === null ? "—" : formatDuration(seconds)}</>;
}

/** Memoised: a 5 s tick re-renders only the rows whose report changed (design §14). */
const FleetRow = memo(function FleetRow({
  service,
  report,
  staleSince,
}: {
  service: Service;
  report: WorkerReport | undefined;
  staleSince: number | null;
}) {
  const deployed = isDeployed(service);
  const view = report
    ? describeState(report)
    : deployed
      ? { word: "not reported", tone: "idle" as const, shape: "?", sentence: "" }
      : notDeployed(service);
  const stale = report !== undefined && staleSince !== null;
  return (
    <tr
      aria-label={service.container}
      className="border-t border-slate-200 max-sm:mb-3 max-sm:block max-sm:rounded-md max-sm:border max-sm:p-3"
    >
      <td className={cell}>
        <span className="font-medium">{service.container}</span>
        <span className="block text-xs text-slate-600">{service.role}</span>
      </td>
      <td className={cell}>
        <StateBadge view={view} stale={stale} />
        {view.sentence && (
          <span className="mt-1 block text-xs text-slate-700">{view.sentence}</span>
        )}
        {stale && <StaleNote at={staleSince} />}
      </td>
      <td data-label="resident model" className={`${labelled} font-mono text-xs`}>
        <ResidentModel report={report} deployed={deployed} />
      </td>
      <td data-label="resident for" className={`${labelled} font-mono text-xs`}>
        {deployed ? <ResidentFor service={service.id} /> : "—"}
      </td>
      <td
        data-label="last error"
        className={`${labelled} max-w-xs text-xs break-words text-slate-700`}
      >
        {report?.error ?? "—"}
      </td>
      <td className={cell}>
        {deployed && (
          <Link
            to={`/services/${service.id}`}
            className="inline-block min-h-6 text-sm text-indigo-700 underline"
          >
            Open {service.id}
          </Link>
        )}
      </td>
    </tr>
  );
});

export function FleetPage() {
  const interval = usePollInterval(5_000);
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: api.workers,
    refetchInterval: interval,
  });
  const byName = new Map(workers.data?.workers.map((w) => [w.name, w]));
  const known = new Set(FLEET.map((s) => s.container));
  const extra = (workers.data?.workers ?? []).filter((w) => !known.has(w.name));
  const stale = useStaleness(workers, interval);
  const staleSince = stale ? workers.dataUpdatedAt : null;
  const unreported = FLEET.filter((s) => !isDeployed(s)).every((s) => !byName.has(s.container));

  return (
    <section aria-label="fleet" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">Fleet</h2>
        <AsOf at={workers.dataUpdatedAt} polling={interval !== false} stale={stale} />
      </div>
      {workers.error && !workers.data && <ErrorBanner error={workers.error} />}
      <table className="w-full text-sm max-sm:block">
        <thead className="max-sm:hidden">
          <tr className="text-left text-xs text-slate-600">
            <th className="pr-4 font-normal">service</th>
            <th className="pr-4 font-normal">state</th>
            <th className="pr-4 font-normal">resident model</th>
            <th className="pr-4 font-normal">resident for</th>
            <th className="pr-4 font-normal">last error</th>
            <th className="font-normal">details</th>
          </tr>
        </thead>
        <tbody className="max-sm:block">
          {FLEET.map((s) => (
            <FleetRow
              key={s.id}
              service={s}
              report={byName.get(s.container)}
              staleSince={staleSince}
            />
          ))}
          {extra.map((w) => (
            <FleetRow
              key={w.name}
              service={{
                id: w.name,
                container: w.name,
                role: "reported by the gateway",
                reason: "",
              }}
              report={w}
              staleSince={staleSince}
            />
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-600">
        Resident for is read from each worker&apos;s /metrics every 30 s.
        {unreported &&
          " The not-deployed rows are known to this console, not reported by the gateway: OCR speaks only DIP, which the gateway does not probe, and STT and TTS have no code yet. Nothing here can start them."}
      </p>
    </section>
  );
}
