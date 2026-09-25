import type { Staleness } from "../lib/stale";
import { clock } from "../lib/visibility";

/** The time of the data and why it is not newer; only the why is announced, never the clock. */
export function AsOf({ at, polling, stale }: { at: number; polling: boolean; stale: Staleness }) {
  if (at === 0) return null;
  return (
    <p className="text-xs text-slate-600">
      as of <time dateTime={new Date(at).toISOString()}>{clock(at)}</time>
      <span role="status">
        {!polling && " · paused while this tab is hidden"}
        {stale === "failed" && " · the latest refresh failed; this is the last good answer"}
        {stale === "late" && " · the latest refresh has not answered; this is the last good answer"}
      </span>
    </p>
  );
}
