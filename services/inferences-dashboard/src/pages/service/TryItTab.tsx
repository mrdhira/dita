import { useMutation } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { api, type TryResult } from "../../api/client";
import { ErrorBanner } from "../../components/ErrorBanner";
import type { DeployedService, TryRoute } from "../../lib/fleet";
import type { Info } from "./info";

const input = "mt-1 block w-full rounded-md border border-slate-300 p-2 font-mono text-sm";
const lines = (text: string) => text.split("\n").filter((l) => l.trim() !== "");
/** A value the person typed that is not a number is sent as typed: the worker judges it. */
const numberOrRaw = (raw: string) => (/^-?\d+$/.test(raw) ? Number(raw) : raw);

const QUESTIONS = `[
  {
    "name": "severity",
    "type": "choice",
    "options": ["low", "high"],
    "criteria": "How urgent is this for the person on call?"
  }
]`;

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block text-sm font-medium">
      {label}
      {children}
    </label>
  );
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="inline-flex min-h-6 items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
        className="h-4 w-4"
      />
      {label}
    </label>
  );
}

function EmbedFields({ info, onBody }: { info: Info | null; onBody: (b: string) => void }) {
  const [inputs, setInputs] = useState("");
  const [normalize, setNormalize] = useState(true);
  const [truncate, setTruncate] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [dimensions, setDimensions] = useState("");
  const prompts = Array.isArray(info?.prompt_names) ? (info.prompt_names as string[]) : [];
  useEffect(() => {
    onBody(
      JSON.stringify({
        inputs: lines(inputs),
        normalize,
        truncate,
        ...(prompt && { prompt_name: prompt }),
        ...(dimensions.trim() && { dimensions: numberOrRaw(dimensions.trim()) }),
      }),
    );
  }, [inputs, normalize, truncate, prompt, dimensions, onBody]);
  return (
    <>
      <Field label="inputs, one per line">
        <textarea
          name="inputs"
          rows={4}
          value={inputs}
          onChange={(e) => {
            setInputs(e.target.value);
          }}
          className={input}
        />
      </Field>
      <div className="flex flex-wrap gap-4">
        <Check label="normalize" checked={normalize} onChange={setNormalize} />
        <Check label="truncate" checked={truncate} onChange={setTruncate} />
      </div>
      <Field label="prompt_name">
        <select
          name="prompt_name"
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
          }}
          className="mt-1 block rounded-md border border-slate-300 p-2 text-sm"
        >
          <option value="">none</option>
          {prompts.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </Field>
      <Field label="dimensions (optional)">
        <input
          name="dimensions"
          value={dimensions}
          onChange={(e) => {
            setDimensions(e.target.value);
          }}
          className={input}
        />
      </Field>
    </>
  );
}

function RerankFields({ onBody }: { onBody: (b: string) => void }) {
  const [query, setQuery] = useState("");
  const [texts, setTexts] = useState("");
  const [rawScores, setRawScores] = useState(false);
  const [returnText, setReturnText] = useState(false);
  const [truncate, setTruncate] = useState("");
  useEffect(() => {
    onBody(
      JSON.stringify({
        query,
        texts: lines(texts),
        raw_scores: rawScores,
        return_text: returnText,
        ...(truncate && { truncate: truncate === "true" }),
      }),
    );
  }, [query, texts, rawScores, returnText, truncate, onBody]);
  return (
    <>
      <Field label="query">
        <input
          name="query"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
          }}
          className={input}
        />
      </Field>
      <Field label="texts, one per line">
        <textarea
          name="texts"
          rows={4}
          value={texts}
          onChange={(e) => {
            setTexts(e.target.value);
          }}
          className={input}
        />
      </Field>
      <div className="flex flex-wrap gap-4">
        <Check label="raw_scores" checked={rawScores} onChange={setRawScores} />
        <Check label="return_text" checked={returnText} onChange={setReturnText} />
      </div>
      <Field label="truncate">
        <select
          name="truncate"
          value={truncate}
          onChange={(e) => {
            setTruncate(e.target.value);
          }}
          className="mt-1 block rounded-md border border-slate-300 p-2 text-sm"
        >
          <option value="">the model&apos;s default</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </Field>
    </>
  );
}

function DecideFields({ onBody }: { onBody: (b: string) => void }) {
  const [text, setText] = useState("");
  const [questions, setQuestions] = useState(QUESTIONS);
  useEffect(() => {
    // The questions are spliced in as typed, so malformed JSON reaches the worker's own refusal.
    onBody(`{"text":${JSON.stringify(text)},"questions":${questions}}`);
  }, [text, questions, onBody]);
  return (
    <>
      <Field label="text">
        <textarea
          name="text"
          rows={4}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
          }}
          className={input}
        />
      </Field>
      <Field label="questions (JSON)">
        <textarea
          name="questions"
          rows={8}
          value={questions}
          onChange={(e) => {
            setQuestions(e.target.value);
          }}
          className={input}
        />
      </Field>
    </>
  );
}

function useNow(running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 100);
    return () => {
      clearInterval(timer);
    };
  }, [running]);
  return now;
}

function Answer({ result }: { result: TryResult }) {
  return (
    <section aria-label="response" className="space-y-2">
      <p className="font-mono text-sm">
        HTTP {result.status}
        {result.computeTime !== null && ` · x-compute-time ${result.computeTime}`}
        {result.modelId !== null && ` · x-model-id ${result.modelId}`}
      </p>
      {result.problem && (
        <div
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
        >
          <p className="font-semibold">
            refused · error_type{" "}
            <span className="font-mono">{result.problem.error_type ?? "—"}</span>
          </p>
          <p className="mt-1 font-mono break-words">{result.problem.error}</p>
        </div>
      )}
      <pre className="max-h-96 overflow-auto rounded bg-slate-50 p-2 font-mono text-xs break-all whitespace-pre-wrap">
        {result.body}
      </pre>
    </section>
  );
}

export function TryItTab({ service, info }: { service: DeployedService; info: Info | null }) {
  const [body, setBody] = useState("");
  const send = useMutation({
    mutationFn: ({ route, body }: { route: TryRoute; body: string }) => api.tryIt(route, body),
  });
  const [since, setSince] = useState(0);
  const elapsed = Math.max(0, useNow(send.isPending) - since);
  return (
    <div className="space-y-4">
      <form
        aria-label={`try ${service.tryRoute}`}
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setSince(Date.now());
          send.mutate({ route: service.tryRoute, body });
        }}
      >
        {service.tryRoute === "/embed" && <EmbedFields info={info} onBody={setBody} />}
        {service.tryRoute === "/rerank" && <RerankFields onBody={setBody} />}
        {service.tryRoute === "/decide" && <DecideFields onBody={setBody} />}
        <details className="text-xs">
          <summary className="cursor-pointer text-slate-700">the exact body sent</summary>
          <pre className="mt-2 overflow-x-auto rounded bg-slate-50 p-2">{body}</pre>
        </details>
        <button
          type="submit"
          disabled={send.isPending}
          className="min-h-6 rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-40"
        >
          {send.isPending
            ? `Waiting for the worker… ${(elapsed / 1000).toFixed(1)} s`
            : `POST /api/inferences${service.tryRoute}`}
        </button>
      </form>
      {send.error && <ErrorBanner error={send.error} />}
      {send.data && <Answer result={send.data} />}
    </div>
  );
}
