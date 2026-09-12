package dip_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/mrdhira/dita/packages/golibs/dip"
	"github.com/mrdhira/dita/packages/golibs/dip/internal/conformance"
)

// framing.go's exported surface, tested from outside the package as a caller sees it. The
// framing underneath is unexported and cannot be reached from here; it is in
// framing_internal_test.go, which says which identifiers force that.

// TestDefaultLimits asserts the invariants rather than the numbers: restating the constants
// would only prove the file can be copied. What matters is that nothing is zero -- a zero
// limit would frame nothing and loop forever -- and that the chunk limit stays under the
// AF_UNIX datagram ceiling, since a Write over it fails with EMSGSIZE instead of splitting.
func TestDefaultLimits(t *testing.T) {
	// SO_SNDBUF on a default Linux kernel, the hard ceiling on one datagram.
	const sndbuf = 212992

	limits := dip.DefaultLimits()

	cases := []struct {
		name string
		got  int
	}{
		{name: "max_chunk", got: limits.MaxChunk},
		{name: "max_control", got: limits.MaxControl},
		{name: "max_payload", got: limits.MaxPayload},
		{name: "idle_timeout_s", got: limits.IdleTimeoutS},
		{name: "message_timeout_s", got: limits.MessageTimeoutS},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.got <= 0 {
				t.Errorf("%s defaults to %d, and a limit of nothing frames nothing", tc.name, tc.got)
			}
		})
	}

	t.Run("max_chunk fits in one datagram", func(t *testing.T) {
		if limits.MaxChunk > sndbuf {
			t.Errorf("max_chunk is %d, over the %d byte AF_UNIX datagram ceiling", limits.MaxChunk, sndbuf)
		}
	})
	t.Run("the sections may be larger than one chunk", func(t *testing.T) {
		if limits.MaxControl < limits.MaxChunk || limits.MaxPayload < limits.MaxChunk {
			t.Errorf("max_control %d and max_payload %d must leave room for a %d byte chunk",
				limits.MaxControl, limits.MaxPayload, limits.MaxChunk)
		}
	})
}

func TestErrorMessage(t *testing.T) {
	cases := []struct {
		name string
		err  *dip.Error
		want string
	}{
		{
			name: "the code leads, the prose follows",
			err:  &dip.Error{Code: dip.ErrorCodeNoModelLoaded, Message: "nothing is resident"},
			want: "no_model_loaded: nothing is resident",
		},
		{
			name: "an empty message still names the code",
			err:  &dip.Error{Code: dip.ErrorCodeBusy},
			want: "busy: ",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.err.Error(); got != tc.want {
				t.Errorf("Error() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestCodeOf(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want dip.ErrorCode
	}{
		{name: "no error", err: nil, want: ""},
		{name: "not a protocol failure", err: errors.New("connection refused"), want: ""},
		{name: "an incomplete message carries no code", err: dip.ErrIncomplete, want: ""},
		{
			name: "a protocol failure",
			err:  &dip.Error{Code: dip.ErrorCodeNoModelLoaded, Message: "nothing is resident"},
			want: dip.ErrorCodeNoModelLoaded,
		},
		{
			name: "wrapped, because a caller adds the op",
			err:  fmt.Errorf("infer: %w", &dip.Error{Code: dip.ErrorCodeFetchFailed, Message: "download failed"}),
			want: dip.ErrorCodeFetchFailed,
		},
		{
			name: "wrapped twice, because the caller's caller adds context too",
			err: fmt.Errorf("worker ocr-1: %w",
				fmt.Errorf("load: %w", &dip.Error{Code: dip.ErrorCodeChecksumMismatch, Message: "sha256 differs"})),
			want: dip.ErrorCodeChecksumMismatch,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := dip.CodeOf(tc.err); got != tc.want {
				t.Errorf("code is %q, expected %q", got, tc.want)
			}
		})
	}
}

// TestValidateControl is this package's own account of the dispatch rule, held separately
// from the corpus below: the corpus lives outside this module and a checkout without it
// skips those cases, which would leave the rule untested here. The corpus proves Go and
// Python refuse the same blocks; this proves Go refuses the right ones on its own.
func TestValidateControl(t *testing.T) {
	cases := []struct {
		name        string
		control     string
		wantOp      dip.Op
		wantCode    dip.ErrorCode
		wantMessage string
	}{
		{name: "handshake", control: `{"op":"handshake"}`, wantOp: dip.OpHandshake},
		{name: "version is handshake under its other name", control: `{"op":"version"}`, wantOp: dip.OpVersion},
		{name: "list", control: `{"op":"list"}`, wantOp: dip.OpList},
		{name: "unload", control: `{"op":"unload"}`, wantOp: dip.OpUnload},
		{name: "infer carries no fields, the image is the payload", control: `{"op":"infer"}`, wantOp: dip.OpInfer},
		{name: "livez", control: `{"op":"livez"}`, wantOp: dip.OpLivez},
		{name: "readyz", control: `{"op":"readyz"}`, wantOp: dip.OpReadyz},
		{name: "startupz", control: `{"op":"startupz"}`, wantOp: dip.OpStartupz},
		{name: "load names a model with id", control: `{"op":"load","id":"rapidocr-ppocrv5"}`, wantOp: dip.OpLoad},
		{name: "load accepts model as an alias for id", control: `{"op":"load","model":"rapidocr-ppocrv5"}`, wantOp: dip.OpLoad},
		{
			name:        "a control block that is not JSON",
			control:     `{"op":`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "not valid JSON",
		},
		{
			name:        "a control block that is null",
			control:     `null`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "must be a JSON object",
		},
		{
			name:        "no op field",
			control:     `{"id":"rapidocr-ppocrv5"}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "no op field",
		},
		{
			name:        "an op that is not a string",
			control:     `{"op":7}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "op must be a string",
		},
		{
			name:        "an op this version does not define",
			control:     `{"op":"teleport"}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: `unknown op "teleport"`,
		},
		{
			name:        "infer carrying a model is pointed back at load",
			control:     `{"op":"infer","model":"rapidocr-ppocrv5"}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "load that model first",
		},
		{
			name:        "an undeclared field is refused rather than ignored",
			control:     `{"op":"list","timeuot_s":5}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: `list does not declare the field "timeuot_s"`,
		},
		{
			name:        "the refusal names the first undeclared field in sorted order",
			control:     `{"op":"list","zeta":1,"alpha":2}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: `the field "alpha"`,
		},
		{
			name:        "load with no id",
			control:     `{"op":"load"}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "load needs an id",
		},
		{
			name:        "load with an empty id",
			control:     `{"op":"load","id":""}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "load needs an id",
		},
		{
			name:        "load with an id that is not a string",
			control:     `{"op":"load","id":42}`,
			wantCode:    dip.ErrorCodeBadRequest,
			wantMessage: "load id must be a string",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			op, err := dip.ValidateControl(json.RawMessage(tc.control))

			if tc.wantCode == "" {
				if err != nil {
					t.Fatalf("expected dispatch to proceed, got: %v", err)
				}
				if op != tc.wantOp {
					t.Errorf("dispatched as %q, expected %q", op, tc.wantOp)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected %s, dispatch proceeded as %q instead", tc.wantCode, op)
			}
			if got := dip.CodeOf(err); got != tc.wantCode {
				t.Fatalf("refused with %q, expected %q: %v", got, tc.wantCode, err)
			}
			if !strings.Contains(err.Error(), tc.wantMessage) {
				t.Errorf("message %q does not mention %q, so the mistake is not findable from it",
					err, tc.wantMessage)
			}
			if op != "" {
				t.Errorf("a refused control block must dispatch to nothing, got %q", op)
			}
		})
	}
}

// dispatchCorpus is specs/dip/conformance/dispatch.json. The op_fields table it also
// carries is cross-checked against this package's own in framing_internal_test.go, which
// is the one thing here that needs to be inside the package.
type dispatchCorpus struct {
	Corpus   string         `json:"corpus"`
	Protocol int            `json:"protocol"`
	Cases    []dispatchCase `json:"cases"`
}

type dispatchCase struct {
	Name    string          `json:"name"`
	Why     string          `json:"why"`
	Control json.RawMessage `json:"control"`
	Expect  struct {
		Outcome         string `json:"outcome"`
		Error           string `json:"error"`
		MessageContains string `json:"message_contains"`
	} `json:"expect"`
}

func TestDispatchCorpus(t *testing.T) {
	var corpus dispatchCorpus
	conformance.Load(t, "dispatch.json", &corpus)

	if corpus.Protocol != dip.ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, dip.ProtocolVersion)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}

	for _, tc := range corpus.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			op, err := dip.ValidateControl(tc.Control)
			expect := tc.Expect

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
				if got := dip.CodeOf(err); got != dip.ErrorCode(expect.Error) {
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
