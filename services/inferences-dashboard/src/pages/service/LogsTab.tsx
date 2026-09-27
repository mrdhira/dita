import type { DeployedService } from "../../lib/fleet";

export const DOZZLE = "https://dozzle.home.arpa";

export function LogsTab({ service }: { service: DeployedService }) {
  return (
    <div className="space-y-3 text-sm">
      <p>
        This console keeps no logs. The container&apos;s own log is in Dozzle, which already runs on
        this box:{" "}
        <a href={DOZZLE} target="_blank" rel="noreferrer" className="text-indigo-700 underline">
          open Dozzle in a new tab
        </a>{" "}
        and choose <span className="font-mono">{service.container}</span>.
      </p>
      <p>From a terminal on the host:</p>
      <pre className="overflow-x-auto rounded bg-slate-50 p-2 font-mono text-xs">
        docker logs --tail 200 --follow {service.container}
      </pre>
      <p className="text-xs text-slate-600">
        The Python workers log plain text, not JSON: filter with grep, not a query.
      </p>
    </div>
  );
}
