import type { Answer } from "../api/client";
import { ProbabilityBar, percent } from "./ProbabilityBar";

/**
 * One question's answer under the recommendation contract: it is a suggestion, it shows the
 * top two alternatives and the confidence, and every option's probability is one step away.
 * There is no rendering of a bare single value.
 */
export function AnswerView({ answer }: { answer: Answer }) {
  const [first, second] = answer.options;
  return (
    <section
      aria-label={`suggestion for ${answer.question}`}
      className="rounded-md border border-dashed border-indigo-300 bg-indigo-50/60 p-4"
    >
      <header className="flex items-baseline justify-between gap-2">
        <h3 className="font-mono text-sm font-semibold text-slate-800">{answer.question}</h3>
        <span className="rounded bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-800">
          Rekomendasi · suggestion
        </span>
      </header>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <div>
          <dt className="text-xs text-slate-500">Top suggestion</dt>
          <dd data-testid="top-1">
            {first ? `${first.option} · ${percent(first.probability)}` : "no option scored"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Alternative</dt>
          <dd data-testid="top-2">
            {second ? `${second.option} · ${percent(second.probability)}` : "none"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Confidence</dt>
          <dd data-testid="confidence">{percent(answer.confidence)}</dd>
        </div>
      </dl>
      <table className="mt-3 w-full text-sm">
        <caption className="sr-only">every option for {answer.question}</caption>
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="w-1/3 font-normal">option</th>
            <th className="font-normal">probability</th>
            <th className="w-16 text-right font-normal" />
          </tr>
        </thead>
        <tbody>
          {answer.options.map((o) => (
            <tr key={o.option}>
              <td className="py-0.5 font-mono">{o.option}</td>
              <td className="py-0.5">
                <ProbabilityBar value={o.probability} label={o.option} />
              </td>
              <td className="py-0.5 text-right tabular-nums">{percent(o.probability)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
