import { clock } from "../lib/visibility";

/** The time of the data and why it is not newer; only the why is announced, never the clock. */
export function AsOf({ at, polling, failed }: { at: number; polling: boolean; failed: boolean }) {
  if (at === 0) return null;
  return (
    <p className="text-xs text-slate-600">
      as of <time dateTime={new Date(at).toISOString()}>{clock(at)}</time>
      <span role="status">
        {!polling && " · paused while this tab is hidden"}
        {failed && " · the latest refresh failed; this is the last good answer"}
      </span>
    </p>
  );
}
