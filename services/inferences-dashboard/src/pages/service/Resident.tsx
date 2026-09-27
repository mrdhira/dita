import type { WorkerReport } from "../../api/client";
import { residentOf } from "../../lib/fleet";

/** What a tab says in place of a model identity it does not have; see residentOf. */
export function NoIdentity({ report }: { report: WorkerReport | undefined }) {
  const resident = residentOf(report);
  if (resident.kind === "none") {
    return (
      <p className="text-sm text-slate-700">
        No model is resident: the worker says so itself. Only one model is ever resident in a
        worker; requests fail until one is loaded.
      </p>
    );
  }
  if (resident.kind === "unknown") {
    return (
      <p className="text-sm text-slate-700">
        The resident model is unknown: {resident.why}, so this page does not say which model, if
        any, is loaded.
      </p>
    );
  }
  return null;
}
