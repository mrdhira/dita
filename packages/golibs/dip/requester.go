package dip

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"
)

// defaultCallTimeout applies when the caller's context carries no deadline. It is generous
// because a cold load downloads and verifies hundreds of megabytes.
const defaultCallTimeout = 5 * time.Minute

// ErrConnectionRetired reports a call on a Requester whose connection a failed exchange has
// already retired. DIP carries one message at a time with no request id matching an answer
// to a question, so an exchange that ends early leaves the peer's answer queued and the two
// sides disagreeing about which message is next. Retrying means dialling again.
var ErrConnectionRetired = errors.New("connection retired by an earlier failed exchange")

// Requester is the requesting half of DIP. One Requester is one connection carrying one
// message at a time, so it is not safe for concurrent use. The receiver owns no queue, so
// rate limiting, retries, timeouts and backpressure are this side's job.
//
// A refusal leaves the connection usable; a timed-out, cancelled or short exchange retires
// it. See ErrConnectionRetired.
type Requester struct {
	path   string
	conn   *net.UnixConn
	packet *packetConn
	limits Limits
	// retired non-nil means a failed exchange closed the connection and every later call
	// fails with it. closed makes Close idempotent, since retiring already closed.
	retired error
	closed  bool
}

// Dial opens a DIP connection to the receiver listening at path. Limits start at
// DefaultLimits and are replaced by the peer's on Handshake, which a caller sends first.
func Dial(ctx context.Context, path string) (*Requester, error) {
	var dialer net.Dialer
	// "unixpacket" is SOCK_SEQPACKET: one Write is one datagram. Never wrap this in a
	// bufio.Writer -- it would merge datagrams and destroy the framing.
	conn, err := dialer.DialContext(ctx, "unixpacket", path)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", path, err)
	}
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		conn.Close()
		return nil, fmt.Errorf("dial %s: expected a unix connection, got %T", path, conn)
	}

	limits := DefaultLimits()
	return &Requester{
		path:   path,
		conn:   unixConn,
		packet: newPacketConn(unixConn, limits),
		limits: limits,
	}, nil
}

// Close releases the connection. It is idempotent.
func (r *Requester) Close() error {
	if r.closed {
		return nil
	}
	r.closed = true
	return r.conn.Close()
}

// Limits reports the limits in force: this package's defaults until Handshake answers.
func (r *Requester) Limits() Limits { return r.limits }

// Call sends one request and returns the response control block and any payload. It is the
// escape hatch under the typed ops: an op this package predates can still be called, because
// adding an op does not bump the wire version. An ErrorResponse comes back as an *Error; an
// ok false with no error body is a health verdict, not a failure.
func (r *Requester) Call(ctx context.Context, op Op, fields map[string]any, payload []byte) (json.RawMessage, []byte, error) {
	control := make(map[string]any, len(fields)+1)
	for name, value := range fields {
		control[name] = value
	}
	control["op"] = string(op)

	if r.retired != nil {
		return nil, nil, fmt.Errorf("%s: %w", op, r.retired)
	}

	stop, err := r.applyDeadline(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("%s: %w", op, err)
	}
	defer stop()

	body, responsePayload, err := r.exchange(control, payload)
	if err != nil {
		return nil, nil, fmt.Errorf("%s: %w", op, explain(ctx, err))
	}

	var head struct {
		Ok    bool       `json:"ok"`
		Error *ErrorBody `json:"error"`
	}
	if err := json.Unmarshal(body, &head); err != nil {
		return body, responsePayload, fmt.Errorf("%s: decode response: %w", op, err)
	}
	if !head.Ok && head.Error != nil {
		return body, responsePayload, fmt.Errorf("%s: %w", op, &Error{Code: head.Error.Code, Message: head.Error.Message})
	}
	return body, responsePayload, nil
}

// exchange writes one message and reads its answer. Either half failing desynchronises the
// connection, so a failure retires it rather than leaving it to look reusable.
func (r *Requester) exchange(control map[string]any, payload []byte) (json.RawMessage, []byte, error) {
	if err := writeMessage(r.packet, control, payload, r.limits); err != nil {
		return nil, nil, r.retire(err)
	}
	body, responsePayload, err := readMessage(r.packet, r.limits)
	if err != nil {
		return nil, nil, r.retire(err)
	}
	return body, responsePayload, nil
}

// retire closes the connection and records why, returning cause unchanged: the caller that
// saw the failure wants its own error, and later calls hear ErrConnectionRetired instead.
func (r *Requester) retire(cause error) error {
	if r.retired == nil {
		r.retired = fmt.Errorf("%w: %v", ErrConnectionRetired, cause)
		_ = r.Close()
	}
	return cause
}

// explain states a failed exchange in the caller's terms: the connection understands only
// deadlines, so a context ending the call reaches the socket as an i/o timeout. Both errors
// stay in the chain, so a log still names the read that stalled.
func explain(ctx context.Context, err error) error {
	if ctxErr := ctx.Err(); ctxErr != nil {
		return fmt.Errorf("%w: %w", ctxErr, err)
	}
	var netErr net.Error
	if _, bounded := ctx.Deadline(); bounded && errors.As(err, &netErr) && netErr.Timeout() {
		// The socket deadline expires a moment before the context itself notices.
		return fmt.Errorf("%w: %w", context.DeadlineExceeded, err)
	}
	return err
}

// applyDeadline honours ctx on a connection that only understands deadlines: a watchdog
// unblocks a read already in flight when ctx is cancelled first.
func (r *Requester) applyDeadline(ctx context.Context) (func(), error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(defaultCallTimeout)
	}
	if err := r.conn.SetDeadline(deadline); err != nil {
		return nil, fmt.Errorf("set deadline: %w", err)
	}

	done := make(chan struct{})
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		select {
		case <-ctx.Done():
			// A deadline in the past is how a blocked Read is woken.
			_ = r.conn.SetDeadline(time.Now())
		case <-done:
		}
	}()

	return func() {
		close(done)
		<-stopped
		_ = r.conn.SetDeadline(time.Time{})
	}, nil
}

// Handshake confirms the peer speaks this wire version and adopts the limits it advertises,
// so no size is hard-coded afterwards. Once per connection, before anything else.
func (r *Requester) Handshake(ctx context.Context) (*HandshakeResponse, error) {
	body, _, err := r.Call(ctx, OpHandshake, nil, nil)
	if err != nil {
		return nil, err
	}
	var response HandshakeResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("handshake: decode response: %w", err)
	}
	if response.Protocol != ProtocolVersion {
		return nil, fmt.Errorf("handshake: peer speaks protocol %d, this package speaks %d", response.Protocol, ProtocolVersion)
	}
	r.adopt(response.Limits)
	return &response, nil
}

// adopt takes the peer's limits, keeping the current value for anything it left at zero.
func (r *Requester) adopt(advertised Limits) {
	if advertised.MaxChunk > 0 {
		r.limits.MaxChunk = advertised.MaxChunk
	}
	if advertised.MaxControl > 0 {
		r.limits.MaxControl = advertised.MaxControl
	}
	if advertised.MaxPayload > 0 {
		r.limits.MaxPayload = advertised.MaxPayload
	}
	if advertised.IdleTimeoutS > 0 {
		r.limits.IdleTimeoutS = advertised.IdleTimeoutS
	}
	if advertised.MessageTimeoutS > 0 {
		r.limits.MessageTimeoutS = advertised.MessageTimeoutS
	}
	r.packet.resize(r.limits)
}

// List reports what the receiver can make resident, and what is resident now.
func (r *Requester) List(ctx context.Context) (*ListResponse, error) {
	body, _, err := r.Call(ctx, OpList, nil, nil)
	if err != nil {
		return nil, err
	}
	var response ListResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("list: decode response: %w", err)
	}
	return &response, nil
}

// Load makes id resident, releasing whatever was resident before and naming it in Unloaded.
// One model is resident per receiver, so a memory budget is the largest single model rather
// than the sum. It can mean a multi-hundred-megabyte download.
func (r *Requester) Load(ctx context.Context, id string) (*LoadResponse, error) {
	if id == "" {
		return nil, errors.New("load: an id is required")
	}
	body, _, err := r.Call(ctx, OpLoad, map[string]any{"id": id}, nil)
	if err != nil {
		return nil, err
	}
	var response LoadResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("load: decode response: %w", err)
	}
	return &response, nil
}

// Unload releases the resident model and gives its memory back.
func (r *Requester) Unload(ctx context.Context) (*UnloadResponse, error) {
	body, _, err := r.Call(ctx, OpUnload, nil, nil)
	if err != nil {
		return nil, err
	}
	var response UnloadResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("unload: decode response: %w", err)
	}
	return &response, nil
}

// Infer runs the resident model over image, which rides in the payload. There is no model
// field: Load first, and an Infer with nothing resident comes back as no_model_loaded.
func (r *Requester) Infer(ctx context.Context, image []byte) (*InferResponse, error) {
	if len(image) == 0 {
		return nil, errors.New("infer: an image payload is required")
	}
	body, _, err := r.Call(ctx, OpInfer, nil, image)
	if err != nil {
		return nil, err
	}
	var response InferResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("infer: decode response: %w", err)
	}
	return &response, nil
}

// Livez asks whether the process and its accept loop are up. It checks no dependencies.
func (r *Requester) Livez(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpLivez)
}

// Readyz asks whether the receiver can be given work. A cold load in flight is progress
// rather than a wedge, so a pass is not a promise that an infer would succeed this instant;
// ResidentModel answers that.
func (r *Requester) Readyz(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpReadyz)
}

// Startupz asks whether the one-time boot has finished.
func (r *Requester) Startupz(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpStartupz)
}

// probe runs a health op. A failing probe is a verdict, not an error: only a broken exchange
// returns a non-nil error.
func (r *Requester) probe(ctx context.Context, op Op) (*ProbeResponse, error) {
	body, _, err := r.Call(ctx, op, nil, nil)
	if err != nil {
		return nil, err
	}
	var response ProbeResponse
	if err := json.Unmarshal(body, &response); err != nil {
		return nil, fmt.Errorf("%s: decode response: %w", op, err)
	}
	return &response, nil
}

// ResidentModel decodes the resident field carried by handshake, list and readyz. It returns
// nil when nothing is resident, which answers "would an infer succeed right now" -- a
// different question from whether the receiver is ready.
func ResidentModel(field any) (*Resident, error) {
	if field == nil {
		return nil, nil
	}
	raw, err := json.Marshal(field)
	if err != nil {
		return nil, fmt.Errorf("encode resident: %w", err)
	}
	if string(raw) == "null" {
		return nil, nil
	}
	var resident Resident
	if err := json.Unmarshal(raw, &resident); err != nil {
		return nil, fmt.Errorf("decode resident: %w", err)
	}
	return &resident, nil
}
