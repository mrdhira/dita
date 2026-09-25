package inferences

import (
	"fmt"
	"net/url"
	"strings"
	"time"
)

// The workers the gateway fronts, by container name on the shared network.
const (
	Embedding = "inferences-embedding"
	Reranker  = "inferences-reranker"
	SystemOne = "inferences-system-one"
)

// DefaultTimeout is how long one proxied request may take. The reranker needs up to ~85 s
// for a 32-pair batch under load, so anything near a client library's 30 s default cuts
// off answers that are merely slow.
const DefaultTimeout = 120 * time.Second

// DefaultProbeTimeout bounds each /info and /health call behind GET /workers. Both answer
// without touching the model lock, so a worker that needs longer is not answering at all.
// The dashboard's read deadline is derived from it, and a dashboard test reads this line.
const DefaultProbeTimeout = 5 * time.Second

// MinAPIToken refuses a token short enough to guess; `openssl rand -hex 32` gives 64.
const MinAPIToken = 32

// Worker is one upstream: its container name and the base URL it answers on.
type Worker struct {
	Name string
	URL  *url.URL
}

// Config says where each worker is and how long a request to one may take. APIToken, when
// set, is the secret every state-changing route requires; empty leaves them open.
type Config struct {
	Workers      []Worker
	Timeout      time.Duration
	ProbeTimeout time.Duration
	APIToken     string
}

var workerEnv = []struct{ name, env string }{
	{Embedding, "INFERENCES_EMBEDDING_URL"},
	{Reranker, "INFERENCES_RERANKER_URL"},
	{SystemOne, "INFERENCES_SYSTEM_ONE_URL"},
}

// ConfigFromEnv reads the worker URLs and the timeout, defaulting each worker to
// http://<name>:8080 and the timeout to DefaultTimeout. It refuses a URL or a duration it
// cannot use rather than guessing one.
func ConfigFromEnv(getenv func(string) string) (Config, error) {
	cfg := Config{Timeout: DefaultTimeout, ProbeTimeout: DefaultProbeTimeout}
	for _, w := range workerEnv {
		raw := getenv(w.env)
		if raw == "" {
			raw = "http://" + w.name + ":8080"
		}
		u, err := url.Parse(raw)
		if err != nil {
			return Config{}, fmt.Errorf("%s: %w", w.env, err)
		}
		if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return Config{}, fmt.Errorf("%s: %q is not an http(s) URL with a host", w.env, raw)
		}
		cfg.Workers = append(cfg.Workers, Worker{Name: w.name, URL: u})
	}
	if raw := getenv("INFERENCES_TIMEOUT"); raw != "" {
		d, err := time.ParseDuration(raw)
		if err != nil {
			return Config{}, fmt.Errorf("INFERENCES_TIMEOUT: %w", err)
		}
		if d <= 0 {
			return Config{}, fmt.Errorf("INFERENCES_TIMEOUT: %s is not positive", raw)
		}
		cfg.Timeout = d
	}
	if raw := getenv("INFERENCES_API_TOKEN"); raw != "" {
		if strings.TrimSpace(raw) != raw || len(raw) < MinAPIToken {
			return Config{}, fmt.Errorf("INFERENCES_API_TOKEN: at least %d characters, no surrounding space", MinAPIToken)
		}
		cfg.APIToken = raw
	}
	return cfg, nil
}

// Worker returns the configured worker called name.
func (c Config) Worker(name string) (Worker, bool) {
	for _, w := range c.Workers {
		if w.Name == name {
			return w, true
		}
	}
	return Worker{}, false
}

// ServerWriteTimeout is the REST server's write deadline. It must outlast Timeout: the
// server's own default of 30 s would cut a slow worker's answer off before the proxy's
// timeout was ever reached.
func (c Config) ServerWriteTimeout() time.Duration {
	return c.Timeout + 15*time.Second
}
