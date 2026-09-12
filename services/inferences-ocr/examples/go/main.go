// Command ocrclient is the reference Go client for the inferences-ocr worker: dial,
// handshake, list, load, infer, unload. The framing lives in packages/golibs/dip, so this
// example and the orchestrator share one implementation rather than two that drift.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/mrdhira/dita/packages/golibs/dip"
)

// callTimeout bounds every op; a cold load downloads and verifies hundreds of megabytes.
const callTimeout = 5 * time.Minute

func main() {
	socket := flag.String("socket", envOr("SOCKET_PATH", "/run/dita/inferences-ocr.sock"), "worker unix socket")
	model := flag.String("model", "", "model id to load (default: the worker's default_model)")
	image := flag.String("image", "", "path to an image to run OCR on (required)")
	flag.Parse()

	if err := run(*socket, *model, *image); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(socket, model, image string) error {
	if image == "" {
		return errors.New("-image is required")
	}
	pixels, err := os.ReadFile(image)
	if err != nil {
		return fmt.Errorf("read image: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), callTimeout)
	defer cancel()

	client, err := dip.Dial(ctx, socket)
	if err != nil {
		return err
	}
	defer client.Close()

	hello, err := client.Handshake(ctx)
	if err != nil {
		return err
	}
	fmt.Printf("handshake  %s v%s, protocol %d\n", hello.Service, hello.Version, hello.Protocol)
	fmt.Printf("           ops: %s\n", strings.Join(hello.Ops, ", "))
	fmt.Printf("           max_chunk=%d max_control=%d max_payload=%d\n",
		hello.Limits.MaxChunk, hello.Limits.MaxControl, hello.Limits.MaxPayload)

	listed, err := client.List(ctx)
	if err != nil {
		return err
	}
	fmt.Printf("list       %d models, default %q\n", len(listed.Models), listed.DefaultModel)
	for _, m := range listed.Models {
		fmt.Printf("           %-20s %-10s %-12s %s\n", m.Id, m.Engine, strings.Join(m.Langs, ","), humanBytes(m.Bytes))
	}

	if model == "" {
		model = listed.DefaultModel
	}

	loaded, err := client.Load(ctx, model)
	if err != nil {
		return err
	}
	fmt.Printf("load       %s (%s) in %.0fms, evicted %s\n",
		loaded.Id, loaded.Engine, loaded.LoadMs, orNone(loaded.Unloaded))

	result, err := client.Infer(ctx, pixels)
	if err != nil {
		return err
	}
	fmt.Printf("infer      %s on %s in %.0fms, %d lines\n",
		filepath.Base(image), result.Model, result.InferMs, len(result.Lines))
	for i, line := range result.Lines {
		confidence := "n/a"
		if line.Confidence != nil {
			confidence = fmt.Sprintf("%.3f", *line.Confidence)
		}
		fmt.Printf("           [%d] %-6s %s\n", i, confidence, line.Text)
	}
	fmt.Printf("text       %s\n", strings.ReplaceAll(result.Text, "\n", "\n           "))

	if _, err := client.Unload(ctx); err != nil {
		return err
	}
	fmt.Println("unload     done")
	return nil
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func orNone(value *string) string {
	if value == nil {
		return "nothing"
	}
	return *value
}

func humanBytes(n int) string {
	switch {
	case n == 0:
		return "-"
	case n < 1<<20:
		return fmt.Sprintf("%d KB", n>>10)
	default:
		return fmt.Sprintf("%d MB", n>>20)
	}
}
