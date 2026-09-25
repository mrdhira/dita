import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { shouldRetry } from "../../lib/errors";
import { readWorkerMetrics } from "../../lib/metrics";
import { useStaleness } from "../../lib/stale";
import { usePollInterval } from "../../lib/visibility";

const retry = (n: number, e: Error) =>
  !(e instanceof ApiError && e.problem.error_type === "NotMetrics") && shouldRetry(n, e);

export function useWorkerMetrics(service: string) {
  const interval = usePollInterval(30_000);
  const query = useQuery({
    queryKey: ["metrics", service],
    queryFn: async ({ signal }) => readWorkerMetrics(await api.metrics(service, signal)),
    refetchInterval: interval,
    retry,
  });
  return { query, polling: interval !== false, stale: useStaleness(query, interval) };
}
