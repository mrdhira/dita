import { useEffect, useState } from "react";
import { READ_DEADLINE_MS } from "../api/client";

/** Why the answer on screen is not current: its refresh failed, or it has gone unanswered. */
export type Staleness = "failed" | "late" | null;

/** A hung refresh never fails, so age marks it too: one missed poll plus a whole read deadline. */
export function useStaleness(
  query: { dataUpdatedAt: number; isError: boolean },
  interval: number | false,
): Staleness {
  const { dataUpdatedAt: at, isError } = query;
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
  if (isError) return "failed";
  return late?.at === at && late.budget === budget ? "late" : null;
}
