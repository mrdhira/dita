package dip

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
)

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

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			sink := &recordingSink{}
			if err := writeMessage(sink, testCase.control, testCase.payload, limits); err != nil {
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
			if head.PayloadLen != len(testCase.payload) {
				t.Errorf("prologue announces %d payload bytes, sent %d", head.PayloadLen, len(testCase.payload))
			}

			body, err := json.Marshal(testCase.control)
			if err != nil {
				t.Fatalf("encode the expected control block: %v", err)
			}
			wantDatagrams := 1 + chunkCount(len(body), limits.MaxChunk) + chunkCount(len(testCase.payload), limits.MaxChunk)
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
			if !bytes.Equal(payload, testCase.payload) {
				t.Errorf("payload read back as %d bytes, sent %d", len(payload), len(testCase.payload))
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
		wants   string
	}{
		{
			name:    "a control block over the peer's ceiling",
			control: map[string]any{"op": "infer", "note": strings.Repeat("c", 200)},
			wants:   "control block is",
		},
		{
			name:    "a payload over the peer's ceiling",
			control: map[string]any{"op": "infer"},
			payload: bytes.Repeat([]byte("p"), 257),
			wants:   "payload is",
		},
		{
			name:    "a control block that will not encode",
			control: map[string]any{"op": make(chan int)},
			wants:   "encode control block",
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			sink := &recordingSink{}
			err := writeMessage(sink, testCase.control, testCase.payload, limits)
			if err == nil {
				t.Fatal("expected the message to be refused before it was sent")
			}
			if !strings.Contains(err.Error(), testCase.wants) {
				t.Errorf("error %q does not mention %q", err, testCase.wants)
			}
			if len(sink.datagrams) != 0 {
				t.Errorf("refused, but %d datagrams went out", len(sink.datagrams))
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

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			resident, err := ResidentModel(testCase.field)
			if testCase.wantErr {
				if err == nil {
					t.Fatalf("expected an error, got %+v", resident)
				}
				return
			}
			if err != nil {
				t.Fatalf("resident: %v", err)
			}
			switch {
			case testCase.want == "":
				if resident != nil {
					t.Errorf("expected nothing resident, got %+v", resident)
				}
			case resident == nil:
				t.Errorf("expected %q resident, got nothing", testCase.want)
			case resident.Id != testCase.want:
				t.Errorf("resident is %q, expected %q", resident.Id, testCase.want)
			}
		})
	}
}

func TestCodeOf(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want ErrorCode
	}{
		{name: "no error", err: nil, want: ""},
		{name: "not a protocol failure", err: errors.New("connection refused"), want: ""},
		{
			name: "a protocol failure",
			err:  &Error{Code: ErrorCodeNoModelLoaded, Message: "nothing is resident"},
			want: ErrorCodeNoModelLoaded,
		},
		{
			name: "wrapped, because a caller adds the op",
			err:  fmt.Errorf("infer: %w", &Error{Code: ErrorCodeFetchFailed, Message: "download failed"}),
			want: ErrorCodeFetchFailed,
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := CodeOf(testCase.err); got != testCase.want {
				t.Errorf("code is %q, expected %q", got, testCase.want)
			}
		})
	}
}

func chunkCount(length, maxChunk int) int {
	return (length + maxChunk - 1) / maxChunk
}
