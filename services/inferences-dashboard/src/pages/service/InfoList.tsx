import { show, type Info } from "./info";

export function InfoList({ info, keys }: { info: Info; keys: readonly string[] }) {
  const present = keys.filter((k) => k in info);
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {present.map((k) => (
        <div key={k} className="contents">
          <dt className="text-slate-600">{k}</dt>
          <dd className="font-mono break-all">{show(info[k])}</dd>
        </div>
      ))}
    </dl>
  );
}
