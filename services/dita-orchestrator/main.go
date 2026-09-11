package main

import (
	"context"
	"dita-orchestrator/cmd"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(cmd.ServeRest().Execute(ctx))
}
