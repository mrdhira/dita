import type { StateView, Tone } from "../lib/fleet";

const tones: Record<Tone, string> = {
  good: "border-emerald-700 bg-emerald-50 text-emerald-900",
  warn: "border-amber-700 bg-amber-50 text-amber-900",
  bad: "border-red-700 bg-red-50 text-red-900",
  idle: "border-slate-500 bg-slate-50 text-slate-700",
};

export function StateBadge({ view }: { view: StateView }) {
  return (
    <span
      data-tone={view.tone}
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${tones[view.tone]}`}
    >
      <span aria-hidden="true">{view.shape}</span>
      {view.word}
    </span>
  );
}
