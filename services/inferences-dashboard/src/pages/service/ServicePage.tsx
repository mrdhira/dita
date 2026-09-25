import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, useParams } from "react-router";
import { api } from "../../api/client";
import { AsOf } from "../../components/AsOf";
import { ErrorBanner } from "../../components/ErrorBanner";
import { StateBadge } from "../../components/StateBadge";
import { describeState, isDeployed, notDeployed, serviceById } from "../../lib/fleet";
import { usePollInterval } from "../../lib/visibility";
import { LogsTab } from "./LogsTab";
import { MetricsTab } from "./MetricsTab";
import { ModelsTab } from "./ModelsTab";
import { OverviewTab } from "./OverviewTab";
import { TryItTab } from "./TryItTab";

const TABS = [
  { path: "", label: "Overview" },
  { path: "models", label: "Models" },
  { path: "metrics", label: "Metrics" },
  { path: "logs", label: "Logs" },
  { path: "try", label: "Try it" },
] as const;

const back = (
  <Link to="/" className="text-sm text-indigo-700 underline">
    Back to Fleet
  </Link>
);

export function ServicePage() {
  const { id, tab = "" } = useParams();
  const service = serviceById(id);
  const interval = usePollInterval(10_000);
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: api.workers,
    enabled: service !== undefined && isDeployed(service),
    refetchInterval: interval,
  });

  if (!service) {
    return (
      <section aria-label="no such service" className="space-y-3">
        <h2 className="text-sm font-semibold">There is no service called {id}</h2>
        {back}
      </section>
    );
  }
  if (!isDeployed(service)) {
    return (
      <section aria-label={service.container} className="space-y-3">
        <h2 className="text-base font-semibold">{service.container}</h2>
        <StateBadge view={notDeployed(service)} />
        <p className="text-sm text-slate-700">
          {service.reason}. It has no tabs and no actions until it is deployed with an HTTP surface.
        </p>
        {back}
      </section>
    );
  }

  const report = workers.data?.workers.find((w) => w.name === service.container);
  const current = TABS.find((t) => t.path === tab);
  const view = report ? describeState(report) : null;
  return (
    <section aria-label={service.container} className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {service.container} <span className="text-sm font-normal">· {service.role}</span>
          </h2>
          {view && (
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <StateBadge view={view} />
              <span>{view.sentence}</span>
            </p>
          )}
        </div>
        <AsOf at={workers.dataUpdatedAt} polling={interval !== false} failed={workers.isError} />
      </div>
      {workers.error && !workers.data && <ErrorBanner error={workers.error} />}
      {workers.isSuccess && !report && (
        <p className="text-sm text-slate-700">The gateway does not report this service.</p>
      )}
      <nav
        aria-label="service tabs"
        className="flex flex-wrap gap-4 border-b border-slate-200 text-sm"
      >
        {TABS.map((t) => (
          <NavLink
            key={t.path}
            to={`/services/${service.id}${t.path && `/${t.path}`}`}
            end
            className={({ isActive }) =>
              `inline-block min-h-6 pb-2 ${isActive ? "border-b-2 border-indigo-700 font-semibold text-indigo-800" : "text-slate-700"}`
            }
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      {!current && <p className="text-sm">There is no {tab} tab.</p>}
      {current?.path === "" && <OverviewTab service={service} report={report} />}
      {current?.path === "models" && <ModelsTab report={report} />}
      {current?.path === "metrics" && <MetricsTab service={service.id} />}
      {current?.path === "logs" && <LogsTab service={service} />}
      {current?.path === "try" && <TryItTab service={service} info={report?.info ?? null} />}
    </section>
  );
}
