import { useQuery } from "@tanstack/react-query";
import { memo } from "react";
import { Link } from "react-router";
import { api, type WorkerReport } from "../api/client";
import { AsOf } from "../components/AsOf";
import { ErrorBanner } from "../components/ErrorBanner";
import { StateBadge } from "../components/StateBadge";
import { FLEET, describeState, isDeployed, notDeployed, type Service } from "../lib/fleet";
import { usePollInterval } from "../lib/visibility";

const cell = "py-2 pr-4 align-top max-sm:block max-sm:py-0.5";

function modelOf(report: WorkerReport | undefined) {
  const info = report?.info;
  const id = typeof info?.model_id === "string" ? info.model_id : null;
  const sha = typeof info?.model_sha === "string" ? info.model_sha : null;
  const rev = typeof info?.model_revision === "string" ? info.model_revision : null;
  return { id, revision: (sha ?? rev)?.slice(0, 7) ?? null };
}

/** Memoised: a 5 s tick re-renders only the rows whose report changed (design §14). */
const FleetRow = memo(function FleetRow({
  service,
  report,
}: {
  service: Service;
  report: WorkerReport | undefined;
}) {
  const deployed = isDeployed(service);
  const view = report
    ? describeState(report)
    : deployed
      ? { word: "not reported", tone: "idle" as const, shape: "?", sentence: "" }
      : notDeployed(service);
  const model = modelOf(report);
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
        <StateBadge view={view} />
        {view.sentence && (
          <span className="mt-1 block text-xs text-slate-700">{view.sentence}</span>
        )}
      </td>
      <td className={`${cell} font-mono text-xs`}>
        {model.id ? (
          <>
            {model.id}
            {model.revision && <span className="block text-slate-600">@{model.revision}</span>}
          </>
        ) : (
          <span className="text-slate-600">{deployed ? "none resident" : "—"}</span>
        )}
      </td>
      <td className={`${cell} max-w-xs text-xs break-words text-slate-700`}>
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

  return (
    <section aria-label="fleet" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">Fleet</h2>
        <AsOf at={workers.dataUpdatedAt} polling={interval !== false} failed={workers.isError} />
      </div>
      {workers.error && !workers.data && <ErrorBanner error={workers.error} />}
      <table className="w-full text-sm max-sm:block">
        <thead className="max-sm:hidden">
          <tr className="text-left text-xs text-slate-600">
            <th className="pr-4 font-normal">service</th>
            <th className="pr-4 font-normal">state</th>
            <th className="pr-4 font-normal">resident model</th>
            <th className="pr-4 font-normal">last error</th>
            <th className="font-normal">details</th>
          </tr>
        </thead>
        <tbody className="max-sm:block">
          {FLEET.map((s) => (
            <FleetRow key={s.id} service={s} report={byName.get(s.container)} />
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
            />
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-600">
        The last three rows are the intended fleet, known to this console rather than reported by
        the gateway: they have no HTTP surface to report from, and nothing here can start them.
      </p>
    </section>
  );
}
