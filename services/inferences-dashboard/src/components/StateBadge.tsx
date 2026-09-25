import type { StateView, Tone } from "../lib/fleet";
import { clock } from "../lib/visibility";

const tones: Record<Tone, string> = {
  good: "border-emerald-700 bg-emerald-50 text-emerald-900",
  warn: "border-amber-700 bg-amber-50 text-amber-900",
  bad: "border-red-700 bg-red-50 text-red-900",
  idle: "border-slate-500 bg-slate-50 text-slate-700",
};

const staleTone = "border-dashed border-slate-500 bg-slate-100 text-slate-700";

/** A stale badge keeps the last word and shape but drops the colour, so it never reads as live. */
export function StateBadge({ view, stale = false }: { view: StateView; stale?: boolean }) {
  return (
    <span
      data-tone={view.tone}
      data-stale={stale || undefined}
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${stale ? staleTone : tones[view.tone]}`}
    >
      <span aria-hidden="true">{view.shape}</span>
      {view.word}
      {stale && <span className="font-normal"> · stale</span>}
    </span>
  );
}

export function StaleNote({ at }: { at: number }) {
  return (
    <span className="mt-1 block text-xs font-semibold text-red-900">
      stale: not refreshed since {clock(at)}
    </span>
  );
}
