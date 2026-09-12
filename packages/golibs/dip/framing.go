// Package dip implements the Dita Inference Protocol, version dip/2: request/response for
// inference workers on the same host, over AF_UNIX / SOCK_SEQPACKET. The specification is
// docs/protocol/[1]dip-specification.md and types.go is generated from
// specs/dip/dip.schema.json.
//
// Both the control block and the payload are chunked, because a single AF_UNIX datagram
// above SO_SNDBUF (212992 bytes by default) fails with EMSGSIZE rather than fragmenting.
package dip

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"slices"
)

// ProtocolVersion is the wire version this package speaks.
const ProtocolVersion = 2

// maxPrologue bounds the first datagram. The prologue is small and fixed-shape, so
// anything larger is refused before its lengths are trusted.
const maxPrologue = 4096

// DefaultLimits are the limits to frame with until Handshake answers. A requester then
// replaces them with the peer's, because nothing on the wire may be hard-coded.
func DefaultLimits() Limits {
	return Limits{
		MaxChunk:        64 * 1024,
		MaxControl:      8 * 1024 * 1024,
		MaxPayload:      64 * 1024 * 1024,
		IdleTimeoutS:    300,
		MessageTimeoutS: 30,
	}
}

// Error is a failure a peer named: the stable code and the prose that accompanies it.
// Branch on Code; Message is for humans and logs.
type Error struct {
	Code    ErrorCode
	Message string
}

func (e *Error) Error() string { return string(e.Code) + ": " + e.Message }

// CodeOf reports the DIP error code carried by err, or the empty string when err is not a
// protocol failure. It is the branch point for a caller deciding whether to retry.
func CodeOf(err error) ErrorCode {
	var protocolErr *Error
	if errors.As(err, &protocolErr) {
		return protocolErr.Code
	}
	return ""
}

// ErrIncomplete reports that the datagrams ended mid-message. It is not a refusal, so it
// carries no error code -- there was never a complete message to refuse.
var ErrIncomplete = errors.New("message incomplete")

func badRequest(format string, args ...any) *Error {
	return &Error{Code: ErrorCodeBadRequest, Message: fmt.Sprintf(format, args...)}
}

// A datagramSource yields exactly one whole datagram per call, in order, so the framing can
// be driven by the conformance corpus as readily as by a socket.
type datagramSource interface {
	next() ([]byte, error)
}

// A datagramSink writes exactly one datagram per call. Not an io.Writer: an io.Writer may
// coalesce, and coalescing destroys the framing.
type datagramSink interface {
	send(datagram []byte) error
}

type packetConn struct {
	conn *net.UnixConn
	// One byte longer than the chunk limit: SOCK_SEQPACKET discards the tail of a datagram
	// that does not fit, so a full buffer is how an over-limit datagram is caught here.
	buf []byte
}

func newPacketConn(conn *net.UnixConn, limits Limits) *packetConn {
	return &packetConn{conn: conn, buf: make([]byte, limits.MaxChunk+1)}
}

func (p *packetConn) resize(limits Limits) {
	if want := limits.MaxChunk + 1; want > 0 && want != len(p.buf) {
		p.buf = make([]byte, want)
	}
}

func (p *packetConn) next() ([]byte, error) {
	n, err := p.conn.Read(p.buf)
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("%w: peer closed the connection", ErrIncomplete)
		}
		var netErr net.Error
		if errors.As(err, &netErr) && netErr.Timeout() {
			return nil, fmt.Errorf("%w: %w", ErrIncomplete, err)
		}
		return nil, err
	}
	if n == 0 {
		// No conforming datagram is empty, so this is the peer closing.
		return nil, fmt.Errorf("%w: peer closed the connection", ErrIncomplete)
	}
	out := make([]byte, n)
	copy(out, p.buf[:n])
	return out, nil
}

func (p *packetConn) send(datagram []byte) error {
	_, err := p.conn.Write(datagram)
	return err
}

// writeMessage sends the prologue in one datagram, then each section chunked.
func writeMessage(dst datagramSink, control any, payload []byte, limits Limits) error {
	body, err := json.Marshal(control)
	if err != nil {
		return fmt.Errorf("encode control block: %w", err)
	}
	if len(body) > limits.MaxControl {
		return fmt.Errorf("control block is %d bytes, over the peer's %d byte limit", len(body), limits.MaxControl)
	}
	if len(payload) > limits.MaxPayload {
		return fmt.Errorf("payload is %d bytes, over the peer's %d byte limit", len(payload), limits.MaxPayload)
	}

	version := ProtocolVersion
	head, err := json.Marshal(Prologue{
		Protocol:   &version,
		ControlLen: len(body),
		PayloadLen: len(payload),
	})
	if err != nil {
		return fmt.Errorf("encode prologue: %w", err)
	}

	if err := dst.send(head); err != nil {
		return fmt.Errorf("write prologue: %w", err)
	}
	if err := sendChunked(dst, body, limits.MaxChunk); err != nil {
		return fmt.Errorf("write control block: %w", err)
	}
	if err := sendChunked(dst, payload, limits.MaxChunk); err != nil {
		return fmt.Errorf("write payload: %w", err)
	}
	return nil
}

func sendChunked(dst datagramSink, blob []byte, maxChunk int) error {
	for start := 0; start < len(blob); start += maxChunk {
		end := min(start+maxChunk, len(blob))
		if err := dst.send(blob[start:end]); err != nil {
			return err
		}
	}
	return nil
}

// readMessage reassembles both sections. The control block comes back raw, validated as a
// JSON object rather than decoded. It fails with an *Error, or with ErrIncomplete.
func readMessage(src datagramSource, limits Limits) (json.RawMessage, []byte, error) {
	raw, err := src.next()
	if err != nil {
		return nil, nil, fmt.Errorf("read prologue: %w", err)
	}
	if len(raw) > limits.MaxChunk {
		return nil, nil, badRequest("peer sent a datagram over the %d byte chunk limit", limits.MaxChunk)
	}
	if len(raw) > maxPrologue {
		return nil, nil, badRequest("prologue is %d bytes; expected a small JSON header", len(raw))
	}

	// Prologue carries the IDL's rules on shape. The ceilings below are framing policy
	// rather than shape, so they live here.
	var head Prologue
	if err := json.Unmarshal(raw, &head); err != nil {
		return nil, nil, badRequest("prologue is not a dip/%d header: %v", ProtocolVersion, err)
	}
	if head.Protocol != nil && *head.Protocol != ProtocolVersion {
		return nil, nil, badRequest("peer speaks protocol %d, this package speaks %d", *head.Protocol, ProtocolVersion)
	}
	if head.ControlLen > limits.MaxControl {
		return nil, nil, badRequest("announced control block of %d bytes exceeds the %d byte limit", head.ControlLen, limits.MaxControl)
	}
	if head.PayloadLen > limits.MaxPayload {
		return nil, nil, badRequest("announced payload of %d bytes exceeds the %d byte limit", head.PayloadLen, limits.MaxPayload)
	}

	control, err := readExactly(src, head.ControlLen, limits)
	if err != nil {
		return nil, nil, fmt.Errorf("read control block: %w", err)
	}
	payload, err := readExactly(src, head.PayloadLen, limits)
	if err != nil {
		return nil, nil, fmt.Errorf("read payload: %w", err)
	}

	var object map[string]json.RawMessage
	if err := json.Unmarshal(control, &object); err != nil {
		return nil, nil, badRequest("control block is not valid JSON: %v", err)
	}
	if object == nil {
		return nil, nil, badRequest("control block must be a JSON object, got null")
	}
	return control, payload, nil
}

// readExactly reassembles total bytes from whole datagrams. The announced length sizes
// nothing: a peer that announces 64 MB and sends nothing must cost this side nothing.
func readExactly(src datagramSource, total int, limits Limits) ([]byte, error) {
	if total == 0 {
		return nil, nil
	}
	out := make([]byte, 0, min(total, limits.MaxChunk))
	for len(out) < total {
		chunk, err := src.next()
		if err != nil {
			return nil, err
		}
		if len(chunk) > limits.MaxChunk {
			return nil, badRequest("peer sent a datagram over the %d byte chunk limit", limits.MaxChunk)
		}
		out = append(out, chunk...)
	}
	if len(out) != total {
		return nil, badRequest("peer sent %d bytes, prologue announced %d", len(out), total)
	}
	return out, nil
}

// Op is a DIP operation: the one field every control block carries.
type Op string

// The ops this protocol version defines. handshake and version are one op under two names,
// and the health probes are ops rather than URL paths: there is no HTTP surface.
const (
	OpHandshake Op = "handshake"
	OpVersion   Op = "version"
	OpList      Op = "list"
	OpLoad      Op = "load"
	OpUnload    Op = "unload"
	OpInfer     Op = "infer"
	OpLivez     Op = "livez"
	OpReadyz    Op = "readyz"
	OpStartupz  Op = "startupz"
)

// opFields is what each op declares beyond "op" itself; anything undeclared is refused
// rather than ignored. Mirrors op_fields in specs/dip/conformance/dispatch.json.
var opFields = map[Op][]string{
	OpHandshake: nil,
	OpVersion:   nil,
	OpList:      nil,
	OpLoad:      {"id", "model"},
	OpUnload:    nil,
	OpInfer:     nil,
	OpLivez:     nil,
	OpReadyz:    nil,
	OpStartupz:  nil,
}

// ValidateControl applies the dispatch rule: the op must exist and every other field must
// be one the op declares. It returns the op, or an *Error with code bad_request naming what
// was wrong. This is the receiver's judgement -- a requester's typed ops are valid by
// construction, so Requester.Call does not gate on it.
func ValidateControl(control json.RawMessage) (Op, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(control, &fields); err != nil {
		return "", badRequest("control block is not valid JSON: %v", err)
	}
	if fields == nil {
		return "", badRequest("control block must be a JSON object")
	}

	rawOp, ok := fields["op"]
	if !ok {
		return "", badRequest("control block has no op field")
	}
	var name string
	if err := json.Unmarshal(rawOp, &name); err != nil {
		return "", badRequest("op must be a string, got %s", rawOp)
	}

	op := Op(name)
	declared, known := opFields[op]
	if !known {
		return "", badRequest("unknown op %q", name)
	}

	// Ahead of the undeclared-field sweep so the refusal points at load rather than merely
	// naming a field: running whatever is resident answers a different question.
	if op == OpInfer {
		if _, carriesModel := fields["model"]; carriesModel {
			return "", badRequest("infer takes no model field; load that model first, then infer")
		}
	}

	var undeclared []string
	for field := range fields {
		if field == "op" || slices.Contains(declared, field) {
			continue
		}
		undeclared = append(undeclared, field)
	}
	if len(undeclared) > 0 {
		slices.Sort(undeclared)
		return "", badRequest("%s does not declare the field %q", op, undeclared[0])
	}

	if op == OpLoad {
		id, err := loadTarget(fields)
		if err != nil {
			return "", err
		}
		if id == "" {
			return "", badRequest("load needs an id naming the model to make resident")
		}
	}
	return op, nil
}

// loadTarget reads the model id from a load control block. id is canonical, model is an
// accepted alias.
func loadTarget(fields map[string]json.RawMessage) (string, error) {
	for _, field := range []string{"id", "model"} {
		raw, ok := fields[field]
		if !ok {
			continue
		}
		var value string
		if err := json.Unmarshal(raw, &value); err != nil {
			return "", badRequest("load %s must be a string, got %s", field, raw)
		}
		if value != "" {
			return value, nil
		}
	}
	return "", nil
}
