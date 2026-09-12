package dip

// The receiver role is deliberately not implemented here.
//
// DIP is symmetric in principle -- one side requests, one side receives -- and the
// specification says a package implementing it should eventually support both directions.
// In this repository the receiver is Python: services/inferences-ocr answers on the
// socket, and packages/pylibs/dip carries that half. Nothing in Go listens, so a Go
// receiver would be code with no caller, and code with no caller is code nobody finds the
// bugs in. It is written when something needs it, not before.
//
// What is already here is the half of the receiver that is protocol rather than role, and
// it is tested against the conformance corpus:
//
//   - readMessage decodes a message from a datagram source, refuses a malformed one with
//     bad_request, and reports a truncated one as ErrIncomplete;
//   - writeMessage frames a reply, chunking both sections at the chunk limit;
//   - ValidateControl applies the dispatch rule, so an undeclared field is refused rather
//     than ignored, and an infer carrying a model is pointed back at load.
//
// The shape the rest will take, fixed by the specification and by the corpus:
//
//	type Handler interface {
//	    Handle(ctx context.Context, op Op, control json.RawMessage, payload []byte) (any, error)
//	}
//
//	type Receiver struct { ... }
//	func Listen(path string, handler Handler, opts ...Option) (*Receiver, error)
//	func (r *Receiver) Serve(ctx context.Context) error
//	func (r *Receiver) Close() error
//
// Listen binds an AF_UNIX SOCK_SEQPACKET socket at path with mode 0660 -- access control
// is filesystem permissions, there are no tokens -- and unlinks a stale socket first.
// Serve accepts, and gives each connection a goroutine that loops readMessage,
// ValidateControl, Handler.Handle, writeMessage, until the peer closes or the idle timeout
// expires. An *Error returned by the handler is marshalled as an ErrorResponse; any other
// error becomes an internal, because leaking a Go error string to a peer is how internal
// detail becomes an accidental contract.
//
// Three behaviours the implementation would have to carry, none of which the framing here
// decides:
//
//   - One model resident, one exclusive lock, and no queue. A receiver serialises behind
//     that lock and never sheds load; a requester that wants concurrency owns the queue.
//   - A connection cap, answered with busy after a drain window so the refusal is readable
//     rather than a silent close.
//   - Ceilings enforced before allocation, and a response over max_control refused with
//     response_too_large rather than half-sent.
