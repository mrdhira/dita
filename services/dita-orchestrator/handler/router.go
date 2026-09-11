package handler

import (
	"dita-orchestrator/handler/chat"
	"log/slog"
	"net/http"

	"github.com/Wigata-Intech/w-tools/httpx"
	"github.com/Wigata-Intech/w-tools/httpx/middleware"
)

func NewRouter(logger *slog.Logger, chatHandler *chat.ChatHandler) *httpx.Server {
	srv := httpx.New(httpx.Config{Addr: ":2104"})

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

	return srv
}
