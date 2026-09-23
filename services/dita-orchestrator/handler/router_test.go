package handler

import (
	"dita-orchestrator/decisions"
	"dita-orchestrator/handler/chat"
	decisionsHandler "dita-orchestrator/handler/decisions"
	"dita-orchestrator/handler/inferences"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

// The real router: the gateway's routes are mounted where the dashboard expects them, and
// the server's write deadline outlasts the gateway's timeout.
func TestTheGatewayIsMountedUnderOnePrefix(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(r.Method + " " + r.URL.Path))
	}))
	t.Cleanup(up.Close)
	base, _ := url.Parse(up.URL)
	cfg := inferences.Config{Timeout: 120 * time.Second, ProbeTimeout: time.Second}
	for _, name := range []string{inferences.Embedding, inferences.Reranker, inferences.SystemOne} {
		cfg.Workers = append(cfg.Workers, inferences.Worker{Name: name, URL: base})
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store, err := decisions.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	decide := decisionsHandler.New(store, cfg.Workers[2], cfg.Timeout, log)
	srv := NewRouter(log, ":0", chat.New("unused", log), inferences.New(cfg, log), decide, cfg.ServerWriteTimeout())

	cases := []struct{ method, path, upstream string }{
		{"POST", "/api/inferences/embed", "POST /embed"},
		{"POST", "/api/inferences/rerank", "POST /rerank"},
		{"POST", "/api/inferences/decide", "POST /decide"},
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
			if rec.Code != 200 || rec.Body.String() != c.upstream {
				t.Fatalf("got %d %q, want the worker to see %q", rec.Code, rec.Body.String(), c.upstream)
			}
		})
	}
	for _, path := range []string{"/api/inferences/workers", "/api/inferences/health", "/api/inferences/schemas",
		"/api/inferences/decisions", "/api/inferences/evaluations", "/api/inferences/stats"} {
		rec := httptest.NewRecorder()
		srv.ServeHTTP(rec, httptest.NewRequest("GET", path, nil))
		if rec.Code != 200 {
			t.Fatalf("GET %s: %d", path, rec.Code)
		}
	}
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, httptest.NewRequest("GET", "/api/inferences/embed", nil))
	if rec.Code == 200 {
		t.Fatal("GET /api/inferences/embed was proxied; only POST should be")
	}
	if got := srv.HTTPServer().WriteTimeout; got <= cfg.Timeout {
		t.Fatalf("server WriteTimeout %s would cut a %s proxied answer short", got, cfg.Timeout)
	}
}
