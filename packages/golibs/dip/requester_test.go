package dip

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The receiver in this file frames by hand rather than calling writeMessage: a test that
// speaks the protocol through the same code it is testing proves only that the code agrees
// with itself. The corpus pins the bytes; this pins the socket.

const testChunk = 4096

// fakeReceiver answers on a real AF_UNIX SOCK_SEQPACKET socket.
type fakeReceiver struct {
	t        *testing.T
	path     string
	listener *net.UnixListener
	answer   func(op string, control map[string]any, payload []byte) any
}

func startReceiver(t *testing.T, answer func(op string, control map[string]any, payload []byte) any) *fakeReceiver {
	t.Helper()
	path := filepath.Join(t.TempDir(), "dip.sock")
	address, err := net.ResolveUnixAddr("unixpacket", path)
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	listener, err := net.ListenUnix("unixpacket", address)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	receiver := &fakeReceiver{t: t, path: path, listener: listener, answer: answer}
	t.Cleanup(func() { listener.Close() })

	go receiver.serve()
	return receiver
}

func (f *fakeReceiver) serve() {
	for {
		conn, err := f.listener.AcceptUnix()
		if err != nil {
			return // the listener closed with the test
		}
		go f.handle(conn)
	}
}

func (f *fakeReceiver) handle(conn *net.UnixConn) {
	defer conn.Close()
	buf := make([]byte, testChunk+1)

	readDatagram := func() ([]byte, bool) {
		n, err := conn.Read(buf)
		if err != nil || n == 0 {
			return nil, false
		}
		if n > testChunk {
			f.t.Errorf("requester sent a %d byte datagram, over the %d byte chunk limit", n, testChunk)
			return nil, false
		}
		return bytes.Clone(buf[:n]), true
	}
	readExact := func(total int) ([]byte, bool) {
		out := make([]byte, 0, total)
		for len(out) < total {
			chunk, ok := readDatagram()
			if !ok {
				return nil, false
			}
			out = append(out, chunk...)
		}
		return out, true
	}

	for {
		raw, ok := readDatagram()
		if !ok {
			return
		}
		var head struct {
			Protocol   *int `json:"protocol"`
			ControlLen int  `json:"control_len"`
			PayloadLen int  `json:"payload_len"`
		}
		if err := json.Unmarshal(raw, &head); err != nil {
			f.t.Errorf("prologue %q: %v", raw, err)
			return
		}
		if head.Protocol == nil || *head.Protocol != ProtocolVersion {
			f.t.Errorf("prologue carried protocol %v", head.Protocol)
			return
		}
		body, ok := readExact(head.ControlLen)
		if !ok {
			return
		}
		payload, ok := readExact(head.PayloadLen)
		if !ok {
			return
		}
		var control map[string]any
		if err := json.Unmarshal(body, &control); err != nil {
			f.t.Errorf("control block %q: %v", body, err)
			return
		}
		op, _ := control["op"].(string)
		if !f.reply(conn, f.answer(op, control, payload)) {
			return
		}
	}
}

func (f *fakeReceiver) reply(conn *net.UnixConn, response any) bool {
	body, err := json.Marshal(response)
	if err != nil {
		f.t.Errorf("encode response: %v", err)
		return false
	}
	head, err := json.Marshal(map[string]int{
		"protocol":    ProtocolVersion,
		"control_len": len(body),
		"payload_len": 0,
	})
	if err != nil {
		f.t.Errorf("encode prologue: %v", err)
		return false
	}
	if _, err := conn.Write(head); err != nil {
		return false
	}
	for start := 0; start < len(body); start += testChunk {
		if _, err := conn.Write(body[start:min(start+testChunk, len(body))]); err != nil {
			return false
		}
	}
	return true
}

// handshakeResponse is what the fake advertises: a chunk limit of 4096, deliberately
// unlike this package's default, so a requester that hard-codes a size fails here.
func handshakeResponse() map[string]any {
	return map[string]any{
		"ok":       true,
		"service":  "fake-receiver",
		"version":  "0.0.1",
		"protocol": ProtocolVersion,
		"limits": map[string]int{
			"max_chunk":         testChunk,
			"max_control":       1 << 20,
			"max_payload":       1 << 20,
			"idle_timeout_s":    300,
			"message_timeout_s": 30,
		},
		"engines":       []string{"fake"},
		"ops":           []string{"handshake", "list", "load", "unload", "infer", "livez", "readyz", "startupz"},
		"default_model": "fake-model",
		"resident":      nil,
	}
}

func dialHandshaked(t *testing.T, receiver *fakeReceiver) *Requester {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)

	requester, err := Dial(ctx, receiver.path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { requester.Close() })

	if _, err := requester.Handshake(ctx); err != nil {
		t.Fatalf("handshake: %v", err)
	}
	return requester
}

func TestRequesterOverASocket(t *testing.T) {
	// An image and a response that both span several datagrams at the advertised chunk
	// limit, because a message that fits in one datagram proves nothing about chunking.
	image := bytes.Repeat([]byte("\x89PNG\r\n\x1a\n"), testChunk)
	lines := make([]map[string]any, 500)
	for i := range lines {
		lines[i] = map[string]any{"text": fmt.Sprintf("line %d %s", i, strings.Repeat("x", 40)), "confidence": 0.5, "box": nil}
	}

	receiver := startReceiver(t, func(op string, control map[string]any, payload []byte) any {
		switch op {
		case "handshake":
			return handshakeResponse()
		case "list":
			return map[string]any{
				"ok":            true,
				"default_model": "fake-model",
				"models": []map[string]any{{
					"id": "fake-model", "description": "a model", "engine": "fake",
					"langs": []string{"en"}, "source_type": "system", "files": 1,
					"bytes": 1024, "unverified_files": []string{},
				}},
				"resident": nil,
			}
		case "load":
			return map[string]any{
				"ok": true, "id": control["id"], "engine": "fake",
				"already_resident": false, "load_ms": 12.5, "unloaded": nil,
			}
		case "infer":
			if !bytes.Equal(payload, image) {
				t.Errorf("payload arrived as %d bytes, sent %d", len(payload), len(image))
			}
			return map[string]any{
				"ok": true, "text": "hello dita", "lines": lines,
				"model": "fake-model", "infer_ms": 42.0,
			}
		case "unload":
			return map[string]any{"ok": true, "unloaded": "fake-model"}
		case "readyz":
			return map[string]any{
				"ok": false, "probe": "readyz", "status": "fail", "uptime_s": 1.5,
				"reasons": []string{"models dir is not writable"}, "resident": nil,
			}
		case "livez", "startupz":
			return map[string]any{
				"ok": true, "probe": op, "status": "pass", "uptime_s": 1.5,
				"reasons": []string{},
			}
		default:
			return map[string]any{"ok": false, "error": map[string]any{
				"code": "bad_request", "message": "unknown op " + op,
			}}
		}
	})

	requester := dialHandshaked(t, receiver)
	ctx := context.Background()

	t.Run("handshake adopts the peer's limits", func(t *testing.T) {
		if got := requester.Limits().MaxChunk; got != testChunk {
			t.Fatalf("max_chunk is %d, the peer advertised %d", got, testChunk)
		}
		if got := requester.Limits().MaxPayload; got != 1<<20 {
			t.Errorf("max_payload is %d, the peer advertised %d", got, 1<<20)
		}
	})

	t.Run("list", func(t *testing.T) {
		listed, err := requester.List(ctx)
		if err != nil {
			t.Fatalf("list: %v", err)
		}
		if len(listed.Models) != 1 || listed.DefaultModel != "fake-model" {
			t.Fatalf("got %d models, default %q", len(listed.Models), listed.DefaultModel)
		}
	})

	t.Run("load", func(t *testing.T) {
		loaded, err := requester.Load(ctx, "fake-model")
		if err != nil {
			t.Fatalf("load: %v", err)
		}
		if loaded.Id != "fake-model" || loaded.Unloaded != nil {
			t.Fatalf("loaded %q, evicted %v", loaded.Id, loaded.Unloaded)
		}
	})

	t.Run("load needs an id", func(t *testing.T) {
		if _, err := requester.Load(ctx, ""); err == nil {
			t.Fatal("expected an error for an empty id")
		}
	})

	t.Run("infer chunks the image and reassembles the response", func(t *testing.T) {
		result, err := requester.Infer(ctx, image)
		if err != nil {
			t.Fatalf("infer: %v", err)
		}
		if result.Text != "hello dita" {
			t.Errorf("text is %q", result.Text)
		}
		if len(result.Lines) != len(lines) {
			t.Fatalf("got %d lines, the receiver sent %d", len(result.Lines), len(lines))
		}
		if result.Lines[len(lines)-1].Text != lines[len(lines)-1]["text"] {
			t.Errorf("last line is %q", result.Lines[len(lines)-1].Text)
		}
	})

	t.Run("infer needs an image", func(t *testing.T) {
		if _, err := requester.Infer(ctx, nil); err == nil {
			t.Fatal("expected an error for an empty payload")
		}
	})

	t.Run("a failing readyz is a verdict, not an error", func(t *testing.T) {
		probe, err := requester.Readyz(ctx)
		if err != nil {
			t.Fatalf("readyz: %v", err)
		}
		if probe.Status != ProbeResponseStatusFail || len(probe.Reasons) != 1 {
			t.Fatalf("status %q, reasons %v", probe.Status, probe.Reasons)
		}
		resident, err := ResidentModel(probe.Resident)
		if err != nil {
			t.Fatalf("resident: %v", err)
		}
		if resident != nil {
			t.Errorf("nothing is resident, got %+v", resident)
		}
	})

	t.Run("livez passes", func(t *testing.T) {
		probe, err := requester.Livez(ctx)
		if err != nil {
			t.Fatalf("livez: %v", err)
		}
		if probe.Status != ProbeResponseStatusPass {
			t.Errorf("status %q", probe.Status)
		}
	})

	t.Run("startupz passes", func(t *testing.T) {
		probe, err := requester.Startupz(ctx)
		if err != nil {
			t.Fatalf("startupz: %v", err)
		}
		if probe.Probe != ProbeResponseProbeStartupz || probe.Status != ProbeResponseStatusPass {
			t.Errorf("probe %q, status %q", probe.Probe, probe.Status)
		}
	})

	t.Run("unload", func(t *testing.T) {
		unloaded, err := requester.Unload(ctx)
		if err != nil {
			t.Fatalf("unload: %v", err)
		}
		if unloaded.Unloaded == nil || *unloaded.Unloaded != "fake-model" {
			t.Fatalf("evicted %v", unloaded.Unloaded)
		}
	})

	t.Run("an error response carries the code to branch on", func(t *testing.T) {
		_, _, err := requester.Call(ctx, Op("teleport"), nil, nil)
		if err == nil {
			t.Fatal("expected a refusal")
		}
		if got := CodeOf(err); got != ErrorCodeBadRequest {
			t.Fatalf("code is %q: %v", got, err)
		}
		if !strings.Contains(err.Error(), "teleport") {
			t.Errorf("message does not name the op: %v", err)
		}
	})
}

func TestRequesterRefusesAnotherWireVersion(t *testing.T) {
	receiver := startReceiver(t, func(string, map[string]any, []byte) any {
		response := handshakeResponse()
		response["protocol"] = 3
		return response
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	requester, err := Dial(ctx, receiver.path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer requester.Close()

	if _, err := requester.Handshake(ctx); err == nil {
		t.Fatal("expected a refusal of protocol 3")
	} else if !strings.Contains(err.Error(), "protocol 3") {
		t.Errorf("error does not name the version: %v", err)
	}
}

func TestRequesterHonoursContext(t *testing.T) {
	// A receiver that accepts and then says nothing, which is what a wedged worker looks
	// like from here.
	receiver := startReceiver(t, func(string, map[string]any, []byte) any {
		time.Sleep(2 * time.Second)
		return handshakeResponse()
	})

	requester, err := Dial(context.Background(), receiver.path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer requester.Close()

	t.Run("a deadline bounds the call", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		if _, err := requester.Handshake(ctx); err == nil {
			t.Fatal("expected the call to time out")
		} else if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("expected a deadline error, got: %v", err)
		}
	})

	t.Run("cancellation unblocks a call in flight", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		go func() {
			time.Sleep(50 * time.Millisecond)
			cancel()
		}()
		if _, err := requester.Handshake(ctx); err == nil {
			t.Fatal("expected the call to be cancelled")
		} else if !errors.Is(err, context.Canceled) {
			t.Errorf("expected a cancellation error, got: %v", err)
		}
	})
}

func TestDialReportsAMissingSocket(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.sock")
	if _, err := Dial(context.Background(), path); err == nil {
		t.Fatal("expected a dial error")
	} else if !strings.Contains(err.Error(), path) {
		t.Errorf("error does not name the socket: %v", err)
	}
}
