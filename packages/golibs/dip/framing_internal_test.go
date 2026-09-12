package dip

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/mrdhira/dita/packages/golibs/dip/internal/conformance"
)

// This file is white-box because the framing it tests is unexported and cannot be reached
// from outside the package at all. Three identifiers force it:
//
//   - writeMessage and readMessage, the framing itself;
//   - datagramSource and datagramSink, whose methods next and send are unexported, so no
//     other package can supply the recorded datagrams a corpus case is made of;
//   - opFields, the dispatch table the corpus cross-checks.
//
// Everything else about framing.go -- ValidateControl, CodeOf, Error, DefaultLimits -- is
// exported and is tested from outside in framing_test.go.

// sliceSource replays a recorded list of datagrams. Running out of them is the peer
// closing, which is what "incomplete" means at this layer.
type sliceSource struct {
	datagrams [][]byte
	at        int
}

func (s *sliceSource) next() ([]byte, error) {
	if s.at >= len(s.datagrams) {
		return nil, fmt.Errorf("%w: the datagrams ran out", ErrIncomplete)
	}
	datagram := s.datagrams[s.at]
	s.at++
	return datagram, nil
}

// recordingSink keeps every datagram it is handed, which is how the chunking rule is
// asserted: the rule is about datagram boundaries, not about the bytes in total.
type recordingSink struct {
	datagrams [][]byte
}

func (r *recordingSink) send(datagram []byte) error {
	r.datagrams = append(r.datagrams, bytes.Clone(datagram))
	return nil
}

func TestWriteMessage(t *testing.T) {
	limits := DefaultLimits()
	limits.MaxChunk = 1024

	cases := []struct {
		name    string
		control map[string]any
		payload []byte
	}{
		{
			name:    "control only sends no payload datagrams",
			control: map[string]any{"op": "list"},
		},
		{
			name:    "a small payload is one datagram",
			control: map[string]any{"op": "infer"},
			payload: []byte("\x89PNG\r\n\x1a\n tiny"),
		},
		{
			name:    "a payload past the chunk limit spans datagrams",
			control: map[string]any{"op": "infer"},
			payload: bytes.Repeat([]byte("p"), 3*limits.MaxChunk+7),
		},
		{
			name:    "a control block past the chunk limit spans datagrams",
			control: map[string]any{"op": "infer", "note": strings.Repeat("c", 5*limits.MaxChunk)},
		},
		{
			name:    "utf-8 is counted in bytes, not characters",
			control: map[string]any{"op": "infer", "note": "日本語のテキスト認識"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sink := &recordingSink{}
			if err := writeMessage(sink, tc.control, tc.payload, limits); err != nil {
				t.Fatalf("write: %v", err)
			}
			if len(sink.datagrams) == 0 {
				t.Fatal("a message is at least a prologue")
			}

			var head Prologue
			if err := json.Unmarshal(sink.datagrams[0], &head); err != nil {
				t.Fatalf("prologue %q: %v", sink.datagrams[0], err)
			}
			if head.Protocol == nil || *head.Protocol != ProtocolVersion {
				t.Fatalf("prologue carries protocol %v", head.Protocol)
			}
			if head.PayloadLen != len(tc.payload) {
				t.Errorf("prologue announces %d payload bytes, sent %d", head.PayloadLen, len(tc.payload))
			}

			body, err := json.Marshal(tc.control)
			if err != nil {
				t.Fatalf("encode the expected control block: %v", err)
			}
			wantDatagrams := 1 + chunkCount(len(body), limits.MaxChunk) + chunkCount(len(tc.payload), limits.MaxChunk)
			if len(sink.datagrams) != wantDatagrams {
				t.Errorf("sent %d datagrams, expected %d", len(sink.datagrams), wantDatagrams)
			}
			for i, datagram := range sink.datagrams {
				if len(datagram) > limits.MaxChunk {
					t.Fatalf("datagram %d is %d bytes, over the %d byte chunk limit", i, len(datagram), limits.MaxChunk)
				}
				if len(datagram) == 0 {
					t.Fatalf("datagram %d is empty, and an empty datagram means the peer closed", i)
				}
			}

			// What was framed must read back, which is the only assertion that covers
			// both halves at once.
			control, payload, err := readMessage(&sliceSource{datagrams: sink.datagrams}, limits)
			if err != nil {
				t.Fatalf("read back: %v", err)
			}
			assertSameJSON(t, body, control)
			if !bytes.Equal(payload, tc.payload) {
				t.Errorf("payload read back as %d bytes, sent %d", len(payload), len(tc.payload))
			}
		})
	}
}

func TestWriteMessageRefusesWhatThePeerWillNotAccept(t *testing.T) {
	limits := Limits{MaxChunk: 64, MaxControl: 128, MaxPayload: 256}

	cases := []struct {
		name    string
		control map[string]any
		payload []byte
		want    string
	}{
		{
			name:    "a control block over the peer's ceiling",
			control: map[string]any{"op": "infer", "note": strings.Repeat("c", 200)},
			want:    "control block is",
		},
		{
			name:    "a payload over the peer's ceiling",
			control: map[string]any{"op": "infer"},
			payload: bytes.Repeat([]byte("p"), 257),
			want:    "payload is",
		},
		{
			name:    "a control block that will not encode",
			control: map[string]any{"op": make(chan int)},
			want:    "encode control block",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sink := &recordingSink{}
			err := writeMessage(sink, tc.control, tc.payload, limits)
			if err == nil {
				t.Fatal("expected the message to be refused before it was sent")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %q does not mention %q", err, tc.want)
			}
			if len(sink.datagrams) != 0 {
				t.Errorf("refused, but %d datagrams went out", len(sink.datagrams))
			}
		})
	}
}

// TestReadMessage is this package's own account of the framing rules, held separately from
// the corpus on purpose: the corpus lives outside this module and a checkout without it
// skips those cases, which would leave readMessage's refusals untested here. The corpus
// proves Go and Python agree; this proves Go is right when the corpus is somewhere else.
func TestReadMessage(t *testing.T) {
	small := Limits{MaxChunk: 64, MaxControl: 128, MaxPayload: 256}
	roomy := Limits{MaxChunk: 8192, MaxControl: 128, MaxPayload: 256}

	prologue := func(control, payload int) []byte {
		return fmt.Appendf(nil, `{"protocol":%d,"control_len":%d,"payload_len":%d}`,
			ProtocolVersion, control, payload)
	}

	cases := []struct {
		name           string
		limits         Limits
		datagrams      [][]byte
		wantControl    string
		wantPayload    string
		wantCode       ErrorCode
		wantIncomplete bool
		wantMessage    string
	}{
		{
			name:        "a whole message decodes",
			limits:      small,
			datagrams:   [][]byte{prologue(14, 5), []byte(`{"op":"infer"}`), []byte("pixel")},
			wantControl: `{"op":"infer"}`,
			wantPayload: "pixel",
		},
		{
			name:        "a payload of zero sends no payload datagrams",
			limits:      small,
			datagrams:   [][]byte{prologue(13, 0), []byte(`{"op":"list"}`)},
			wantControl: `{"op":"list"}`,
		},
		{
			name:        "a section split across datagrams is reassembled",
			limits:      small,
			datagrams:   [][]byte{prologue(14, 9), []byte(`{"op":"infer"}`), []byte("pix"), []byte("els"), []byte("!!!")},
			wantControl: `{"op":"infer"}`,
			wantPayload: "pixels!!!",
		},
		{
			name:        "a prologue over the chunk limit is refused before it is parsed",
			limits:      small,
			datagrams:   [][]byte{bytes.Repeat([]byte("x"), small.MaxChunk+1)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "chunk limit",
		},
		{
			name:   "a prologue under the chunk limit but over the prologue ceiling is refused",
			limits: roomy,
			datagrams: [][]byte{fmt.Appendf(nil, `{"protocol":%d,"control_len":1,"payload_len":0,"pad":%q}`,
				ProtocolVersion, strings.Repeat("p", maxPrologue))},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "expected a small JSON header",
		},
		{
			name:        "a prologue that is not JSON is refused",
			limits:      small,
			datagrams:   [][]byte{[]byte("not json at all")},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "is not a dip/2 header",
		},
		{
			name:        "a prologue with no control_len is refused",
			limits:      small,
			datagrams:   [][]byte{[]byte(`{"protocol":2,"payload_len":0}`)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "control_len",
		},
		{
			name:        "a peer speaking another wire version is refused",
			limits:      small,
			datagrams:   [][]byte{[]byte(`{"protocol":3,"control_len":13,"payload_len":0}`)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "peer speaks protocol 3",
		},
		{
			name:        "an announced control block over the ceiling is refused before it is read",
			limits:      small,
			datagrams:   [][]byte{prologue(small.MaxControl+1, 0)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "announced control block",
		},
		{
			name:        "an announced payload over the ceiling is refused before it is read",
			limits:      small,
			datagrams:   [][]byte{prologue(13, small.MaxPayload+1)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "announced payload",
		},
		{
			name:        "a control chunk over the chunk limit is refused",
			limits:      small,
			datagrams:   [][]byte{prologue(100, 0), bytes.Repeat([]byte("c"), small.MaxChunk+1)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "chunk limit",
		},
		{
			name:        "more bytes than the prologue announced is refused",
			limits:      small,
			datagrams:   [][]byte{prologue(5, 0), []byte(`{"op":"list"}`)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "prologue announced 5",
		},
		{
			name:        "a control block that is not JSON is refused",
			limits:      small,
			datagrams:   [][]byte{prologue(12, 0), []byte(`{"op":"list"`)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "not valid JSON",
		},
		{
			name:        "a control block that is null is refused",
			limits:      small,
			datagrams:   [][]byte{prologue(4, 0), []byte(`null`)},
			wantCode:    ErrorCodeBadRequest,
			wantMessage: "got null",
		},
		{
			name:           "no datagrams at all is an incomplete message",
			limits:         small,
			datagrams:      nil,
			wantIncomplete: true,
		},
		{
			name:           "datagrams ending inside the control block is an incomplete message",
			limits:         small,
			datagrams:      [][]byte{prologue(20, 0), []byte(`{"op":`)},
			wantIncomplete: true,
		},
		{
			name:           "datagrams ending inside the payload is an incomplete message",
			limits:         small,
			datagrams:      [][]byte{prologue(13, 9), []byte(`{"op":"list"}`), []byte("pix")},
			wantIncomplete: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			control, payload, err := readMessage(&sliceSource{datagrams: tc.datagrams}, tc.limits)

			switch {
			case tc.wantIncomplete:
				if err == nil {
					t.Fatalf("expected an incomplete message, it decoded to %s", control)
				}
				if !errors.Is(err, ErrIncomplete) {
					t.Fatalf("expected an incomplete message, got: %v", err)
				}
				if code := CodeOf(err); code != "" {
					t.Errorf("an incomplete message must carry no error code, got %q", code)
				}

			case tc.wantCode != "":
				if err == nil {
					t.Fatalf("expected %s, the message decoded to %s", tc.wantCode, control)
				}
				if errors.Is(err, ErrIncomplete) {
					t.Fatalf("expected the refusal %s, got an incomplete message: %v", tc.wantCode, err)
				}
				if got := CodeOf(err); got != tc.wantCode {
					t.Fatalf("refused with %q, expected %q: %v", got, tc.wantCode, err)
				}
				if !strings.Contains(err.Error(), tc.wantMessage) {
					t.Errorf("message %q does not mention %q, so the mistake is not findable from it",
						err, tc.wantMessage)
				}
				if control != nil || payload != nil {
					t.Error("a refused message must decode to nothing")
				}

			default:
				if err != nil {
					t.Fatalf("expected the message to decode, got: %v", err)
				}
				assertSameJSON(t, json.RawMessage(tc.wantControl), control)
				if string(payload) != tc.wantPayload {
					t.Errorf("payload is %q, expected %q", payload, tc.wantPayload)
				}
			}
		})
	}
}

// The corpus in specs/dip/conformance/ is language-neutral and both implementations run it,
// so a disagreement between Go and Python shows up here rather than on the wire. It lives
// outside this module; see internal/conformance for what an absent corpus does.

type framingCorpus struct {
	Corpus   string `json:"corpus"`
	Protocol int    `json:"protocol"`
	Limits   struct {
		MaxChunk   int `json:"max_chunk"`
		MaxControl int `json:"max_control"`
		MaxPayload int `json:"max_payload"`
	} `json:"limits"`
	Cases []framingCase `json:"cases"`
}

type framingCase struct {
	Name      string   `json:"name"`
	Why       string   `json:"why"`
	Datagrams []string `json:"datagrams"`
	Expect    struct {
		Outcome       string          `json:"outcome"`
		Error         string          `json:"error"`
		Control       json.RawMessage `json:"control"`
		ControlShape  *controlShape   `json:"control_shape"`
		PayloadLen    *int            `json:"payload_len"`
		PayloadSHA256 string          `json:"payload_sha256"`
	} `json:"expect"`
}

// controlShape is the structural assertion the corpus uses where echoing the block would
// only make the file large: a 220 KB control block is checked by these four facts, and
// exact equality is already proven by the small cases.
type controlShape struct {
	ControlLen    int     `json:"control_len"`
	Lines         int     `json:"lines"`
	FirstLineText string  `json:"first_line_text"`
	LastLineN     float64 `json:"last_line_n"`
}

func TestFramingCorpus(t *testing.T) {
	var corpus framingCorpus
	conformance.Load(t, "framing.json", &corpus)

	if corpus.Protocol != ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, ProtocolVersion)
	}
	// The corpus carries only the three size ceilings; the timeouts are not its business.
	limits := DefaultLimits()
	limits.MaxChunk = corpus.Limits.MaxChunk
	limits.MaxControl = corpus.Limits.MaxControl
	limits.MaxPayload = corpus.Limits.MaxPayload

	if len(corpus.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}

	for _, tc := range corpus.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			source := &sliceSource{datagrams: decodeDatagrams(t, tc.Datagrams)}
			control, payload, err := readMessage(source, limits)
			expect := tc.Expect

			switch expect.Outcome {
			case "accept":
				if err != nil {
					t.Fatalf("expected the message to decode, got error: %v", err)
				}
				if expect.Control != nil {
					assertSameJSON(t, expect.Control, control)
				}
				if expect.ControlShape != nil {
					assertControlShape(t, *expect.ControlShape, control)
				}
				if expect.PayloadLen == nil {
					t.Fatal("an accepted case must assert a payload length")
				}
				if len(payload) != *expect.PayloadLen {
					t.Errorf("payload is %d bytes, corpus says %d", len(payload), *expect.PayloadLen)
				}
				digest := sha256.Sum256(payload)
				if got := hex.EncodeToString(digest[:]); got != expect.PayloadSHA256 {
					t.Errorf("payload sha256 is %s, corpus says %s", got, expect.PayloadSHA256)
				}

			case "reject":
				if err == nil {
					t.Fatalf("expected %s, the message decoded instead", expect.Error)
				}
				if errors.Is(err, ErrIncomplete) {
					t.Fatalf("expected the refusal %s, got an incomplete message: %v", expect.Error, err)
				}
				if got := CodeOf(err); got != ErrorCode(expect.Error) {
					t.Fatalf("refused with %q, corpus says %q: %v", got, expect.Error, err)
				}
				if control != nil || payload != nil {
					t.Error("a refused message must decode to nothing")
				}

			case "incomplete":
				if err == nil {
					t.Fatalf("expected an incomplete message, it decoded instead")
				}
				if !errors.Is(err, ErrIncomplete) {
					t.Fatalf("expected an incomplete message, got: %v", err)
				}
				if code := CodeOf(err); code != "" {
					t.Errorf("an incomplete message must carry no error code, got %q", code)
				}

			default:
				t.Fatalf("unknown outcome %q", expect.Outcome)
			}
		})
	}
}

// TestOpFieldsMatchTheCorpus is the half of the dispatch corpus that needs the unexported
// table. The cases themselves go through ValidateControl and are run from outside the
// package, in framing_test.go; this compares the table the corpus declares against the one
// this package dispatches on, in both directions, so an op added on either side without the
// other is a failure rather than a case nobody wrote.
func TestOpFieldsMatchTheCorpus(t *testing.T) {
	var corpus struct {
		Protocol int                 `json:"protocol"`
		OpFields map[string][]string `json:"op_fields"`
	}
	conformance.Load(t, "dispatch.json", &corpus)

	if corpus.Protocol != ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, ProtocolVersion)
	}
	if len(corpus.OpFields) == 0 {
		t.Fatal("corpus declares no ops")
	}

	for name, fields := range corpus.OpFields {
		declared, known := opFields[Op(name)]
		if !known {
			t.Errorf("corpus declares op %q, this package does not know it", name)
			continue
		}
		if len(fields) == 0 && len(declared) == 0 {
			continue
		}
		if !reflect.DeepEqual(fields, declared) {
			t.Errorf("op %q declares %v here, corpus says %v", name, declared, fields)
		}
	}
	for name := range opFields {
		if _, known := corpus.OpFields[string(name)]; !known {
			t.Errorf("this package knows op %q, the corpus does not declare it", name)
		}
	}
}

func decodeDatagrams(t *testing.T, encoded []string) [][]byte {
	t.Helper()
	datagrams := make([][]byte, 0, len(encoded))
	for i, text := range encoded {
		raw, err := base64.StdEncoding.DecodeString(text)
		if err != nil {
			t.Fatalf("decode datagram %d: %v", i, err)
		}
		datagrams = append(datagrams, raw)
	}
	return datagrams
}

// assertSameJSON compares two control blocks as decoded values rather than bytes: key
// order and whitespace are not part of the protocol.
func assertSameJSON(t *testing.T, want, got json.RawMessage) {
	t.Helper()
	var wantValue, gotValue any
	if err := json.Unmarshal(want, &wantValue); err != nil {
		t.Fatalf("decode the corpus control block: %v", err)
	}
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatalf("decode the framed control block: %v", err)
	}
	if !reflect.DeepEqual(wantValue, gotValue) {
		t.Errorf("control block is %v, corpus says %v", gotValue, wantValue)
	}
}

func assertControlShape(t *testing.T, want controlShape, control json.RawMessage) {
	t.Helper()
	if len(control) != want.ControlLen {
		t.Errorf("control block is %d bytes, corpus says %d", len(control), want.ControlLen)
	}
	var decoded struct {
		Lines []struct {
			Text string  `json:"text"`
			N    float64 `json:"n"`
		} `json:"lines"`
	}
	if err := json.Unmarshal(control, &decoded); err != nil {
		t.Fatalf("decode the framed control block: %v", err)
	}
	if len(decoded.Lines) != want.Lines {
		t.Fatalf("control block has %d lines, corpus says %d", len(decoded.Lines), want.Lines)
	}
	if len(decoded.Lines) == 0 {
		return
	}
	if got := decoded.Lines[0].Text; got != want.FirstLineText {
		t.Errorf("first line is %q, corpus says %q", got, want.FirstLineText)
	}
	if got := decoded.Lines[len(decoded.Lines)-1].N; got != want.LastLineN {
		t.Errorf("last line n is %v, corpus says %v", got, want.LastLineN)
	}
}

func chunkCount(length, maxChunk int) int {
	return (length + maxChunk - 1) / maxChunk
}
