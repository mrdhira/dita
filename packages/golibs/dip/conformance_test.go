package dip

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// The corpus in specs/dip/conformance/ is language-neutral and both implementations run
// it, so a disagreement between Go and Python shows up here rather than on the wire.

type framingCorpus struct {
	Corpus   string `json:"corpus"`
	Protocol int    `json:"protocol"`
	Limits   struct {
		MaxChunk   int `json:"max_chunk"`
		MaxControl int `json:"max_control"`
		MaxPayload int `json:"max_payload"`
	} `json:"limits"`
	Cases []struct {
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
	} `json:"cases"`
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

type dispatchCorpus struct {
	Corpus   string              `json:"corpus"`
	Protocol int                 `json:"protocol"`
	OpFields map[string][]string `json:"op_fields"`
	Cases    []struct {
		Name    string          `json:"name"`
		Why     string          `json:"why"`
		Control json.RawMessage `json:"control"`
		Expect  struct {
			Outcome         string `json:"outcome"`
			Error           string `json:"error"`
			MessageContains string `json:"message_contains"`
		} `json:"expect"`
	} `json:"cases"`
}

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

func TestFramingCorpus(t *testing.T) {
	var corpus framingCorpus
	loadCorpus(t, "framing.json", &corpus)

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

	for _, testCase := range corpus.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			source := &sliceSource{datagrams: decodeDatagrams(t, testCase.Datagrams)}
			control, payload, err := readMessage(source, limits)
			expect := testCase.Expect

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

func TestDispatchCorpus(t *testing.T) {
	var corpus dispatchCorpus
	loadCorpus(t, "dispatch.json", &corpus)

	if corpus.Protocol != ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, ProtocolVersion)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}

	t.Run("op table matches the corpus", func(t *testing.T) {
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
	})

	for _, testCase := range corpus.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			op, err := ValidateControl(testCase.Control)
			expect := testCase.Expect

			switch expect.Outcome {
			case "accept":
				if err != nil {
					t.Fatalf("expected dispatch to proceed, got: %v", err)
				}
				if op == "" {
					t.Error("an accepted control block must name its op")
				}

			case "reject":
				if err == nil {
					t.Fatalf("expected %s, dispatch proceeded as %q instead", expect.Error, op)
				}
				if got := CodeOf(err); got != ErrorCode(expect.Error) {
					t.Fatalf("refused with %q, corpus says %q: %v", got, expect.Error, err)
				}
				if expect.MessageContains != "" && !strings.Contains(err.Error(), expect.MessageContains) {
					t.Errorf("message %q does not mention %q, so the mistake is not findable from it",
						err.Error(), expect.MessageContains)
				}

			default:
				t.Fatalf("unknown outcome %q", expect.Outcome)
			}
		})
	}
}

func loadCorpus(t *testing.T, name string, into any) {
	t.Helper()
	path := filepath.Join(repoRoot(t), "specs", "dip", "conformance", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	if err := json.Unmarshal(raw, into); err != nil {
		t.Fatalf("decode %s: %v", path, err)
	}
}

// repoRoot walks up from the test's directory looking for the corpus, so the suite runs
// from wherever the module is checked out.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("working directory: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "specs", "dip", "conformance")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("no specs/dip/conformance above the working directory")
		}
		dir = parent
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
