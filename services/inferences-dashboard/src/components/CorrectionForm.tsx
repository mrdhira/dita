import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Decision } from "../api/client";
import { ErrorBanner } from "./ErrorBanner";

/**
 * The human's answer: nothing is preselected, so every recorded answer was chosen, and
 * "use suggestion" is a click, never a default. Submitting writes the pair once.
 */
export function CorrectionForm({ decision }: { decision: Decision }) {
  const client = useQueryClient();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => api.correct(decision.id, answers),
    onSuccess: (updated) => {
      client.setQueryData(["decision", decision.id], updated);
      void client.invalidateQueries({ queryKey: ["decisions"] });
    },
  });
  const complete = decision.answers.every((a) => answers[a.question] !== undefined);

  return (
    <form
      aria-label="record the human answer"
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete) save.mutate();
      }}
    >
      {decision.answers.map((a) => {
        const top = a.options[0]?.option;
        return (
          <fieldset key={a.question} className="rounded-md border border-slate-200 p-3">
            <legend className="px-1 font-mono text-sm font-semibold">{a.question}</legend>
            <div className="flex flex-wrap gap-3">
              {a.options.map((o) => (
                <label key={o.option} className="flex items-center gap-1 text-sm">
                  <input
                    type="radio"
                    name={a.question}
                    value={o.option}
                    checked={answers[a.question] === o.option}
                    onChange={() => {
                      setAnswers({ ...answers, [a.question]: o.option });
                    }}
                  />
                  {o.option}
                </label>
              ))}
              {top !== undefined && (
                <button
                  type="button"
                  className="ml-auto rounded border border-indigo-300 px-2 text-xs text-indigo-800"
                  onClick={() => {
                    setAnswers({ ...answers, [a.question]: top });
                  }}
                >
                  use suggestion ({top})
                </button>
              )}
            </div>
          </fieldset>
        );
      })}
      {save.error && <ErrorBanner error={save.error} />}
      <button
        type="submit"
        disabled={!complete || save.isPending}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
      >
        {save.isPending ? "Recording…" : "Record answer"}
      </button>
    </form>
  );
}

/** A recorded correction, beside the prediction it belongs to. */
export function RecordedCorrection({ decision }: { decision: Decision }) {
  const c = decision.correction;
  if (!c) return null;
  return (
    <section
      aria-label="recorded answer"
      className="rounded-md border border-emerald-300 bg-emerald-50 p-4"
    >
      <h3 className="text-sm font-semibold text-emerald-900">
        Recorded {new Date(c.corrected_at).toLocaleString()}
      </h3>
      <ul className="mt-2 space-y-1 text-sm">
        {Object.entries(c.answers).map(([question, value]) => (
          <li key={question} data-testid={`recorded-${question}`}>
            <span className="font-mono">{question}</span>: {value}{" "}
            <span className="text-xs text-slate-600">({c.outcomes[question]})</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
