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
	"net/http/httputil"
	"strings"
	"syscall"
	"time"
)

// maxBufferedBody caps a body the gateway has to read to learn its length. The workers
// refuse a body with no Content-Length (411), so a chunked request is buffered first; one
// with a length streams through untouched and the worker applies its own cap.
const maxBufferedBody = 8 << 20

// Gateway fronts the inference workers under one prefix. It forwards bodies and returns
// statuses unchanged; it only speaks for itself when no worker answered.
type Gateway struct {
	byName map[string]Worker
	order  []Worker
	cfg    Config
	proxy  *http.Transport
	probe  *http.Client
	log    *slog.Logger
}

// New builds a Gateway from cfg.
func New(cfg Config, log *slog.Logger) *Gateway {
	g := &Gateway{byName: map[string]Worker{}, order: cfg.Workers, cfg: cfg, log: log}
	for _, w := range cfg.Workers {
		g.byName[w.Name] = w
	}
	g.proxy = &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		ResponseHeaderTimeout: cfg.Timeout,
		// Every open connection holds one of a worker's eight HTTP slots, idle or not, and
		// the worker drops an idle one after 10 s; keep few, and let them go first.
		MaxIdleConnsPerHost: 2,
		IdleConnTimeout:     5 * time.Second,
	}
	g.probe = &http.Client{
		Timeout:   cfg.ProbeTimeout,
		Transport: &http.Transport{DisableKeepAlives: true, DialContext: (&net.Dialer{Timeout: cfg.ProbeTimeout}).DialContext},
	}
	return g
}

// Embed proxies to the embedding worker's TEI /embed.
func (g *Gateway) Embed(w http.ResponseWriter, r *http.Request) { g.forward(w, r, Embedding, "/embed") }

// Rerank proxies to the reranker's TEI /rerank.
func (g *Gateway) Rerank(w http.ResponseWriter, r *http.Request) {
	g.forward(w, r, Reranker, "/rerank")
}

// Decide proxies to the system-one worker's /decide.
func (g *Gateway) Decide(w http.ResponseWriter, r *http.Request) {
	g.forward(w, r, SystemOne, "/decide")
}

// Health is the gateway's own liveness. It says nothing about any worker; GET /workers does.
func (g *Gateway) Health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (g *Gateway) forward(w http.ResponseWriter, r *http.Request, name, path string) {
	worker, ok := g.byName[name]
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, failure{Error: name + " is not configured", ErrorType: "Unhealthy", Worker: name, Reason: "not_configured"})
		return
	}
	if r.ContentLength < 0 {
		body, err := io.ReadAll(io.LimitReader(r.Body, maxBufferedBody+1))
		if err != nil {
			writeJSON(w, http.StatusBadRequest, failure{Error: "reading the request body: " + err.Error(), ErrorType: "Validation", Worker: name, Reason: "bad_body"})
			return
		}
		if len(body) > maxBufferedBody {
			writeJSON(w, http.StatusRequestEntityTooLarge, failure{Error: fmt.Sprintf("a body without Content-Length is limited to %d bytes", maxBufferedBody), ErrorType: "Validation", Worker: name, Reason: "too_large"})
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
	}
	target := *worker.URL
	target.Path = worker.URL.Path + path
	target.RawQuery = ""
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL = &target
			pr.Out.Host = target.Host
		},
		Transport: g.proxy,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			reason := WriteUnavailable(w, r.Context(), worker, g.cfg.Timeout, err)
			g.log.WarnContext(r.Context(), "inference worker did not answer",
				slog.String("worker", name), slog.String("reason", reason), slog.Any("err", err))
		},
	}
	proxy.ServeHTTP(w, r)
}

// WriteUnavailable answers for a worker that gave no response at all, the way every gateway
// route does, and returns the reason it wrote.
func WriteUnavailable(w http.ResponseWriter, ctx context.Context, worker Worker, timeout time.Duration, err error) string {
	status, reason := classify(ctx, err)
	writeJSON(w, status, failure{
		Error: describe(worker, reason, timeout, err), ErrorType: "Unhealthy",
		Worker: worker.Name, URL: worker.URL.String(), Reason: reason,
	})
	return reason
}

// failure is TEI's error body, {error, error_type}, plus which worker and why, so a TEI
// client parses it and a person can read it.
type failure struct {
	Error     string `json:"error"`
	ErrorType string `json:"error_type"`
	Worker    string `json:"worker"`
	URL       string `json:"url,omitempty"`
	Reason    string `json:"reason"`
}

// classify turns a transport failure into a status and a reason. "not_running" is only
// claimed when nothing answers to the name or the port; a connection accepted and then
// closed without a response is a worker at its connection cap, which is "busy".
func classify(ctx context.Context, err error) (int, string) {
	var dns *net.DNSError
	switch {
	case errors.Is(err, context.DeadlineExceeded) || isTimeout(err):
		return http.StatusGatewayTimeout, "timeout"
	// Any DNS failure, not just NXDOMAIN: for a name no container has, Docker's embedded
	// resolver forwards upstream and answers SERVFAIL ("server misbehaving").
	case errors.As(err, &dns), errors.Is(err, syscall.ECONNREFUSED):
		return http.StatusServiceUnavailable, "not_running"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF), errors.Is(err, syscall.ECONNRESET),
		// net/http's own, unexported: what a worker refusing past its connection cap looks like.
		strings.Contains(err.Error(), "server closed idle connection"):
		return http.StatusServiceUnavailable, "busy"
	case ctx.Err() != nil:
		return http.StatusServiceUnavailable, "client_gone"
	default:
		return http.StatusServiceUnavailable, "unreachable"
	}
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

func describe(w Worker, reason string, timeout time.Duration, err error) string {
	switch reason {
	case "not_running":
		return fmt.Sprintf("%s is not running: nothing answers at %s (%v)", w.Name, w.URL, err)
	case "busy":
		return fmt.Sprintf("%s accepted the connection and closed it without answering; it is at its connection limit (%v)", w.Name, err)
	case "timeout":
		return fmt.Sprintf("%s did not answer within %s", w.Name, timeout)
	default:
		return fmt.Sprintf("%s could not be reached at %s: %v", w.Name, w.URL, err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
