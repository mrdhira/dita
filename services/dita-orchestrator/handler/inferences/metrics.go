package inferences

import (
	"context"
	"io"
	"log/slog"
	"net/http"
)

// MetricsContentType is the Prometheus text exposition format every worker's /metrics speaks.
const MetricsContentType = "text/plain; version=0.0.4; charset=utf-8"

// maxMetricsBody bounds what a scrape may buffer; a worker's page is a few kilobytes.
const maxMetricsBody = 1 << 20

var metricsWorkers = map[string]string{
	"embedding":  Embedding,
	"reranker":   Reranker,
	"system-one": SystemOne,
}

// Metrics answers a worker's /metrics body verbatim. The service is one of a fixed set and the
// upstream path is always /metrics: this is not a path proxy, and nothing else is reachable
// through it.
func (g *Gateway) Metrics(w http.ResponseWriter, r *http.Request) {
	service := r.PathValue("service")
	worker, ok := g.byName[metricsWorkers[service]]
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error":      "no metrics for " + service + "; known: embedding, reranker, system-one",
			"error_type": "NotFound",
		})
		return
	}
	status, contentType, body, err := g.scrape(r.Context(), worker)
	if err != nil {
		reason := WriteUnavailable(w, r.Context(), worker, g.cfg.ProbeTimeout, err)
		g.log.WarnContext(r.Context(), "worker metrics did not answer", slog.String("worker", worker.Name),
			slog.String("url", worker.URL.String()), slog.String("reason", reason), slog.Any("err", err))
		return
	}
	if status == http.StatusOK {
		contentType = MetricsContentType
	}
	if contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func (g *Gateway) scrape(ctx context.Context, worker Worker) (int, string, []byte, error) {
	target := *worker.URL
	target.Path = worker.URL.Path + "/metrics"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return 0, "", nil, err
	}
	resp, err := g.probe.Do(req)
	if err != nil {
		return 0, "", nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetricsBody))
	if err != nil {
		return 0, "", nil, err
	}
	return resp.StatusCode, resp.Header.Get("Content-Type"), body, nil
}
