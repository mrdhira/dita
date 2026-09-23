import { describeError } from "../lib/errors";

export function ErrorBanner({ error }: { error: unknown }) {
  const { title, detail } = describeError(error);
  return (
    <div
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
    >
      <p className="font-semibold">{title}</p>
      <p className="mt-1 break-words">{detail}</p>
    </div>
  );
}
