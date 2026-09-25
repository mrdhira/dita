import type { WorkerReport } from "../../api/client";
import { IDENTITY } from "./info";
import { InfoList } from "./InfoList";

export function ModelsTab({ report }: { report: WorkerReport | undefined }) {
  const info = report?.info;
  const rest = info
    ? Object.keys(info).filter((k) => !(IDENTITY as readonly string[]).includes(k))
    : [];
  return (
    <div className="space-y-4">
      <section aria-label="resident" className="space-y-2 rounded-md border border-slate-200 p-3">
        <h3 className="text-sm font-semibold">Resident now</h3>
        {info ? (
          <>
            <InfoList info={info} keys={[...IDENTITY, ...rest]} />
            <details className="text-xs">
              <summary className="cursor-pointer text-slate-700">raw /info</summary>
              <pre className="mt-2 overflow-x-auto rounded bg-slate-50 p-2">
                {JSON.stringify(info, null, 2)}
              </pre>
            </details>
          </>
        ) : (
          <p className="text-sm text-slate-700">
            No model is resident. Only one model is ever resident in a worker; requests fail until
            one is loaded.
          </p>
        )}
      </section>
      <section
        aria-label="registry"
        className="space-y-1 rounded-md border border-dashed border-slate-400 p-3 text-sm"
      >
        <h3 className="font-semibold">Registered models: not available yet</h3>
        <p className="text-slate-700">
          This worker registers its models in its own models.yaml, but no route lists them: there is
          no <span className="font-mono">GET /models</span> on any worker today. The full registry,
          with pinned revisions and what is on disk, arrives with that read route in a later
          release. Until then this page shows only the resident model, and lists nothing it cannot
          read.
        </p>
      </section>
    </div>
  );
}
