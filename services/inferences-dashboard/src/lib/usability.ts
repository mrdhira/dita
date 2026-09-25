import type { Fault, Template } from "../api/client";
import { templateFaults } from "../contract/schema";

export interface Usability {
  /** The version runs: the runtime rules, which keep templates saved under older rules working. */
  usable: boolean;
  faults: Fault[];
  /** What the editor's rules would refuse today: a reason to save the next version, not a block. */
  authoring: Fault[];
}

/**
 * Whether a stored version runs is the orchestrator's verdict, and the dashboard only displays
 * it. The fallback below is for an orchestrator that predates `usable`: it applies the editor's
 * rules, which are stricter than the runtime's, and is a stopgap, never a second source of truth.
 */
export function usability(t: Template): Usability {
  if (t.usable !== undefined) {
    return {
      usable: t.usable,
      faults: t.faults ?? [],
      authoring: t.authoring_issues ?? templateFaults(t),
    };
  }
  const faults = templateFaults(t);
  return { usable: faults.length === 0, faults, authoring: faults };
}
