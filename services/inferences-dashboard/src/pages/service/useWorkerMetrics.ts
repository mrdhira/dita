import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { readWorkerMetrics } from "../../lib/metrics";
import { usePollInterval } from "../../lib/visibility";

export function useWorkerMetrics(service: string) {
  const interval = usePollInterval(30_000);
  const query = useQuery({
    queryKey: ["metrics", service],
    queryFn: () => api.metrics(service),
    select: readWorkerMetrics,
    refetchInterval: interval,
  });
  return { query, polling: interval !== false };
}
