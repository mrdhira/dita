package inferences

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// upstream is a fake worker: it records exactly what arrived and answers with a fixed
// status, headers and body.
type upstream struct {
	mu       sync.Mutex
	method   string
	path     string
	query    string
	body     []byte
	length   int64
	status   int
	headers  map[string]string
	response []byte
	block    chan struct{}
}

func (u *upstream) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	u.mu.Lock()
	u.method, u.path, u.query, u.body, u.length = r.Method, r.URL.Path, r.URL.RawQuery, body, r.ContentLength
	u.mu.Unlock()
	if u.block != nil {
		<-u.block
	}
	for k, v := range u.headers {
		w.Header().Set(k, v)
	}
	w.WriteHeader(u.status)
	w.Write(u.response)
}

func start(t *testing.T, h http.Handler) *url.URL {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL)
	return u
}

// closedURL is a port that was listening a moment ago and is not any more.
func closedURL(t *testing.T) *url.URL {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	u, _ := url.Parse("http://" + l.Addr().String())
	l.Close()
	return u
}

func gatewayFor(t *testing.T, timeout time.Duration, workers map[string]*url.URL) http.Handler {
	t.Helper()
	cfg := Config{Timeout: timeout, ProbeTimeout: time.Second}
	for _, name := range []string{Embedding, Reranker, SystemOne} {
		if u, ok := workers[name]; ok {
			cfg.Workers = append(cfg.Workers, Worker{Name: name, URL: u})
		}
	}
	g := New(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/inferences/embed", g.Embed)
	mux.HandleFunc("POST /api/inferences/rerank", g.Rerank)
	mux.HandleFunc("POST /api/inferences/decide", g.Decide)
	mux.HandleFunc("GET /api/inferences/workers", g.Workers)
	mux.HandleFunc("GET /api/inferences/health", g.Health)
	mux.HandleFunc("GET /api/inferences/metrics/{service}", g.Metrics)
	return mux
}

func do(t *testing.T, h http.Handler, method, path string, body []byte) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestPassThrough(t *testing.T) {
	// Deliberately not canonical JSON: odd spacing, key order, escapes and UTF-8, so a
	// decode-and-re-encode anywhere on the path would change the bytes.
	request := []byte("{\"inputs\" :[ \"日本語\\u00e9\",  \"x\"],\"truncate\":true }\n")
	cases := []struct {
		name, route, upstreamPath, worker string
		status                            int
		response                          []byte
	}{
		{"embed 200", "/api/inferences/embed", "/embed", Embedding, 200, []byte("[[0.1,-2.5e-07],[1]]")},
		{"rerank 200", "/api/inferences/rerank", "/rerank", Reranker, 200, []byte(`[{"index":1,"score":0.99815416}]`)},
		{"decide 200", "/api/inferences/decide", "/decide", SystemOne, 200, []byte(`{"decision":"x"}`)},
		{"a 400 stays a 400", "/api/inferences/embed", "/embed", Embedding, 400, []byte(`{"error":"` + "`inputs` cannot be empty" + `","error_type":"Empty"}`)},
		{"a 422 stays a 422", "/api/inferences/rerank", "/rerank", Reranker, 422, []byte(`{"error":"batch size 33 > maximum allowed batch size 32","error_type":"Validation"}`)},
		{"a worker's own 503 stays its own", "/api/inferences/embed", "/embed", Embedding, 503, []byte(`{"error":"no model is loaded","error_type":"Unhealthy"}`)},
		{"a 413 stays a 413", "/api/inferences/embed", "/embed", Embedding, 413, []byte("body too big")},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			up := &upstream{status: c.status, response: c.response,
				headers: map[string]string{"Content-Type": "application/json", "x-model-id": "m1", "Retry-After": "1"}}
			gw := gatewayFor(t, time.Minute, map[string]*url.URL{c.worker: start(t, up)})

			rec := do(t, gw, http.MethodPost, c.route, request)

			if rec.Code != c.status {
				t.Fatalf("status %d, want the worker's %d", rec.Code, c.status)
			}
			if !bytes.Equal(rec.Body.Bytes(), c.response) {
				t.Fatalf("body %q, want the worker's bytes %q", rec.Body.Bytes(), c.response)
			}
			if !bytes.Equal(up.body, request) {
				t.Fatalf("the worker received %q, want the caller's bytes %q", up.body, request)
			}
			if up.path != c.upstreamPath {
				t.Fatalf("the worker was asked for %q, want %q", up.path, c.upstreamPath)
			}
			for _, h := range []string{"Content-Type", "X-Model-Id", "Retry-After"} {
				if rec.Header().Get(h) == "" {
					t.Fatalf("header %s was dropped", h)
				}
			}
		})
	}
}

func TestABodyWithoutALengthReachesTheWorkerWithOne(t *testing.T) {
	up := &upstream{status: 200, response: []byte("[]")}
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Embedding: start(t, up)})
	req := httptest.NewRequest(http.MethodPost, "/api/inferences/embed", io.MultiReader(strings.NewReader(`{"inputs":`), strings.NewReader(`["a"]}`)))
	req.ContentLength = -1
	rec := httptest.NewRecorder()
	gw.ServeHTTP(rec, req)

	if rec.Code != 200 || string(up.body) != `{"inputs":["a"]}` {
		t.Fatalf("got %d, worker saw %q", rec.Code, up.body)
	}
	if up.length != int64(len(`{"inputs":["a"]}`)) {
		t.Fatalf("the worker saw Content-Length %d; it refuses a body without one", up.length)
	}
}

func TestAWorkerThatDoesNotAnswer(t *testing.T) {
	blocked := &upstream{status: 200, block: make(chan struct{})}
	slow := start(t, blocked)
	// Registered after the server's Close, so it runs first: Close waits for the handler.
	t.Cleanup(func() { close(blocked.block) })
	cases := []struct {
		name, route, worker string
		url                 *url.URL
		status              int
		reason, mentions    string
	}{
		{"a port nobody listens on", "/api/inferences/embed", Embedding, closedURL(t), 503, "not_running", "inferences-embedding is not running"},
		{"a name nothing answers to", "/api/inferences/decide", SystemOne, mustURL("http://inferences-system-one.invalid:8080"), 503, "not_running", "inferences-system-one is not running"},
		{"a worker slower than the timeout", "/api/inferences/rerank", Reranker, slow, 504, "timeout", "did not answer within"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			gw := gatewayFor(t, 200*time.Millisecond, map[string]*url.URL{c.worker: c.url})
			// The gateway's own timeout must end this, not the test binary's ten minutes.
			done := make(chan *httptest.ResponseRecorder, 1)
			go func() { done <- do(t, gw, http.MethodPost, c.route, []byte(`{}`)) }()
			var rec *httptest.ResponseRecorder
			select {
			case rec = <-done:
			case <-time.After(10 * time.Second):
				t.Fatal("the gateway never answered: nothing bounded the wait on the worker")
			}

			if rec.Code != c.status {
				t.Fatalf("status %d, want %d", rec.Code, c.status)
			}
			var got failure
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
				t.Fatalf("body %q is not JSON: %v", rec.Body.String(), err)
			}
			if got.Worker != c.worker || got.Reason != c.reason || got.ErrorType != "Unhealthy" {
				t.Fatalf("got %+v, want worker %s reason %s", got, c.worker, c.reason)
			}
			if !strings.Contains(got.Error, c.mentions) {
				t.Fatalf("error %q does not say %q", got.Error, c.mentions)
			}
			if body := rec.Body.String(); strings.Contains(body, c.url.Host) || strings.Contains(body, "http://") {
				t.Fatalf("the answer leaks the worker's address: %s", body)
			}
		})
	}
}

func TestAWorkerThatHangsUpWithoutAnsweringIsBusyNotDown(t *testing.T) {
	// What a worker at its connection cap does: accept, then close with nothing sent.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}
	}()
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Embedding: mustURL("http://" + l.Addr().String())})
	rec := do(t, gw, http.MethodPost, "/api/inferences/embed", []byte(`{}`))

	var got failure
	json.Unmarshal(rec.Body.Bytes(), &got)
	if rec.Code != 503 || got.Reason != "busy" {
		t.Fatalf("got %d %+v, want 503 busy", rec.Code, got)
	}
}

func TestWorkersReportsEachWorkersState(t *testing.T) {
	info := []byte(`{"model_id": "qwen3-embedding-0.6b", "dimensions": 1024}`)
	ready := http.NewServeMux()
	ready.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {})
	ready.HandleFunc("GET /info", func(w http.ResponseWriter, r *http.Request) { w.Write(info) })
	empty := http.NewServeMux()
	empty.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	empty.HandleFunc("GET /info", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })

	gw := gatewayFor(t, time.Minute, map[string]*url.URL{
		Embedding: start(t, ready),
		Reranker:  start(t, empty),
		SystemOne: mustURL("http://inferences-system-one.invalid:8080"),
	})
	rec := do(t, gw, http.MethodGet, "/api/inferences/workers", nil)
	if rec.Code != 200 {
		t.Fatalf("status %d: the report itself never fails", rec.Code)
	}
	var doc struct {
		Workers []report `json:"workers"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	want := []struct {
		name, state string
		health      *int
		info        string
	}{
		{Embedding, "ready", intPtr(200), compact(t, info)},
		{Reranker, "no_model", intPtr(503), "null"},
		{SystemOne, "not_running", nil, "null"},
	}
	if len(doc.Workers) != len(want) {
		t.Fatalf("got %d workers, want %d", len(doc.Workers), len(want))
	}
	for i, w := range want {
		t.Run(w.name, func(t *testing.T) {
			got := doc.Workers[i]
			if got.Name != w.name || got.State != w.state {
				t.Fatalf("got %s %s, want %s %s", got.Name, got.State, w.name, w.state)
			}
			if (got.Health == nil) != (w.health == nil) || (got.Health != nil && *got.Health != *w.health) {
				t.Fatalf("health %v, want %v", got.Health, w.health)
			}
			if string(got.Info) != w.info {
				t.Fatalf("info %s, want the worker's own %s", got.Info, w.info)
			}
			if w.state == "not_running" && !strings.Contains(got.Error, "is not running") {
				t.Fatalf("error %q does not say it is not running", got.Error)
			}
		})
	}
}

func TestClassify(t *testing.T) {
	refused := &net.OpError{Op: "dial", Err: &os.SyscallError{Syscall: "connect", Err: syscall.ECONNREFUSED}}
	cases := []struct {
		name   string
		err    error
		status int
		reason string
	}{
		{"NXDOMAIN", &net.DNSError{Err: "no such host", Name: "x", IsNotFound: true}, 503, "not_running"},
		{"Docker's SERVFAIL for a missing container", &net.DNSError{Err: "server misbehaving", Name: "x", IsTemporary: true}, 503, "not_running"},
		{"connection refused", refused, 503, "not_running"},
		{"closed before answering", errors.New("http: server closed idle connection"), 503, "busy"},
		{"reset", &net.OpError{Op: "read", Err: syscall.ECONNRESET}, 503, "busy"},
		{"EOF", io.EOF, 503, "busy"},
		{"deadline", context.DeadlineExceeded, 504, "timeout"},
		{"anything else", errors.New("tls: bad certificate"), 503, "unreachable"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			status, reason := classify(context.Background(), c.err)
			if status != c.status || reason != c.reason {
				t.Fatalf("got %d %s, want %d %s", status, reason, c.status, c.reason)
			}
		})
	}
}

func TestHealthIsTheGatewaysOwn(t *testing.T) {
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Embedding: closedURL(t)})
	rec := do(t, gw, http.MethodGet, "/api/inferences/health", nil)
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != `{"status":"ok"}` {
		t.Fatalf("got %d %q", rec.Code, rec.Body.String())
	}
}

func TestTheAPITokenIsOptInAndRefusedWhenWeak(t *testing.T) {
	strong := strings.Repeat("a1", 16)
	cases := []struct{ name, raw, want, err string }{
		{"unset leaves the routes open", "", "", ""},
		{"a 32-character token", strong, strong, ""},
		{"a short token", "secret", "", "at least 32"},
		{"a token with a trailing newline", strong + "\n", "", "surrounding space"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg, err := ConfigFromEnv(func(k string) string {
				if k == "INFERENCES_API_TOKEN" {
					return c.raw
				}
				return ""
			})
			if c.err != "" {
				if err == nil || !strings.Contains(err.Error(), c.err) {
					t.Fatalf("err %v, want one mentioning %q", err, c.err)
				}
				return
			}
			if err != nil || cfg.APIToken != c.want {
				t.Fatalf("token %q, err %v; want %q", cfg.APIToken, err, c.want)
			}
		})
	}
}

func TestConfigFromEnv(t *testing.T) {
	cases := []struct {
		name    string
		env     map[string]string
		timeout time.Duration
		urls    []string
		err     string
	}{
		{"defaults", nil, 120 * time.Second,
			[]string{"http://inferences-embedding:8080", "http://inferences-reranker:8080", "http://inferences-system-one:8080"}, ""},
		{"compose re-points a worker and the timeout",
			map[string]string{"INFERENCES_RERANKER_URL": "http://127.0.0.1:18081", "INFERENCES_TIMEOUT": "90s"}, 90 * time.Second,
			[]string{"http://inferences-embedding:8080", "http://127.0.0.1:18081", "http://inferences-system-one:8080"}, ""},
		{"a URL with no scheme", map[string]string{"INFERENCES_EMBEDDING_URL": "inferences-embedding:8080"}, 0, nil, "INFERENCES_EMBEDDING_URL"},
		{"a timeout with no unit", map[string]string{"INFERENCES_TIMEOUT": "120"}, 0, nil, "INFERENCES_TIMEOUT"},
		{"a zero timeout", map[string]string{"INFERENCES_TIMEOUT": "0s"}, 0, nil, "not positive"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg, err := ConfigFromEnv(func(k string) string { return c.env[k] })
			if c.err != "" {
				if err == nil || !strings.Contains(err.Error(), c.err) {
					t.Fatalf("err %v, want one mentioning %q", err, c.err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if cfg.Timeout != c.timeout {
				t.Fatalf("timeout %s, want %s", cfg.Timeout, c.timeout)
			}
			for i, u := range c.urls {
				if cfg.Workers[i].URL.String() != u {
					t.Fatalf("worker %d at %s, want %s", i, cfg.Workers[i].URL, u)
				}
			}
			if cfg.ServerWriteTimeout() <= cfg.Timeout {
				t.Fatalf("server write timeout %s would cut off a proxied answer at %s", cfg.ServerWriteTimeout(), cfg.Timeout)
			}
		})
	}
}

func mustURL(raw string) *url.URL {
	u, err := url.Parse(raw)
	if err != nil {
		panic(err)
	}
	return u
}

func intPtr(v int) *int { return &v }

func compact(t *testing.T, raw []byte) string {
	t.Helper()
	var out bytes.Buffer
	if err := json.Compact(&out, raw); err != nil {
		t.Fatal(err)
	}
	return out.String()
}

func TestTheProxyTimeoutMustOutlastTheWorkersDeadline(t *testing.T) {
	info := func(body string) *url.URL {
		return start(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/info" {
				w.Write([]byte(body))
			}
		}))
	}
	cases := []struct {
		name     string
		timeout  time.Duration
		worker   *url.URL
		checked  bool
		mentions string
	}{
		{"120 s over the worker's 100 s", 120 * time.Second, info(`{"deadline_s": 100.0}`), true, ""},
		{"90 s under it", 90 * time.Second, info(`{"deadline_s": 100.0}`), true, "does not outlast"},
		{"exactly equal", 100 * time.Second, info(`{"deadline_s": 100.0}`), true, "does not outlast"},
		{"a worker that publishes no deadline", 90 * time.Second, info(`{}`), false, ""},
		{"a worker not up yet", 90 * time.Second, closedURL(t), false, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			g := New(Config{Timeout: c.timeout, ProbeTimeout: time.Second, Workers: []Worker{{Name: SystemOne, URL: c.worker}}},
				slog.New(slog.NewTextHandler(io.Discard, nil)))
			checked, err := g.CheckWorkerDeadline(context.Background())
			if checked != c.checked || (c.mentions == "") != (err == nil) || (err != nil && !strings.Contains(err.Error(), c.mentions)) {
				t.Fatalf("checked %v, err %v; want checked %v mentioning %q", checked, err, c.checked, c.mentions)
			}
		})
	}
}

func TestABodyWithoutALengthIsBufferedOnlyUpToTheCap(t *testing.T) {
	for _, c := range []struct {
		name   string
		size   int
		status int
	}{{"exactly the cap", maxBufferedBody, 200}, {"one byte over", maxBufferedBody + 1, 413}} {
		t.Run(c.name, func(t *testing.T) {
			up := &upstream{status: 200}
			gw := gatewayFor(t, time.Minute, map[string]*url.URL{Embedding: start(t, up)})
			req := httptest.NewRequest(http.MethodPost, "/api/inferences/embed", bytes.NewReader(bytes.Repeat([]byte("x"), c.size)))
			req.ContentLength = -1
			rec := httptest.NewRecorder()
			gw.ServeHTTP(rec, req)
			up.mu.Lock()
			reached := up.body != nil
			up.mu.Unlock()
			if rec.Code != c.status || reached != (c.status == 200) {
				t.Fatalf("%d bytes without a length: %d, worker reached %v", c.size, rec.Code, reached)
			}
		})
	}
}

func TestMetricsPassesTheWorkersPageThroughVerbatim(t *testing.T) {
	page := []byte("# HELP dita_worker_ops_total DIP requests handled.\n# TYPE dita_worker_ops_total counter\n" +
		"dita_worker_ops_total{worker=\"inferences-reranker\",op=\"infer\",outcome=\"ok\"} 24\n" +
		"dita_worker_infer_duration_seconds_bucket{le=\"+Inf\"}   24\n")
	cases := []struct{ service, worker string }{
		{"embedding", Embedding},
		{"reranker", Reranker},
		{"system-one", SystemOne},
	}
	for _, c := range cases {
		t.Run(c.service, func(t *testing.T) {
			up := &upstream{status: 200, response: page, headers: map[string]string{"Content-Type": "text/plain"}}
			gw := gatewayFor(t, time.Minute, map[string]*url.URL{c.worker: start(t, up)})

			rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/"+c.service+"?path=/info", nil)

			if rec.Code != 200 || !bytes.Equal(rec.Body.Bytes(), page) {
				t.Fatalf("got %d %q, want the worker's bytes %q", rec.Code, rec.Body.Bytes(), page)
			}
			if got := rec.Header().Get("Content-Type"); got != MetricsContentType {
				t.Fatalf("content type %q, want %q", got, MetricsContentType)
			}
			if up.method != http.MethodGet || up.path != "/metrics" || up.query != "" {
				t.Fatalf("the worker was asked %s %q ?%q; only GET /metrics is reachable", up.method, up.path, up.query)
			}
		})
	}
}

func TestMetricsForAServiceItDoesNotKnowIsTheOneErrorShape(t *testing.T) {
	up := &upstream{status: 200, response: []byte("x 1\n")}
	u := start(t, up)
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Embedding: u, Reranker: u, SystemOne: u})
	for _, service := range []string{"ocr", "inferences-embedding", "embed", "%2e%2e%2finfo"} {
		t.Run(service, func(t *testing.T) {
			rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/"+service, nil)
			var body map[string]string
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != 404 ||
				body["error_type"] != "NotFound" || body["error"] == "" || len(body) != 2 {
				t.Fatalf("got %d %q (%v), want 404 {error, error_type: NotFound}", rec.Code, rec.Body, err)
			}
			if got := rec.Header().Get("Content-Type"); got != "application/json" {
				t.Fatalf("content type %q", got)
			}
		})
	}
	if up.path != "" {
		t.Fatalf("an unknown service reached a worker at %q", up.path)
	}
}

func TestMetricsFromAWorkerThatIsDown(t *testing.T) {
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Reranker: closedURL(t)})
	rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/reranker", nil)
	var got failure
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || rec.Code != 503 ||
		got.Reason != "not_running" || got.ErrorType != "Unhealthy" || got.Worker != Reranker {
		t.Fatalf("got %d %q (%v), want 503 not_running", rec.Code, rec.Body, err)
	}
}

func TestMetricsKeepsAWorkersFailureAFailure(t *testing.T) {
	sendError := "<!DOCTYPE HTML>\n<html><body><h1>Error response</h1><p>Error code: 404</p></body></html>\n"
	own := `{"error":"no model is loaded","error_type":"Unhealthy"}`
	cases := []struct {
		name        string
		status      int
		contentType string
		body        string
		verbatim    bool
	}{
		{"404: the worker's send_error HTML page, as JSON naming the worker", 404, "text/html;charset=utf-8", sendError, false},
		{"500 as plain text, as JSON naming the worker", 500, "text/plain; charset=utf-8", "boom", false},
		{"500 with an empty body, as JSON naming the worker", 500, "", "", false},
		{"502: JSON that is not an error object, as JSON naming the worker", 502, "application/json", `{"status":"down"}`, false},
		{"503: the worker's own JSON error, unchanged", 503, "application/json", own, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			up := &upstream{status: c.status, response: []byte(c.body), headers: map[string]string{"Content-Type": c.contentType}}
			gw := gatewayFor(t, time.Minute, map[string]*url.URL{Reranker: start(t, up)})
			rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/reranker", nil)
			if rec.Code != c.status {
				t.Fatalf("status %d, want the worker's %d", rec.Code, c.status)
			}
			if got := rec.Header().Get("Content-Type"); got == MetricsContentType || !strings.HasPrefix(got, "application/json") {
				t.Fatalf("content type %q, want JSON and never %q", got, MetricsContentType)
			}
			if c.verbatim {
				if rec.Body.String() != c.body {
					t.Fatalf("got %q, want the worker's own error %q unchanged", rec.Body, c.body)
				}
				return
			}
			var got failure
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got.ErrorType != "Backend" || got.Worker != Reranker ||
				!strings.Contains(got.Error, fmt.Sprintf("answered %d", c.status)) || strings.Contains(rec.Body.String(), "<") {
				t.Fatalf("got %q (%v), want the orchestrator's JSON naming %s and its %d, with none of the page", rec.Body, err, Reranker, c.status)
			}
		})
	}
}

func TestMetricsRefusesAPageOverTheBound(t *testing.T) {
	cases := []struct {
		name string
		size int
		want int
	}{
		{"at the bound", maxMetricsBody, 200},
		{"one byte over", maxMetricsBody + 1, 502},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			page := bytes.Repeat([]byte("x"), c.size)
			up := &upstream{status: 200, response: page}
			gw := gatewayFor(t, time.Minute, map[string]*url.URL{Reranker: start(t, up)})
			rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/reranker", nil)
			if rec.Code != c.want {
				t.Fatalf("status %d for a %d-byte page, want %d", rec.Code, c.size, c.want)
			}
			if c.want == 200 {
				if rec.Body.Len() != c.size {
					t.Fatalf("served %d of %d bytes", rec.Body.Len(), c.size)
				}
				return
			}
			var got failure
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got.Reason != "too_large" ||
				got.Worker != Reranker || !strings.Contains(got.Error, "exceeds") {
				t.Fatalf("got %q (%v), want a too_large refusal naming the worker", rec.Body, err)
			}
		})
	}
}

func TestMetricsStopsReadingAnEndlessPageAtTheBound(t *testing.T) {
	endless := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chunk := bytes.Repeat([]byte("x"), 32<<10)
		for {
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	})
	gw := gatewayFor(t, time.Minute, map[string]*url.URL{Reranker: start(t, endless)})
	rec := do(t, gw, http.MethodGet, "/api/inferences/metrics/reranker", nil)
	var got failure
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || rec.Code != 502 || got.Reason != "too_large" {
		t.Fatalf("got %d %q (%v), want 502 too_large: reading must stop at the bound, not run to the probe timeout", rec.Code, rec.Body, err)
	}
}
