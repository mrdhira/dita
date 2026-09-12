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
// because a cold load downloads and verifies hundreds of megabytes; anything that wants a
// tighter bound should say so with a context, which is the only way to get one.
const defaultCallTimeout = 5 * time.Minute

// ErrConnectionRetired reports a call made on a Requester whose connection a failed
// exchange has already retired. A DIP connection carries one message at a time with no
// request id to match an answer to a question, so an exchange that ends early -- a
// deadline, a cancellation, a short write -- leaves the peer's answer queued on the socket
// and the two sides disagreeing about which message is next. The only safe reading of that
// state is that there is no next message: the connection is closed, and every later call
// fails with this instead of returning some earlier call's answer.
//
// A caller that wants to retry after a failure dials again.
var ErrConnectionRetired = errors.New("connection retired by an earlier failed exchange")

// Requester is the requesting half of DIP: it dials a receiver, sends ops and reads
// responses. One Requester is one connection, and a connection carries one message at a
// time, so a Requester is not safe for concurrent use. A caller that wants concurrency
// wants several connections, and the queueing that decides how many.
//
// The receiver owns no queue -- it holds one exclusive lock and serialises behind it, so
// rate limiting, retries, timeouts and backpressure are this side's job.
//
// A Requester survives a refusal but not a failed exchange: an error carrying a code is an
// answer and leaves the connection usable, while a timed-out, cancelled or short exchange
// retires it. See ErrConnectionRetired. Retrying means dialling again.
type Requester struct {
	path   string
	conn   *net.UnixConn
	packet *packetConn
	limits Limits
	// retired holds the failure that ended the last exchange, wrapping
	// ErrConnectionRetired. Non-nil means the connection is closed and every later call
	// fails with it. closed makes Close idempotent, since retiring already closed.
	retired error
	closed  bool
}

// Dial opens a DIP connection to the receiver listening at path. The limits start at
// DefaultLimits and are replaced by whatever the peer advertises on Handshake, which a
// caller should send before anything else.
func Dial(ctx context.Context, path string) (*Requester, error) {
	var dialer net.Dialer
	// "unixpacket" is SOCK_SEQPACKET: the kernel preserves message boundaries and
	// ordering, so one Write is one datagram and one Read is one datagram. Never wrap
	// this connection in a bufio.Writer -- it would merge datagrams and destroy the
	// framing this protocol depends on.
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

// Close releases the connection. It is idempotent, so closing a Requester a failed
// exchange has already retired reports no error.
func (r *Requester) Close() error {
	if r.closed {
		return nil
	}
	r.closed = true
	return r.conn.Close()
}

// Limits reports the limits currently in force: this package's defaults until Handshake
// has answered, and the peer's own after that.
func (r *Requester) Limits() Limits { return r.limits }

// Call sends one request and returns the response control block, along with the payload
// the receiver sent back, if any. It is the escape hatch under the typed ops: an op this
// package predates can be called through it, because adding an op does not bump the wire
// version.
//
// An ErrorResponse comes back as an *Error carrying the code to branch on. A response
// with ok false and no error body is not a failure -- that is a health probe reporting a
// verdict -- and is returned to the caller to interpret.
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

// exchange writes one message and reads the answer to it. Either half failing desynchronises
// the connection -- a half-written message the peer is still reading, or an answer left
// queued on the socket for whoever reads next -- so a failure retires the connection rather
// than leaving it to look reusable.
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

// retire closes the connection and records why, then returns cause unchanged: the caller
// that saw the failure wants to hear about its own deadline or its own short write, and it
// is the calls after this one that hear ErrConnectionRetired instead. The recorded cause
// is prose in that message rather than a %w chain, because a later call did not time out --
// it found a connection that someone else's timeout had already ended.
func (r *Requester) retire(cause error) error {
	if r.retired == nil {
		r.retired = fmt.Errorf("%w: %v", ErrConnectionRetired, cause)
		_ = r.Close()
	}
	return cause
}

// explain states a failed exchange in the caller's terms. The connection understands only
// deadlines, so a context deadline reaches the socket as an i/o timeout and a cancellation
// reaches it as a deadline in the past; either way the caller wants to hear that its own
// context ended the call, not that the peer went quiet. Both errors are kept in the chain,
// so a log still names the read that stalled.
func explain(ctx context.Context, err error) error {
	if ctxErr := ctx.Err(); ctxErr != nil {
		return fmt.Errorf("%w: %w", ctxErr, err)
	}
	var netErr net.Error
	if _, bounded := ctx.Deadline(); bounded && errors.As(err, &netErr) && netErr.Timeout() {
		// The socket deadline is the context's deadline, so it expires a moment before
		// the context itself notices.
		return fmt.Errorf("%w: %w", context.DeadlineExceeded, err)
	}
	return err
}

// applyDeadline honours ctx on a connection that only understands deadlines: the deadline
// bounds the call, and a watchdog unblocks a read already in flight when ctx is cancelled
// first. The returned function stops the watchdog and clears the deadline.
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

// Handshake performs the once-per-connection exchange: it confirms the peer speaks this
// wire version and adopts the limits it advertises, so no size is hard-coded afterwards.
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

// adopt takes the peer's limits, keeping the current value for anything it left at zero:
// a missing limit is not a limit of nothing.
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

// Load makes id resident, releasing whatever was resident before and naming it in
// Unloaded. One model is resident per receiver, so a memory budget is the largest single
// model rather than the sum, and load is a control-plane decision rather than a step in
// every request: it can mean a multi-hundred-megabyte download.
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

// Infer runs the resident model over image, which rides in the payload rather than the
// control block. There is no model field: Load first, so which model answered is never in
// doubt, and an Infer with nothing resident comes back as no_model_loaded rather than
// loading something implicitly.
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

// Livez asks whether the process and its accept loop are up. A false verdict means restart
// it; it checks no dependencies.
func (r *Requester) Livez(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpLivez)
}

// Readyz asks whether the receiver can be given work. A cold load in flight is progress
// rather than a wedge, so it does not make readyz false -- which means a pass is not a
// promise that an infer would succeed this instant. ResidentModel on the response is what
// answers that question.
func (r *Requester) Readyz(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpReadyz)
}

// Startupz asks whether the one-time boot has finished. A false verdict means the receiver
// is still booting and should not be killed for it.
func (r *Requester) Startupz(ctx context.Context) (*ProbeResponse, error) {
	return r.probe(ctx, OpStartupz)
}

// probe runs a health op. A failing probe is a verdict, not an error: it comes back as a
// ProbeResponse with Status fail and the reasons it failed, and only a broken exchange
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

// ResidentModel decodes the resident field carried by handshake, list and readyz. It
// returns nil when nothing is resident, which is the honest answer to "would an infer
// succeed right now" -- a different question from whether the receiver is ready.
//
// The field is a oneOf in the IDL, so the generated types type it as any and the decode
// lands here rather than in the struct.
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
