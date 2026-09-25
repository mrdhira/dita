package handler

import (
	"crypto/sha256"
	"crypto/subtle"
	"dita-orchestrator/handler/chat"
	"dita-orchestrator/handler/decisions"
	"dita-orchestrator/handler/inferences"
	"encoding/json"
	"log/slog"
	"mime"
	"net/http"
	"time"

	"github.com/Wigata-Intech/w-tools/httpx"
	"github.com/Wigata-Intech/w-tools/httpx/middleware"
)

// NewRouter builds the REST server on addr. writeTimeout must outlast the slowest proxied
// inference; see inferences.Config.ServerWriteTimeout. Every POST goes through guardWrites;
// apiToken, when set, is what it requires besides.
func NewRouter(logger *slog.Logger, addr string, chatHandler *chat.ChatHandler, gateway *inferences.Gateway,
	decisionsHandler *decisions.Handler, writeTimeout time.Duration, apiToken string) *httpx.Server {
	srv := httpx.New(httpx.Config{Addr: addr, WriteTimeout: writeTimeout})

	srv.Use(
		middleware.RealIP(middleware.RealIPConfig{}),
		middleware.RequestID(middleware.RequestIDConfig{}),
		middleware.Trace(),
		middleware.Logger(middleware.LoggerConfig{Log: logger}),
		middleware.Recover(middleware.RecoverConfig{Log: logger, ErrorWriter: func(w http.ResponseWriter, _ *http.Request, status int, _ string) {
			refuse(w, status, "Backend", "the orchestrator failed while answering; the log has the details")
		}}),
	)

	write := guardWrites(apiToken)
	v1API := srv.Group("/api/v1")
	v1API.Get("/ping", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("pong"))
	})
	v1API.Post("/chat", write(chatHandler.Chat))

	inferencesAPI := srv.Group("/api/inferences")
	inferencesAPI.Post("/embed", write(gateway.Embed))
	inferencesAPI.Post("/rerank", write(gateway.Rerank))
	inferencesAPI.Post("/decide", write(gateway.Decide))
	inferencesAPI.Get("/workers", gateway.Workers)
	inferencesAPI.Get("/health", gateway.Health)

	inferencesAPI.Get("/schemas", decisionsHandler.Templates)
	inferencesAPI.Post("/schemas", write(decisionsHandler.SaveTemplate))
	inferencesAPI.Get("/schemas/{name}/versions", decisionsHandler.Versions)
	inferencesAPI.Get("/schemas/{name}/versions/{version}", decisionsHandler.Template)
	inferencesAPI.Post("/schemas/{name}/versions/{version}/retire", write(decisionsHandler.Retire))
	inferencesAPI.Post("/decisions", write(decisionsHandler.Decide))
	inferencesAPI.Get("/decisions", decisionsHandler.Recent)
	inferencesAPI.Get("/decisions/{id}", decisionsHandler.Decision)
	inferencesAPI.Post("/decisions/{id}/correction", write(decisionsHandler.Correct))
	inferencesAPI.Post("/evaluations", write(decisionsHandler.Evaluate))
	inferencesAPI.Get("/evaluations", decisionsHandler.Evaluations)
	inferencesAPI.Get("/stats", decisionsHandler.Stats)

	return srv
}

// TokenHeader carries INFERENCES_API_TOKEN on a POST.
const TokenHeader = "X-Inferences-Token"

// guardWrites wraps every POST. A cross-site request is refused, and so is any body not declared
// as JSON: a text/plain or form POST is a CORS simple request a browser sends cross-site without
// a preflight, while application/json forces one, which fails because no CORS headers are served.
// With a token, the request must also carry it; both sides are hashed first so the comparison
// takes the same time whatever the length of the guess.
func guardWrites(token string) func(http.HandlerFunc) http.HandlerFunc {
	want := sha256.Sum256([]byte(token))
	return func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Sec-Fetch-Site") == "cross-site" {
				refuse(w, http.StatusForbidden, "Forbidden", "a cross-site request cannot write here")
				return
			}
			if media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || media != "application/json" {
				refuse(w, http.StatusUnsupportedMediaType, "Validation", "send the body as Content-Type: application/json")
				return
			}
			if token != "" {
				got, present := r.Header[http.CanonicalHeaderKey(TokenHeader)]
				sum := sha256.Sum256([]byte(r.Header.Get(TokenHeader)))
				switch {
				case !present:
					refuse(w, http.StatusUnauthorized, "Unauthorized", "this route needs the "+TokenHeader+" header")
					return
				case len(got) != 1 || subtle.ConstantTimeCompare(sum[:], want[:]) != 1:
					refuse(w, http.StatusUnauthorized, "Unauthorized", "the "+TokenHeader+" header does not match the gateway's INFERENCES_API_TOKEN")
					return
				}
			}
			next(w, r)
		}
	}
}

// refuse answers in the one error shape every route and every worker uses: {error, error_type}.
func refuse(w http.ResponseWriter, status int, errorType, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message, "error_type": errorType})
}
