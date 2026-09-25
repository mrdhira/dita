package inferences

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
)

// MetricsContentType is the Prometheus text exposition format every worker's /metrics speaks.
const MetricsContentType = "text/plain; version=0.0.4; charset=utf-8"

// maxMetricsBody bounds what a scrape may buffer; a worker's page is a few kilobytes. A page
// over it is refused rather than truncated, since a cut page still parses as a complete one.
const maxMetricsBody = 1 << 20

var errMetricsTooLarge = fmt.Errorf("the /metrics page exceeds %d bytes", maxMetricsBody)

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
	if errors.Is(err, errMetricsTooLarge) {
		writeJSON(w, http.StatusBadGateway, failure{Error: worker.Name + ": " + err.Error(), ErrorType: "Unhealthy", Worker: worker.Name, Reason: "too_large"})
		return
	}
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
	target.RawQuery = ""
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return 0, "", nil, err
	}
	resp, err := g.probe.Do(req)
	if err != nil {
		return 0, "", nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetricsBody+1))
	if err != nil {
		return 0, "", nil, err
	}
	if len(body) > maxMetricsBody {
		return 0, "", nil, errMetricsTooLarge
	}
	return resp.StatusCode, resp.Header.Get("Content-Type"), body, nil
}
