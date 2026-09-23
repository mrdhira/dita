import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useFieldArray, useForm } from "react-hook-form";
import { api, type Template } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { MAX_QUESTIONS, QUESTION_TYPES } from "../contract/schema";
import { fromDraft, templateResolver, toDraft, type TemplateForm } from "./templateForm";

const blank: TemplateForm = {
  name: "",
  description: "",
  questions: [{ name: "", type: "choice", optionsText: "", min: "", max: "" }],
};

/** Saving always writes a new version; an existing version is never edited in place. */
export function TemplatesPage() {
  const client = useQueryClient();
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const [editing, setEditing] = useState<string | null>(null);
  const form = useForm<TemplateForm>({ resolver: templateResolver, defaultValues: blank });
  const questions = useFieldArray({ control: form.control, name: "questions" });
  const save = useMutation({
    mutationFn: (values: TemplateForm) => api.saveTemplate(toDraft(values)),
    onSuccess: (t) => {
      void client.invalidateQueries({ queryKey: ["templates"] });
      setEditing(`${t.name} v${t.version} saved`);
      form.reset(fromDraft(t));
    },
  });
  const errors = form.formState.errors;

  const load = (t: Template) => {
    form.reset(fromDraft(t));
    setEditing(`editing ${t.name}: saving writes v${t.version + 1}`);
  };

  return (
    <div className="grid gap-8 md:grid-cols-[1fr_2fr]">
      <section aria-label="templates">
        <h2 className="mb-2 text-sm font-semibold">Templates</h2>
        {templates.error && <ErrorBanner error={templates.error} />}
        <ul className="space-y-1 text-sm">
          {templates.data?.templates.map((t) => (
            <li key={t.name}>
              <button
                type="button"
                className="text-indigo-700"
                onClick={() => {
                  load(t);
                }}
              >
                {t.name} v{t.version}
              </button>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="mt-3 text-sm text-slate-600 underline"
          onClick={() => {
            form.reset(blank);
            setEditing(null);
          }}
        >
          new template
        </button>
      </section>
      <form
        aria-label="template editor"
        className="space-y-4"
        onSubmit={(e) => {
          void form.handleSubmit((values) => {
            save.mutate(values);
          })(e);
        }}
      >
        {editing && <p className="text-xs text-slate-600">{editing}</p>}
        <label className="block text-sm font-medium">
          Name
          <input
            {...form.register("name")}
            className="mt-1 block w-full rounded-md border border-slate-300 p-2 font-mono text-sm"
          />
          {errors.name && <span className="text-xs text-red-700">{errors.name.message}</span>}
        </label>
        <label className="block text-sm font-medium">
          Description
          <input
            {...form.register("description")}
            className="mt-1 block w-full rounded-md border border-slate-300 p-2 text-sm"
          />
        </label>
        {questions.fields.map((field, i) => {
          const qe = errors.questions?.[i];
          return (
            <fieldset key={field.id} className="space-y-2 rounded-md border border-slate-200 p-3">
              <legend className="px-1 text-xs text-slate-500">question {i + 1}</legend>
              <div className="flex gap-2">
                <input
                  aria-label={`question ${i + 1} name`}
                  {...form.register(`questions.${i}.name`)}
                  placeholder="name"
                  className="flex-1 rounded-md border border-slate-300 p-2 font-mono text-sm"
                />
                <select
                  aria-label={`question ${i + 1} type`}
                  {...form.register(`questions.${i}.type`)}
                  className="rounded-md border border-slate-300 p-2 text-sm"
                >
                  {QUESTION_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
              {qe?.name && <span className="text-xs text-red-700">{qe.name.message}</span>}
              <textarea
                aria-label={`question ${i + 1} options`}
                {...form.register(`questions.${i}.optionsText`)}
                rows={3}
                placeholder="one option per line (2-20)"
                className="block w-full rounded-md border border-slate-300 p-2 font-mono text-sm"
              />
              {qe?.optionsText && (
                <span className="text-xs text-red-700">{qe.optionsText.message}</span>
              )}
              <div className="flex gap-2 text-sm">
                <input
                  aria-label={`question ${i + 1} range min`}
                  {...form.register(`questions.${i}.min`)}
                  placeholder="range min (score)"
                  className="w-40 rounded-md border border-slate-300 p-2"
                />
                <input
                  aria-label={`question ${i + 1} range max`}
                  {...form.register(`questions.${i}.max`)}
                  placeholder="range max (score)"
                  className="w-40 rounded-md border border-slate-300 p-2"
                />
                <button
                  type="button"
                  className="ml-auto text-xs text-slate-600"
                  onClick={() => {
                    questions.remove(i);
                  }}
                >
                  remove
                </button>
              </div>
              {qe?.min && <span className="text-xs text-red-700">{qe.min.message}</span>}
            </fieldset>
          );
        })}
        {errors.questions?.root && (
          <p className="text-xs text-red-700">{errors.questions.root.message}</p>
        )}
        <div className="flex gap-3">
          <button
            type="button"
            disabled={questions.fields.length >= MAX_QUESTIONS}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-40"
            onClick={() => {
              questions.append({ name: "", type: "choice", optionsText: "", min: "", max: "" });
            }}
          >
            add question
          </button>
          <button
            type="submit"
            disabled={save.isPending}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            Save as a new version
          </button>
        </div>
        {save.error && <ErrorBanner error={save.error} />}
      </form>
    </div>
  );
}
