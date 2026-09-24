package cmd

import (
	"context"
	"dita-orchestrator/handler"
	"dita-orchestrator/handler/chat"
	"dita-orchestrator/handler/inferences"
	"fmt"
	"log/slog"
	"os"

	"github.com/Wigata-Intech/w-tools/cli"
	"github.com/Wigata-Intech/w-tools/httpx/middleware"
	"github.com/Wigata-Intech/w-tools/logger"
)

func ServeRest() *cli.Command {
	return &cli.Command{
		Name:   "dita-orchestrator",
		Short:  "Dita Orchestrator",
		Config: cli.ConfigFile{},
		Commands: []*cli.Command{{
			Name:  "serveRest",
			Short: "start the REST server",
			Run: func(ctx context.Context, args []string) error {
				log := logger.New(logger.Config{
					Env:      "local",
					App:      "dita-orchestrator",
					Protocol: logger.ProtocolHTTP,
					Level:    logger.ParseLevel(os.Getenv("LOG_LEVEL")),
					Redact: logger.RedactConfig{
						Redacted: []string{"authorization"},
					},
					ContextAttrs: func(ctx context.Context) []slog.Attr {
						var a []slog.Attr
						if id := middleware.RequestIDFrom(ctx); id != "" {
							a = append(a, slog.String("request_id", id))
						}
						return a
					},
				})

				apiKey := os.Getenv("DEEPSEEK_API_KEY")
				if apiKey == "" {
					panic("DEEPSEEK_API_KEY cannot be empty")
				}

				inferencesCfg, err := inferences.ConfigFromEnv(os.Getenv)
				if err != nil {
					return fmt.Errorf("inferences gateway config: %w", err)
				}
				gateway := inferences.New(inferencesCfg, log.Slog())

				chatHndlr := chat.New(apiKey, log.Slog())
				server := handler.NewRouter(log.Slog(), chatHndlr, gateway, inferencesCfg.ServerWriteTimeout())
				log.Info(ctx, "REST run on :2104 - Ctrl-C to stop")
				if err := server.Run(ctx); err != nil {
					log.Error(ctx, "error when run rest", slog.String("addr", ":2104"))
				}

				return nil
			},
		}},
	}
}
