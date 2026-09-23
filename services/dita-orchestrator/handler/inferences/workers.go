package inferences

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sync"
)

// report is one worker's state as the gateway saw it just now.
type report struct {
	Name   string          `json:"name"`
	URL    string          `json:"url"`
	State  string          `json:"state"`
	Health *int            `json:"health_status"`
	Info   json.RawMessage `json:"info"`
	Error  string          `json:"error,omitempty"`
}

// Workers aggregates every worker's /health and /info into one document; `info` is the
// worker's own /info, unchanged but for whitespace. It always answers
// 200: the document is the report, and "not_running" is a finding, not a failure.
//
// States: ready (a model is resident), no_model (up, nothing resident), unhealthy (any
// other /health status), and not_running, busy, timeout or unreachable when /health got no
// answer at all.
func (g *Gateway) Workers(w http.ResponseWriter, r *http.Request) {
	reports := make([]report, len(g.order))
	var wg sync.WaitGroup
	for i, worker := range g.order {
		wg.Add(1)
		go func() {
			defer wg.Done()
			reports[i] = g.inspect(r.Context(), worker)
		}()
	}
	wg.Wait()
	writeJSON(w, http.StatusOK, map[string]any{"workers": reports})
}

func (g *Gateway) inspect(ctx context.Context, worker Worker) report {
	rep := report{Name: worker.Name, URL: worker.URL.String(), Info: json.RawMessage("null")}
	status, _, err := g.get(ctx, worker, "/health")
	if err != nil {
		_, rep.State = classify(ctx, err)
		rep.Error = describe(worker, rep.State, g.cfg.ProbeTimeout, err)
		return rep
	}
	rep.Health = &status
	switch status {
	case http.StatusOK:
		rep.State = "ready"
	case http.StatusServiceUnavailable:
		rep.State = "no_model"
	default:
		rep.State = "unhealthy"
	}
	if status, body, err := g.get(ctx, worker, "/info"); err == nil && status == http.StatusOK && json.Valid(body) {
		rep.Info = body
	}
	return rep
}

func (g *Gateway) get(ctx context.Context, worker Worker, path string) (int, []byte, error) {
	target := *worker.URL
	target.Path = worker.URL.Path + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return 0, nil, err
	}
	resp, err := g.probe.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return 0, nil, err
	}
	return resp.StatusCode, body, nil
}
