import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Evaluation } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { ProbabilityBar, percent } from "../components/ProbabilityBar";
import { CsvError, parseLabelledCsv } from "../lib/csv";

/** One run: the model against the majority-class baseline, side by side, never alone. */
export function EvaluationResult({ result }: { result: Evaluation }) {
  const rows = [
    {
      metric: "accuracy (higher is better)",
      model: result.accuracy,
      baseline: result.baseline.accuracy,
    },
    { metric: "Brier (lower is better)", model: result.brier, baseline: result.baseline.brier },
  ];
  return (
    <section aria-label={`evaluation ${result.name}`} className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h3 className="font-semibold">
          {result.name} · {result.rows} rows
        </h3>
        <span
          data-testid="verdict"
          className={`rounded px-2 py-0.5 text-xs ${result.beats_baseline ? "bg-emerald-100 text-emerald-900" : "bg-red-100 text-red-900"}`}
        >
          {result.beats_baseline
            ? "beats the majority-class baseline"
            : "does not beat the majority-class baseline"}
        </span>
      </header>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="font-normal">metric</th>
            <th className="font-normal">model</th>
            <th className="font-normal">majority baseline ({result.baseline.class})</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.metric}>
              <td className="py-1">{r.metric}</td>
              <td className="tabular-nums">{r.model.toFixed(3)}</td>
              <td className="tabular-nums">{r.baseline.toFixed(3)}</td>
            </tr>
          ))}
          <tr>
            <td className="py-1">ECE, 10 bins (lower is better)</td>
            <td className="tabular-nums">{result.ece.toFixed(3)}</td>
            <td className="text-xs text-slate-500">0 by construction</td>
          </tr>
        </tbody>
      </table>
      <table className="w-full text-sm">
        <caption className="text-left text-xs text-slate-500">per class</caption>
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="font-normal">class</th>
            <th className="font-normal">support</th>
            <th className="font-normal">precision</th>
            <th className="w-1/3 font-normal">recall</th>
          </tr>
        </thead>
        <tbody>
          {result.classes.map((c) => (
            <tr key={c.class}>
              <td className="py-0.5 font-mono">{c.class}</td>
              <td className="tabular-nums">{c.support}</td>
              <td className="tabular-nums">{c.precision === null ? "—" : percent(c.precision)}</td>
              <td>
                <div className="flex items-center gap-2">
                  <ProbabilityBar value={c.recall ?? 0} label={`recall of ${c.class}`} />
                  <span className="w-14 text-right tabular-nums">
                    {c.recall === null ? "—" : percent(c.recall)}
                  </span>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/**
 * States what the worker applies, which is nothing: the checkpoint ships every temperature at
 * 1.0 and nothing has been fitted, since there are no labels yet. Change this text only when a
 * fitted checkpoint lands.
 */
export function CalibrationPanel() {
  return (
    <section
      aria-label="calibration"
      className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
    >
      <h2 className="font-semibold">Calibration: none fitted</h2>
      <p data-testid="calibration">
        Every temperature in the checkpoint is 1.0, so temperature scaling is the identity
        transform: the probabilities shown are the model&apos;s raw softmax, uncalibrated. The ECE
        below measures that uncalibrated output.
      </p>
    </section>
  );
}

export function EvalPage() {
  const client = useQueryClient();
  const history = useQuery({ queryKey: ["evaluations"], queryFn: api.evaluations });
  const [parseError, setParseError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: (input: { name: string; text: string }) =>
      api.evaluate(input.name, parseLabelledCsv(input.text)),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });

  return (
    <div className="space-y-6">
      <CalibrationPanel />
      <form aria-label="evaluate a labelled set" className="space-y-2">
        <label className="block text-sm font-medium">
          Labelled CSV: a <code>label</code> column and one <code>p:&lt;option&gt;</code> column per
          option
          <input
            type="file"
            accept=".csv,text/csv"
            className="mt-1 block text-sm"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              void file.text().then((text) => {
                try {
                  parseLabelledCsv(text);
                  setParseError(null);
                  run.mutate({ name: file.name, text });
                } catch (err) {
                  setParseError(err instanceof CsvError ? err.message : String(err));
                }
              });
            }}
          />
        </label>
      </form>
      {parseError && (
        <p role="alert" className="text-sm text-red-800">
          The CSV could not be read: {parseError}
        </p>
      )}
      {run.error && <ErrorBanner error={run.error} />}
      {run.data && <EvaluationResult result={run.data} />}
      <section aria-label="previous evaluations">
        <h2 className="text-sm font-semibold">Previous runs</h2>
        <ul className="text-sm">
          {history.data?.evaluations.map((e) => (
            <li key={e.id}>
              {e.name}: accuracy {e.accuracy.toFixed(3)} vs baseline{" "}
              {e.baseline.accuracy.toFixed(3)} · {new Date(e.created_at).toLocaleString()}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
