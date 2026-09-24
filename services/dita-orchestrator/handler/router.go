package handler

import (
	"dita-orchestrator/handler/chat"
	"dita-orchestrator/handler/inferences"
	"log/slog"
	"net/http"
	"time"

	"github.com/Wigata-Intech/w-tools/httpx"
	"github.com/Wigata-Intech/w-tools/httpx/middleware"
)

// NewRouter builds the REST server on :2104. writeTimeout must outlast the slowest
// proxied inference; see inferences.Config.ServerWriteTimeout.
func NewRouter(logger *slog.Logger, chatHandler *chat.ChatHandler, gateway *inferences.Gateway, writeTimeout time.Duration) *httpx.Server {
	srv := httpx.New(httpx.Config{Addr: ":2104", WriteTimeout: writeTimeout})

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

	return srv
}
