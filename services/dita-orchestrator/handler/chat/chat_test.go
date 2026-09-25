package chat

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// deepseek stands in for the API and counts what reaches it, so a refusal is shown to happen
// before any outbound call and the accepted case shows the count can move.
func deepseek(t *testing.T, key string) (*ChatHandler, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Write([]byte(`{"choices":[]}`))
	}))
	t.Cleanup(srv.Close)
	h := New(key, slog.New(slog.NewTextHandler(io.Discard, nil)))
	h.baseURL = srv.URL
	return h, &calls
}

func post(h *ChatHandler, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.Chat(rec, httptest.NewRequest("POST", "/api/v1/chat", strings.NewReader(body)))
	return rec
}

// A key-shaped value that is not a key: built here so no literal in the repo looks like one.
var fakeKey = "sk-" + strings.Repeat("0", 32)

func TestAnEmptyBodyNeverReachesDeepSeek(t *testing.T) {
	for _, body := range []string{"", " \n\t "} {
		h, calls := deepseek(t, fakeKey)
		if rec := post(h, body); rec.Code != http.StatusBadRequest || calls.Load() != 0 {
			t.Fatalf("body %q: %d, %d calls to DeepSeek", body, rec.Code, calls.Load())
		}
	}
	h, calls := deepseek(t, fakeKey)
	if rec := post(h, `{"message":"hello"}`); rec.Code != http.StatusOK || calls.Load() != 1 {
		t.Fatalf("a real message: %d, %d calls; the stand-in must be reachable for the refusals to mean anything", rec.Code, calls.Load())
	}
}

func TestAMissingOrPlaceholderKeyIsNeverSent(t *testing.T) {
	for _, key := range []string{"", "   ", "unused", "changeme", "your-api-key", "sk-xxxxxxxx", "PLACEHOLDER", fakeKey + "\n"} {
		h, calls := deepseek(t, key)
		rec := post(h, `{"message":"hello"}`)
		if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "DEEPSEEK_API_KEY") || calls.Load() != 0 {
			t.Fatalf("key %q: %d %q, %d calls to DeepSeek", key, rec.Code, rec.Body, calls.Load())
		}
	}
}

func TestEveryRefusalAnswersInTheOneErrorShape(t *testing.T) {
	unreachable := func(t *testing.T) *ChatHandler {
		h := New(fakeKey, slog.New(slog.NewTextHandler(io.Discard, nil)))
		h.baseURL = "http://127.0.0.1:1"
		return h
	}
	configured := func(t *testing.T) *ChatHandler { h, _ := deepseek(t, fakeKey); return h }
	cases := []struct {
		name      string
		handler   func(*testing.T) *ChatHandler
		body      string
		status    int
		errorType string
	}{
		{"no key", func(t *testing.T) *ChatHandler { h, _ := deepseek(t, ""); return h }, `{"message":"hello"}`, 503, "Unhealthy"},
		{"an empty body", configured, "", 400, "Validation"},
		{"malformed JSON", configured, "{", 400, "Validation"},
		{"DeepSeek unreachable", unreachable, `{"message":"hello"}`, 500, "Backend"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := post(c.handler(t), c.body)
			var body map[string]string
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != c.status ||
				body["error_type"] != c.errorType || body["error"] == "" || rec.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("got %d %q %q (%v), want %d %s", rec.Code, rec.Header().Get("Content-Type"), rec.Body, err, c.status, c.errorType)
			}
		})
	}
}
