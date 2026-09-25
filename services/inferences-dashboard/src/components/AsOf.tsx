import { clock } from "../lib/visibility";

/** A stale screen must never read as live: the time of the data, and why it is not newer. */
export function AsOf({ at, polling, failed }: { at: number; polling: boolean; failed: boolean }) {
  if (at === 0) return null;
  return (
    <p className="text-xs text-slate-600" aria-live="polite">
      as of <time dateTime={new Date(at).toISOString()}>{clock(at)}</time>
      {!polling && " · paused while this tab is hidden"}
      {failed && " · the latest refresh failed; this is the last good answer"}
    </p>
  );
}
