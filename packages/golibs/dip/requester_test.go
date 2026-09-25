package dip_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mrdhira/dita/packages/golibs/dip"
)

// The receiver in this file frames by hand rather than calling the package's own writer: a
// test that speaks the protocol through the code it is testing proves only that the code
// agrees with itself. The corpus pins the bytes; this pins the socket.

const testChunk = 4096

// fakeReceiver answers on a real AF_UNIX SOCK_SEQPACKET socket.
type fakeReceiver struct {
	t        *testing.T
	path     string
	listener *net.UnixListener
	answer   func(op string, control map[string]any, payload []byte) any
}

// sockPath is a socket path short enough to bind. The kernel caps a unix socket's sun_path at 108
// bytes and refuses anything longer with EINVAL, and t.TempDir() is built from $TMPDIR plus the test
// name, so a long $TMPDIR (a deep workspace, an agent session) or a long test name blows the limit
// while the test itself is perfectly fine. Prefer the test's own directory when it fits, and a short
// one under /tmp when it does not.
func sockPath(t *testing.T) string {
	t.Helper()
	const limit = 100 // sun_path is 108 bytes including the terminator; stay clear of the edge
	if dir := t.TempDir(); len(filepath.Join(dir, "dip.sock")) < limit {
		return filepath.Join(dir, "dip.sock")
	}
	dir, err := os.MkdirTemp("/tmp", "dip")
	if err != nil {
		t.Skipf("no directory short enough for a unix socket (TMPDIR is %d bytes): %v", len(os.TempDir()), err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return filepath.Join(dir, "dip.sock")
}

func startReceiver(t *testing.T, answer func(op string, control map[string]any, payload []byte) any) *fakeReceiver {
	t.Helper()
	path := sockPath(t)
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
		if head.Protocol == nil || *head.Protocol != dip.ProtocolVersion {
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
		"protocol":    dip.ProtocolVersion,
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
		"protocol": dip.ProtocolVersion,
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

func dialHandshaked(t *testing.T, receiver *fakeReceiver) *dip.Requester {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)

	requester, err := dip.Dial(ctx, receiver.path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { requester.Close() })

	if _, err := requester.Handshake(ctx); err != nil {
		t.Fatalf("handshake: %v", err)
	}
	return requester
}

// TestRequesterOverASocket walks the contract in the order a caller has to walk it, which
// is why it is a sequence of subtests rather than a table: load has to precede infer and
// unload has to follow it, because each step changes what is resident on the other side.
// The tables in this file are the tests whose cases are genuinely independent.
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
		_, _, err := requester.Call(ctx, dip.Op("teleport"), nil, nil)
		if err == nil {
			t.Fatal("expected a refusal")
		}
		if got := dip.CodeOf(err); got != dip.ErrorCodeBadRequest {
			t.Fatalf("code is %q: %v", got, err)
		}
		if !strings.Contains(err.Error(), "teleport") {
			t.Errorf("message does not name the op: %v", err)
		}
	})
}

// TestRequesterProbes covers the health ops. A failing probe is a verdict rather than an
// error, which is the distinction the control plane reads: readyz says the receiver can be
// given work, and resident says whether an inference would succeed right now.
func TestRequesterProbes(t *testing.T) {
	receiver := startReceiver(t, func(op string, control map[string]any, payload []byte) any {
		switch op {
		case "handshake":
			return handshakeResponse()
		case "readyz":
			return map[string]any{
				"ok": false, "probe": "readyz", "status": "fail", "uptime_s": 1.5,
				"reasons": []string{"models dir is not writable"}, "resident": nil,
			}
		default:
			return map[string]any{
				"ok": true, "probe": op, "status": "pass", "uptime_s": 1.5,
				"reasons": []string{},
			}
		}
	})

	requester := dialHandshaked(t, receiver)
	ctx := context.Background()

	cases := []struct {
		name        string
		probe       func(context.Context) (*dip.ProbeResponse, error)
		wantProbe   dip.ProbeResponseProbe
		wantStatus  dip.ProbeResponseStatus
		wantReasons int
	}{
		{
			name:       "livez passes",
			probe:      requester.Livez,
			wantProbe:  dip.ProbeResponseProbeLivez,
			wantStatus: dip.ProbeResponseStatusPass,
		},
		{
			name:       "startupz passes",
			probe:      requester.Startupz,
			wantProbe:  dip.ProbeResponseProbeStartupz,
			wantStatus: dip.ProbeResponseStatusPass,
		},
		{
			name:        "a failing readyz is a verdict, not an error",
			probe:       requester.Readyz,
			wantProbe:   dip.ProbeResponseProbeReadyz,
			wantStatus:  dip.ProbeResponseStatusFail,
			wantReasons: 1,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			probe, err := tc.probe(ctx)
			if err != nil {
				t.Fatalf("%s: %v", tc.name, err)
			}
			if probe.Probe != tc.wantProbe {
				t.Errorf("probe is %q, expected %q", probe.Probe, tc.wantProbe)
			}
			if probe.Status != tc.wantStatus {
				t.Errorf("status is %q, expected %q", probe.Status, tc.wantStatus)
			}
			if len(probe.Reasons) != tc.wantReasons {
				t.Errorf("got %d reasons %v, expected %d", len(probe.Reasons), probe.Reasons, tc.wantReasons)
			}
			resident, err := dip.ResidentModel(probe.Resident)
			if err != nil {
				t.Fatalf("resident: %v", err)
			}
			if resident != nil {
				t.Errorf("nothing is resident, got %+v", resident)
			}
		})
	}
}

func TestResidentModel(t *testing.T) {
	cases := []struct {
		name    string
		field   any
		want    string
		wantErr bool
	}{
		{
			name:  "nothing resident",
			field: nil,
		},
		{
			name:  "an explicit null",
			field: json.RawMessage("null"),
		},
		{
			name: "a resident model",
			field: map[string]any{
				"id": "rapidocr-ppocrv5", "engine": "rapidocr",
				"langs": []string{"ja", "en"}, "resident_seconds": 12.5,
			},
			want: "rapidocr-ppocrv5",
		},
		{
			name:    "a resident model missing a required field",
			field:   map[string]any{"id": "rapidocr-ppocrv5"},
			wantErr: true,
		},
		{
			name:    "not an object at all",
			field:   "rapidocr-ppocrv5",
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resident, err := dip.ResidentModel(tc.field)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected an error, got %+v", resident)
				}
				return
			}
			if err != nil {
				t.Fatalf("resident: %v", err)
			}
			switch {
			case tc.want == "":
				if resident != nil {
					t.Errorf("expected nothing resident, got %+v", resident)
				}
			case resident == nil:
				t.Errorf("expected %q resident, got nothing", tc.want)
			case resident.Id != tc.want:
				t.Errorf("resident is %q, expected %q", resident.Id, tc.want)
			}
		})
	}
}

func TestRequesterRefusesAnotherWireVersion(t *testing.T) {
	cases := []struct {
		name     string
		protocol int
		want     string
	}{
		{name: "a later version", protocol: dip.ProtocolVersion + 1, want: "protocol 3"},
		{name: "an earlier version", protocol: dip.ProtocolVersion - 1, want: "protocol 1"},
		{name: "no version at all", protocol: 0, want: "protocol 0"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			receiver := startReceiver(t, func(string, map[string]any, []byte) any {
				response := handshakeResponse()
				response["protocol"] = tc.protocol
				return response
			})

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()

			requester, err := dip.Dial(ctx, receiver.path)
			if err != nil {
				t.Fatalf("dial: %v", err)
			}
			defer requester.Close()

			_, err = requester.Handshake(ctx)
			if err == nil {
				t.Fatalf("expected a refusal of protocol %d", tc.protocol)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error does not name the version: %v", err)
			}
		})
	}
}

func TestRequesterHonoursContext(t *testing.T) {
	// A receiver that accepts and then says nothing, which is what a wedged worker looks
	// like from here.
	receiver := startReceiver(t, func(string, map[string]any, []byte) any {
		time.Sleep(2 * time.Second)
		return handshakeResponse()
	})

	cases := []struct {
		name string
		ctx  func(t *testing.T) context.Context
		want error
	}{
		{
			name: "a deadline bounds the call",
			ctx: func(t *testing.T) context.Context {
				ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
				t.Cleanup(cancel)
				return ctx
			},
			want: context.DeadlineExceeded,
		},
		{
			name: "cancellation unblocks a call in flight",
			ctx: func(t *testing.T) context.Context {
				ctx, cancel := context.WithCancel(context.Background())
				go func() {
					time.Sleep(50 * time.Millisecond)
					cancel()
				}()
				t.Cleanup(cancel)
				return ctx
			},
			want: context.Canceled,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// One connection per case on purpose: a call that ends on the caller's terms
			// leaves the peer mid-message, so the exchange retires the connection and
			// reusing it here would assert the retirement rather than the context.
			requester, err := dip.Dial(context.Background(), receiver.path)
			if err != nil {
				t.Fatalf("dial: %v", err)
			}
			t.Cleanup(func() { requester.Close() })

			if _, err := requester.Handshake(tc.ctx(t)); err == nil {
				t.Fatalf("expected the call to end with %v", tc.want)
			} else if !errors.Is(err, tc.want) {
				t.Errorf("expected %v, got: %v", tc.want, err)
			}
		})
	}
}

// TestAFailedExchangeRetiresTheConnection is the regression test for the worst failure this
// package can have: not an error, but a wrong answer. A call that gives up on its own
// deadline leaves the receiver's response in flight, and those datagrams stay queued on the
// socket. The next call on the same connection reads them and hands back the previous call's
// result -- one image's text for another image -- with no error anywhere. An orchestrator
// that retries a timed-out infer is exactly the caller that would see it.
//
// The three steps are one story in order, which is why they are subtests rather than a table.
func TestAFailedExchangeRetiresTheConnection(t *testing.T) {
	// Long enough that the first caller gives up well before the answer is written, and
	// short enough that the second case can wait for that answer to land.
	const answerDelay = 300 * time.Millisecond

	receiver := startReceiver(t, func(op string, control map[string]any, payload []byte) any {
		if op == "handshake" {
			return handshakeResponse()
		}
		time.Sleep(answerDelay)
		// The text names the image it came from, so a stale answer is identifiable
		// rather than merely suspicious.
		return map[string]any{
			"ok": true, "text": string(payload), "lines": []map[string]any{},
			"model": "fake-model", "infer_ms": 1.0,
		}
	})

	requester := dialHandshaked(t, receiver)

	t.Run("a call slower than its deadline fails", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), answerDelay/10)
		defer cancel()
		if _, err := requester.Infer(ctx, []byte("first image")); err == nil {
			t.Fatal("expected the call to time out")
		} else if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("expected a deadline error, got: %v", err)
		}
	})

	t.Run("the next call never reads the previous answer", func(t *testing.T) {
		// Wait for the abandoned response to reach the socket, so the stale datagrams
		// are really queued: without this the case could pass on timing rather than on
		// the connection having been retired.
		time.Sleep(2 * answerDelay)

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		result, err := requester.Infer(ctx, []byte("second image"))

		if result != nil && result.Text == "first image" {
			t.Fatalf("the second image was answered with the first image's text %q", result.Text)
		}
		if err == nil {
			t.Fatalf("expected a refusal from a retired connection, got %+v", result)
		}
		if !errors.Is(err, dip.ErrConnectionRetired) {
			t.Fatalf("expected ErrConnectionRetired, got: %v", err)
		}
		if result != nil {
			t.Errorf("a retired connection must return no response, got %+v", result)
		}
	})

	t.Run("closing a retired connection is not an error", func(t *testing.T) {
		if err := requester.Close(); err != nil {
			t.Errorf("close: %v", err)
		}
	})
}

func TestDialReportsWhatIsNotThere(t *testing.T) {
	cases := []struct {
		name  string
		setup func(t *testing.T, dir string) string
	}{
		{
			name: "no socket at the path",
			setup: func(t *testing.T, dir string) string {
				return filepath.Join(dir, "absent.sock")
			},
		},
		{
			name: "a path that is a directory",
			setup: func(t *testing.T, dir string) string {
				return dir
			},
		},
		{
			name: "a path that is an ordinary file",
			setup: func(t *testing.T, dir string) string {
				path := filepath.Join(dir, "not-a-socket")
				if err := os.WriteFile(path, []byte("not a socket"), 0o600); err != nil {
					t.Fatalf("write: %v", err)
				}
				return path
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := tc.setup(t, t.TempDir())
			requester, err := dip.Dial(context.Background(), path)
			if err == nil {
				requester.Close()
				t.Fatal("expected a dial error")
			}
			if !strings.Contains(err.Error(), path) {
				t.Errorf("error does not name the socket: %v", err)
			}
		})
	}
}
