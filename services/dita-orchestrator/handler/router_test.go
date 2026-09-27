package handler

import (
	"dita-orchestrator/decisions"
	"dita-orchestrator/handler/chat"
	decisionsHandler "dita-orchestrator/handler/decisions"
	"dita-orchestrator/handler/inferences"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
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
	srv := NewRouter(log, ":0", chat.New("unused", log), inferences.New(cfg, log), decide, cfg.ServerWriteTimeout(), "")

	cases := []struct{ method, path, upstream string }{
		{"POST", "/api/inferences/embed", "POST /embed"},
		{"POST", "/api/inferences/rerank", "POST /rerank"},
		{"POST", "/api/inferences/decide", "POST /decide"},
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(c.method, c.path, nil)
			req.Header.Set("Content-Type", jsonType)
			srv.ServeHTTP(rec, req)
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
	srv.ServeHTTP(rec, httptest.NewRequest("GET", "/api/inferences/metrics/system-one", nil))
	if rec.Code != 200 || rec.Body.String() != "GET /metrics" {
		t.Fatalf("GET metrics: %d %q, want the worker to see GET /metrics", rec.Code, rec.Body)
	}
	rec = httptest.NewRecorder()
	srv.ServeHTTP(rec, httptest.NewRequest("GET", "/api/inferences/embed", nil))
	if rec.Code == 200 {
		t.Fatal("GET /api/inferences/embed was proxied; only POST should be")
	}
	if got := srv.HTTPServer().WriteTimeout; got <= cfg.Timeout {
		t.Fatalf("server WriteTimeout %s would cut a %s proxied answer short", got, cfg.Timeout)
	}
}

type routerCall func(method, path, body string, header map[string]string) *httptest.ResponseRecorder

// serve is the real router over a fresh store, with every worker at a port nothing listens on.
func serve(t *testing.T, token string, store *decisions.Store) routerCall {
	t.Helper()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := inferences.Config{Timeout: time.Second, ProbeTimeout: time.Second}
	for _, name := range []string{inferences.Embedding, inferences.Reranker, inferences.SystemOne} {
		cfg.Workers = append(cfg.Workers, inferences.Worker{Name: name, URL: &url.URL{Scheme: "http", Host: "127.0.0.1:1"}})
	}
	decide := decisionsHandler.New(store, cfg.Workers[2], cfg.Timeout, log)
	srv := NewRouter(log, ":0", chat.New("unused", log), inferences.New(cfg, log), decide, cfg.ServerWriteTimeout(), token)
	return func(method, path, body string, header map[string]string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		for k, v := range header {
			req.Header.Set(k, v)
		}
		rec := httptest.NewRecorder()
		srv.ServeHTTP(rec, req)
		return rec
	}
}

func openStore(t *testing.T) *decisions.Store {
	t.Helper()
	store, err := decisions.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return store
}

// Every POST the router serves; a GET-only route is not a write.
var writes = []struct{ path, body string }{
	{"/api/inferences/schemas", `{"name":"alert-triage","questions":[{"name":"severity","type":"choice","options":["low","high"],"criteria":"How bad?"}]}`},
	{"/api/inferences/schemas/alert-triage/versions/1/retire", ""},
	{"/api/inferences/decisions", `{"text":"x","schema":{"name":"alert-triage","version":1}}`},
	{"/api/inferences/decisions/nope/correction", `{"answers":{}}`},
	{"/api/inferences/evaluations", `{"name":"x","rows":[]}`},
	// Empty: the chat handler refuses it before any call to DeepSeek, so no test reaches the network.
	{"/api/v1/chat", ""},
	{"/api/inferences/embed", `{"inputs":"x"}`},
	{"/api/inferences/rerank", `{}`},
	{"/api/inferences/decide", `{}`},
}

const jsonType = "application/json"

func TestEveryWriteRefusesWhatABrowserSendsCrossSite(t *testing.T) {
	call := serve(t, "", openStore(t))
	cases := []struct {
		name    string
		header  map[string]string
		refused int
	}{
		{"no content type", nil, 415},
		{"text/plain, a CORS simple request", map[string]string{"Content-Type": "text/plain"}, 415},
		{"a form post", map[string]string{"Content-Type": "application/x-www-form-urlencoded"}, 415},
		{"JSON marked cross-site", map[string]string{"Content-Type": jsonType, "Sec-Fetch-Site": "cross-site"}, 403},
		{"JSON with a charset, same origin", map[string]string{"Content-Type": jsonType + "; charset=utf-8", "Sec-Fetch-Site": "same-origin"}, 0},
	}
	for _, w := range writes {
		for _, c := range cases {
			t.Run(w.path+"/"+c.name, func(t *testing.T) {
				rec := call("POST", w.path, w.body, c.header)
				if c.refused == 0 {
					if rec.Code == 415 || rec.Code == 403 {
						t.Fatalf("a same-origin JSON request was refused: %d %s", rec.Code, rec.Body)
					}
					return
				}
				if rec.Code != c.refused || !strings.Contains(rec.Body.String(), `"error_type"`) {
					t.Fatalf("got %d %s, want %d", rec.Code, rec.Body, c.refused)
				}
			})
		}
	}
	if rec := call("POST", writes[0].path, writes[0].body, map[string]string{"Content-Type": jsonType}); rec.Code != 201 {
		t.Fatalf("the dashboard's own request: %d %s", rec.Code, rec.Body)
	}
}

func TestTheTokenGuardsEveryWriteOnlyWhenSet(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	reads := []string{"/api/inferences/schemas", "/api/inferences/decisions", "/api/inferences/evaluations",
		"/api/inferences/stats", "/api/inferences/health", "/api/inferences/workers", "/api/v1/ping"}

	t.Run("unset: no write asks for one", func(t *testing.T) {
		call := serve(t, "", openStore(t))
		for _, w := range writes {
			if rec := call("POST", w.path, w.body, map[string]string{"Content-Type": jsonType}); rec.Code == 401 {
				t.Fatalf("POST %s refused with no token configured", w.path)
			}
		}
	})

	t.Run("set: every write needs the right token, a read does not", func(t *testing.T) {
		call := serve(t, token, openStore(t))
		for _, w := range writes {
			for _, c := range []struct{ name, value, mentions string }{
				{"missing", "", "needs the X-Inferences-Token header"},
				{"wrong", token[:31] + "x", "does not match"},
				{"a prefix", token[:16], "does not match"},
				{"empty", "-", "does not match"},
			} {
				header := map[string]string{"Content-Type": jsonType}
				if c.value == "-" {
					header[TokenHeader] = ""
				} else if c.value != "" {
					header[TokenHeader] = c.value
				}
				rec := call("POST", w.path, w.body, header)
				if rec.Code != 401 || !strings.Contains(rec.Body.String(), `"error_type":"Unauthorized"`) ||
					!strings.Contains(rec.Body.String(), c.mentions) {
					t.Fatalf("POST %s, token %s: %d %s", w.path, c.name, rec.Code, rec.Body)
				}
			}
		}
		with := map[string]string{"Content-Type": jsonType, TokenHeader: token}
		if rec := call("POST", writes[0].path, writes[0].body, with); rec.Code != 201 {
			t.Fatalf("saving a schema with the token: %d %s", rec.Code, rec.Body)
		}
		if rec := call("POST", writes[1].path, "", with); rec.Code != 200 {
			t.Fatalf("retiring with the token: %d %s", rec.Code, rec.Body)
		}
		for _, path := range reads {
			if rec := call("GET", path, "", nil); rec.Code != 200 {
				t.Fatalf("GET %s without a token: %d", path, rec.Code)
			}
		}
	})
}

func TestTheRoutersOwnRefusalsAnswerInTheOneErrorShape(t *testing.T) {
	call := serve(t, "", openStore(t))
	cases := []struct {
		name, method, path string
		status             int
		errorType, error   string
	}{
		{"no route", "GET", "/api/inferences/nope", 404, "NotFound", "no route for GET /api/inferences/nope"},
		{"a route, another method", "GET", "/api/inferences/embed", 405, "MethodNotAllowed", "GET is not served on /api/inferences/embed; allowed: POST"},
		{"metrics are read-only", "POST", "/api/inferences/metrics/embedding", 405, "MethodNotAllowed", "POST is not served on /api/inferences/metrics/embedding; allowed: GET, HEAD"},
		{"a handler's own 404 passes through", "GET", "/api/inferences/decisions/nope", 404, "NotFound", "not found"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := call(c.method, c.path, "", nil)
			var body map[string]string
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != c.status ||
				body["error_type"] != c.errorType || body["error"] != c.error {
				t.Fatalf("got %d %q (%v), want %d %s %q", rec.Code, rec.Body, err, c.status, c.errorType, c.error)
			}
			if got := rec.Header().Get("Content-Type"); got != jsonType {
				t.Fatalf("content type %q", got)
			}
		})
	}
	if rec := call("GET", "/api/inferences/embed", "", nil); rec.Header().Get("Allow") != "POST" {
		t.Fatalf("a 405 lost its Allow header: %v", rec.Header())
	}
	if rec := call("POST", "/api/inferences/metrics/embedding", "{}", map[string]string{"Content-Type": jsonType}); rec.Header().Get("Allow") != "GET, HEAD" {
		t.Fatalf("POST metrics: Allow %q, want GET only (HEAD is implied by GET)", rec.Header().Get("Allow"))
	}
}

func TestARecoveredPanicAnswersInTheOneErrorShape(t *testing.T) {
	call := serve(t, "", nil)
	rec := call("GET", "/api/inferences/schemas", "", nil)
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != 500 || body["error_type"] != "Backend" || body["error"] == "" {
		t.Fatalf("a panic answered %d %s (%v)", rec.Code, rec.Body, err)
	}
	if strings.Contains(rec.Header().Get("Content-Type"), "problem") {
		t.Fatalf("content type %s", rec.Header().Get("Content-Type"))
	}
}
