import { useEffect, useState } from "react";
import { ApiError, READ_DEADLINE_MS } from "../api/client";

/** Why the answer on screen is not current: its refresh failed, or it has gone unanswered. */
export type Staleness = "failed" | "late" | null;

/** A read the console abandoned at its deadline did not fail: it has not answered. */
const noAnswer = (error: unknown) =>
  error instanceof ApiError && error.problem.error_type === "NoAnswer";

/** A hung refresh never fails, so age marks it too: one missed poll plus a whole read deadline. */
export function useStaleness(
  query: { dataUpdatedAt: number; isError: boolean; error: unknown },
  interval: number | false,
): Staleness {
  const { dataUpdatedAt: at, isError, error } = query;
  const budget = interval === false ? null : 2 * interval + READ_DEADLINE_MS;
  const [late, setLate] = useState<{ at: number; budget: number } | null>(null);
  useEffect(() => {
    if (budget === null || at === 0) return;
    const timer = setTimeout(
      () => {
        setLate({ at, budget });
      },
      Math.max(0, at + budget - Date.now()),
    );
    return () => {
      clearTimeout(timer);
    };
  }, [at, budget]);
  if (at === 0) return null;
  if (isError) return noAnswer(error) ? "late" : "failed";
  return late?.at === at && late.budget === budget ? "late" : null;
}
