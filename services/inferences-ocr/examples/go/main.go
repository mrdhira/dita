// Command ocrclient is the reference Go client for the inferences-ocr worker.
//
// It is the shape the orchestrator's client will take: dial the unix socket, handshake,
// list what is selectable, load one model, run an image through it, unload. Everything
// here is standard library — net, encoding/json, os and friends. There is no module
// dependency and there never should be one; the protocol is small enough that a
// dependency would cost more than it saves.
//
// The framing is the interesting part, so read sendMessage and recvMessage first.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	// protocolVersion is what handshake must report back. A worker answering a version
	// this client does not know is a worker it should not drive.
	protocolVersion = 2

	// maxChunk is the largest datagram either side may send, and therefore also the read
	// buffer size. It is well under SO_SNDBUF (212992 bytes by default): an AF_UNIX
	// datagram larger than that is rejected outright rather than fragmented, which is
	// exactly why the control block is chunked and not sent whole.
	maxChunk = 64 * 1024

	dialTimeout = 10 * time.Second
	callTimeout = 5 * time.Minute // a cold load downloads and verifies hundreds of MB
)

// prologue is the first datagram of every message, in both directions. It is small and
// fixed-shape, so it always fits in one datagram no matter how large the message is.
type prologue struct {
	Protocol   int `json:"protocol"`
	ControlLen int `json:"control_len"`
	PayloadLen int `json:"payload_len"`
}

type client struct {
	conn *net.UnixConn
	buf  []byte
}

func dial(path string) (*client, error) {
	addr, err := net.ResolveUnixAddr("unixpacket", path)
	if err != nil {
		return nil, fmt.Errorf("resolve %s: %w", path, err)
	}
	// "unixpacket" is SOCK_SEQPACKET: message boundaries and ordering are preserved, so
	// one Write is one datagram and one Read is one datagram. Never wrap this in a
	// bufio.Writer — that would merge datagrams and destroy the framing.
	conn, err := net.DialUnix("unixpacket", nil, addr)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", path, err)
	}
	return &client{conn: conn, buf: make([]byte, maxChunk)}, nil
}

func (c *client) Close() error { return c.conn.Close() }

// sendMessage writes one message: the prologue, then the control block and the payload,
// each split into datagrams of at most maxChunk bytes.
func (c *client) sendMessage(control any, payload []byte) error {
	body, err := json.Marshal(control)
	if err != nil {
		return fmt.Errorf("encode control block: %w", err)
	}

	head, err := json.Marshal(prologue{
		Protocol:   protocolVersion,
		ControlLen: len(body),
		PayloadLen: len(payload),
	})
	if err != nil {
		return fmt.Errorf("encode prologue: %w", err)
	}

	if _, err := c.conn.Write(head); err != nil {
		return fmt.Errorf("write prologue: %w", err)
	}
	if err := c.writeChunked(body); err != nil {
		return fmt.Errorf("write control block: %w", err)
	}
	if err := c.writeChunked(payload); err != nil {
		return fmt.Errorf("write payload: %w", err)
	}
	return nil
}

func (c *client) writeChunked(blob []byte) error {
	for start := 0; start < len(blob); start += maxChunk {
		end := min(start+maxChunk, len(blob))
		if _, err := c.conn.Write(blob[start:end]); err != nil {
			return err
		}
	}
	return nil
}

// recvMessage reads one message back, reassembling both chunked sections.
func (c *client) recvMessage() (json.RawMessage, []byte, error) {
	raw, err := c.readDatagram()
	if err != nil {
		return nil, nil, fmt.Errorf("read prologue: %w", err)
	}

	var head prologue
	if err := json.Unmarshal(raw, &head); err != nil {
		return nil, nil, fmt.Errorf("decode prologue %q: %w", raw, err)
	}
	if head.Protocol != 0 && head.Protocol != protocolVersion {
		return nil, nil, fmt.Errorf("worker speaks protocol %d, this client speaks %d", head.Protocol, protocolVersion)
	}

	body, err := c.readExactly(head.ControlLen)
	if err != nil {
		return nil, nil, fmt.Errorf("read control block: %w", err)
	}
	payload, err := c.readExactly(head.PayloadLen)
	if err != nil {
		return nil, nil, fmt.Errorf("read payload: %w", err)
	}
	return body, payload, nil
}

func (c *client) readDatagram() ([]byte, error) {
	n, err := c.conn.Read(c.buf)
	if err != nil {
		return nil, err
	}
	if n == 0 {
		// No conforming datagram is empty, so this is the peer closing.
		return nil, errors.New("worker closed the connection")
	}
	out := make([]byte, n)
	copy(out, c.buf[:n])
	return out, nil
}

func (c *client) readExactly(total int) ([]byte, error) {
	if total == 0 {
		return nil, nil
	}
	out := make([]byte, 0, total)
	for len(out) < total {
		chunk, err := c.readDatagram()
		if err != nil {
			return nil, err
		}
		out = append(out, chunk...)
	}
	if len(out) != total {
		return nil, fmt.Errorf("got %d bytes, prologue announced %d", len(out), total)
	}
	return out, nil
}

// workerError is the failure shape every op shares.
type workerError struct {
	OK    bool `json:"ok"`
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// call runs one request and returns the raw control block, or the worker's error.
func (c *client) call(op string, fields map[string]any, payload []byte) (json.RawMessage, error) {
	if err := c.conn.SetDeadline(time.Now().Add(callTimeout)); err != nil {
		return nil, err
	}

	control := map[string]any{"op": op}
	for k, v := range fields {
		control[k] = v
	}
	if err := c.sendMessage(control, payload); err != nil {
		return nil, err
	}

	body, _, err := c.recvMessage()
	if err != nil {
		return nil, err
	}

	var failure workerError
	if err := json.Unmarshal(body, &failure); err != nil {
		return nil, fmt.Errorf("decode %s response: %w", op, err)
	}
	if !failure.OK {
		return body, fmt.Errorf("%s: %s: %s", op, failure.Error.Code, failure.Error.Message)
	}
	return body, nil
}

type handshakeResponse struct {
	Service      string         `json:"service"`
	Version      string         `json:"version"`
	Protocol     int            `json:"protocol"`
	Limits       map[string]int `json:"limits"`
	Ops          []string       `json:"ops"`
	DefaultModel string         `json:"default_model"`
}

type listResponse struct {
	DefaultModel string `json:"default_model"`
	Models       []struct {
		ID          string   `json:"id"`
		Engine      string   `json:"engine"`
		Langs       []string `json:"langs"`
		Bytes       int64    `json:"bytes"`
		Description string   `json:"description"`
	} `json:"models"`
}

type loadResponse struct {
	ID       string  `json:"id"`
	Engine   string  `json:"engine"`
	LoadMS   float64 `json:"load_ms"`
	Unloaded *string `json:"unloaded"`
}

type inferResponse struct {
	Text    string  `json:"text"`
	Model   string  `json:"model"`
	InferMS float64 `json:"infer_ms"`
	Lines   []struct {
		Text       string      `json:"text"`
		Confidence *float64    `json:"confidence"`
		Box        [][]float64 `json:"box"`
	} `json:"lines"`
}

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

	c, err := dial(socket)
	if err != nil {
		return err
	}
	defer c.Close()

	// 1. handshake — confirm the protocol and learn the limits.
	body, err := c.call("handshake", nil, nil)
	if err != nil {
		return err
	}
	var hs handshakeResponse
	if err := json.Unmarshal(body, &hs); err != nil {
		return err
	}
	fmt.Printf("handshake  %s v%s, protocol %d\n", hs.Service, hs.Version, hs.Protocol)
	fmt.Printf("           ops: %s\n", strings.Join(hs.Ops, ", "))
	fmt.Printf("           max_chunk=%d max_control=%d max_payload=%d\n",
		hs.Limits["max_chunk"], hs.Limits["max_control"], hs.Limits["max_payload"])

	// 2. list — what is selectable.
	body, err = c.call("list", nil, nil)
	if err != nil {
		return err
	}
	var listed listResponse
	if err := json.Unmarshal(body, &listed); err != nil {
		return err
	}
	fmt.Printf("list       %d models, default %q\n", len(listed.Models), listed.DefaultModel)
	for _, m := range listed.Models {
		fmt.Printf("           %-20s %-10s %-12s %s\n", m.ID, m.Engine, strings.Join(m.Langs, ","), humanBytes(m.Bytes))
	}

	if model == "" {
		model = listed.DefaultModel
	}

	// 3. load — the orchestrator decides what is resident; the worker never guesses.
	body, err = c.call("load", map[string]any{"id": model}, nil)
	if err != nil {
		return err
	}
	var loaded loadResponse
	if err := json.Unmarshal(body, &loaded); err != nil {
		return err
	}
	fmt.Printf("load       %s (%s) in %.0fms, evicted %s\n",
		loaded.ID, loaded.Engine, loaded.LoadMS, orNone(loaded.Unloaded))

	// 4. infer — the image rides in the payload, not in the control block.
	body, err = c.call("infer", nil, pixels)
	if err != nil {
		return err
	}
	var result inferResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return err
	}
	fmt.Printf("infer      %s on %s in %.0fms, %d lines\n",
		filepath.Base(image), result.Model, result.InferMS, len(result.Lines))
	for i, line := range result.Lines {
		confidence := "n/a"
		if line.Confidence != nil {
			confidence = fmt.Sprintf("%.3f", *line.Confidence)
		}
		fmt.Printf("           [%d] %-6s %s\n", i, confidence, line.Text)
	}
	fmt.Printf("text       %s\n", strings.ReplaceAll(result.Text, "\n", "\n           "))

	// 5. unload — give the memory back.
	if _, err := c.call("unload", nil, nil); err != nil {
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

func humanBytes(n int64) string {
	switch {
	case n == 0:
		return "-"
	case n < 1<<20:
		return fmt.Sprintf("%d KB", n>>10)
	default:
		return fmt.Sprintf("%d MB", n>>20)
	}
}
