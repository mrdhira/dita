package handler

import (
	"dita-orchestrator/handler/chat"
	"dita-orchestrator/handler/decisions"
	"dita-orchestrator/handler/inferences"
	"log/slog"
	"net/http"
	"time"

	"github.com/Wigata-Intech/w-tools/httpx"
	"github.com/Wigata-Intech/w-tools/httpx/middleware"
)

// NewRouter builds the REST server on addr. writeTimeout must outlast the slowest proxied
// inference; see inferences.Config.ServerWriteTimeout.
func NewRouter(logger *slog.Logger, addr string, chatHandler *chat.ChatHandler, gateway *inferences.Gateway,
	decisionsHandler *decisions.Handler, writeTimeout time.Duration) *httpx.Server {
	srv := httpx.New(httpx.Config{Addr: addr, WriteTimeout: writeTimeout})

	srv.Use(
		middleware.RealIP(middleware.RealIPConfig{}),
		middleware.RequestID(middleware.RequestIDConfig{}),
		middleware.Trace(),
		middleware.Logger(middleware.LoggerConfig{Log: logger}),
		middleware.Recover(middleware.RecoverConfig{Log: logger}),
	)

	v1API := srv.Group("/api/v1")
	v1API.Get("/ping", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("pong"))
	})
	v1API.Post("/chat", chatHandler.Chat)

	inferencesAPI := srv.Group("/api/inferences")
	inferencesAPI.Post("/embed", gateway.Embed)
	inferencesAPI.Post("/rerank", gateway.Rerank)
	inferencesAPI.Post("/decide", gateway.Decide)
	inferencesAPI.Get("/workers", gateway.Workers)
	inferencesAPI.Get("/health", gateway.Health)

	inferencesAPI.Get("/schemas", decisionsHandler.Templates)
	inferencesAPI.Post("/schemas", decisionsHandler.SaveTemplate)
	inferencesAPI.Get("/schemas/{name}/versions", decisionsHandler.Versions)
	inferencesAPI.Get("/schemas/{name}/versions/{version}", decisionsHandler.Template)
	inferencesAPI.Post("/decisions", decisionsHandler.Decide)
	inferencesAPI.Get("/decisions", decisionsHandler.Recent)
	inferencesAPI.Get("/decisions/{id}", decisionsHandler.Decision)
	inferencesAPI.Post("/decisions/{id}/correction", decisionsHandler.Correct)
	inferencesAPI.Post("/evaluations", decisionsHandler.Evaluate)
	inferencesAPI.Get("/evaluations", decisionsHandler.Evaluations)
	inferencesAPI.Get("/stats", decisionsHandler.Stats)

	return srv
}
