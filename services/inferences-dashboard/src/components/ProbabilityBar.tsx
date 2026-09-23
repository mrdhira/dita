/** A probability as a bar: inline SVG, so no style attribute and nothing the CSP must allow. */
export function ProbabilityBar({ value, label }: { value: number; label: string }) {
  const width = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <svg
      viewBox="0 0 100 8"
      preserveAspectRatio="none"
      className="h-2 w-full text-indigo-500"
      role="img"
      aria-label={`${label}: ${percent(value)}`}
    >
      <rect x="0" y="0" width="100" height="8" className="fill-slate-200" />
      <rect x="0" y="0" width={width} height="8" fill="currentColor" />
    </svg>
  );
}

export function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}
