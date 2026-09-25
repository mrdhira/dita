package cmd

import (
	"context"
	"dita-orchestrator/decisions"
	"dita-orchestrator/handler"
	"dita-orchestrator/handler/chat"
	decisionsHandler "dita-orchestrator/handler/decisions"
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
				switch checked, err := gateway.CheckWorkerDeadline(ctx); {
				case err != nil:
					log.Error(ctx, "refusing to serve: "+err.Error())
					return err
				case !checked:
					log.Warn(ctx, "could not read "+inferences.SystemOne+"'s deadline_s: INFERENCES_TIMEOUT is not checked against it")
				}

				// The training store: without it a decision could render and never be kept.
				storeDir := envOr("DECISIONS_DIR", "/data/decisions")
				store, err := decisions.Open(storeDir)
				if err != nil {
					return fmt.Errorf("decisions store at %s: %w", storeDir, err)
				}
				for _, r := range store.Rejections() {
					log.Error(ctx, "decisions store: line set aside", slog.String("file", r.File),
						slog.Int("line", r.Line), slog.String("reason", r.Reason))
				}
				for file, n := range store.Quarantined() {
					if n > 0 {
						log.Error(ctx, fmt.Sprintf("%d lines quarantined; see %s.rejected", n, file))
					}
				}
				systemOne, _ := inferencesCfg.Worker(inferences.SystemOne)
				decide := decisionsHandler.New(store, systemOne, inferencesCfg.Timeout, log.Slog())

				addr := envOr("REST_ADDR", ":2104")
				chatHndlr := chat.New(apiKey, log.Slog())
				server := handler.NewRouter(log.Slog(), addr, chatHndlr, gateway, decide, inferencesCfg.ServerWriteTimeout(),
					inferencesCfg.APIToken)
				if inferencesCfg.APIToken == "" {
					log.Info(ctx, "INFERENCES_API_TOKEN is unset: the decisions store's write routes are open")
				}
				log.Info(ctx, "REST run on "+addr+" - Ctrl-C to stop")
				if err := server.Run(ctx); err != nil {
					log.Error(ctx, "error when run rest", slog.String("addr", addr))
				}

				return nil
			},
		}},
	}
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
