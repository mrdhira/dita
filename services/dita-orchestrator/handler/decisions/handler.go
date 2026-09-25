// Package decisions serves the dashboard's routes: schema templates, decisions through the
// system-one worker, corrections, evaluations and the capture path's health.
package decisions

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"dita-orchestrator/decisions"
	"dita-orchestrator/handler/inferences"
)

// Body limits: a schema or a decision is small; an evaluation carries up to 10 000 rows.
// maxRecent bounds one history page: each entry carries up to MaxText of pasted text.
const (
	maxBody     = 1 << 20
	maxEvalBody = 8 << 20
	maxRecent   = 50
)

// Handler owns the store and the route to the decision worker.
type Handler struct {
	store   *decisions.Store
	worker  inferences.Worker
	timeout time.Duration
	client  *http.Client
	log     *slog.Logger

	mu       sync.Mutex
	outcomes map[string]int

	// scoring is one slot: an evaluation is a person's upload, and two at once are what fills
	// the container's memory, never real use.
	scoring chan struct{}
}

// New builds a Handler. worker is inferences-system-one as the gateway is configured.
func New(store *decisions.Store, worker inferences.Worker, timeout time.Duration, log *slog.Logger) *Handler {
	return &Handler{store: store, worker: worker, timeout: timeout, log: log, outcomes: map[string]int{},
		scoring: make(chan struct{}, 1),
		client: &http.Client{Transport: &http.Transport{
			DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
			ResponseHeaderTimeout: timeout,
			MaxIdleConnsPerHost:   2,
			IdleConnTimeout:       5 * time.Second,
		}}}
}

// problem is TEI's error body, with the validation issues when there are some.
type problem struct {
	Error     string            `json:"error"`
	ErrorType string            `json:"error_type"`
	Issues    []decisions.Issue `json:"issues,omitempty"`
}

// SaveTemplate writes a new version of a template: POST /schemas.
func (h *Handler) SaveTemplate(w http.ResponseWriter, r *http.Request) {
	var d decisions.Draft
	if !decode(w, r, maxBody, &d) {
		return
	}
	if issues := decisions.ValidateDraft(d); len(issues) > 0 {
		writeJSON(w, http.StatusBadRequest, problem{"the schema is not valid", "Validation", issues})
		return
	}
	t, err := h.store.SaveTemplate(d)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

// Templates lists the latest version of each template: GET /schemas.
func (h *Handler) Templates(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"templates": h.store.Templates()})
}

// Versions lists every version of one template: GET /schemas/{name}/versions.
func (h *Handler) Versions(w http.ResponseWriter, r *http.Request) {
	versions, err := h.store.Versions(r.PathValue("name"))
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"versions": versions})
}

// Template returns one version: GET /schemas/{name}/versions/{version}.
func (h *Handler) Template(w http.ResponseWriter, r *http.Request) {
	version, err := strconv.Atoi(r.PathValue("version"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, problem{Error: "the version is a number", ErrorType: "Validation"})
		return
	}
	t, err := h.store.Template(r.PathValue("name"), version)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

// Retire withdraws a version from new decisions: POST /schemas/{name}/versions/{version}/retire.
// Retiring twice answers with the first retirement.
func (h *Handler) Retire(w http.ResponseWriter, r *http.Request) {
	version, err := strconv.Atoi(r.PathValue("version"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, problem{Error: "the version is a number", ErrorType: "Validation"})
		return
	}
	rt, err := h.store.Retire(r.PathValue("name"), version)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"name": rt.Name, "version": rt.Version, "retired": true, "retired_at": rt.RetiredAt})
}

type decideBody struct {
	Text   string `json:"text"`
	Schema struct {
		Name    string `json:"name"`
		Version int    `json:"version"`
	} `json:"schema"`
}

// Decide asks the worker and, only if it answered with a valid answer, writes the
// prediction: POST /decisions. A worker's own refusal is passed through unchanged and
// nothing is written; a prediction that never happened must not enter the training store.
// A retired version, or one the listing marks unusable, is answered here without spending a
// worker slot.
func (h *Handler) Decide(w http.ResponseWriter, r *http.Request) {
	var body decideBody
	if !decode(w, r, maxBody, &body) {
		return
	}
	if issues := decisions.ValidateText(body.Text); len(issues) > 0 {
		h.count("schema_invalid")
		writeJSON(w, http.StatusBadRequest, problem{"the text is not valid", "Validation", issues})
		return
	}
	v, err := h.store.Template(body.Schema.Name, body.Schema.Version)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if v.Retired {
		h.count("retired")
		writeJSON(w, http.StatusGone, problem{Error: fmt.Sprintf("schema %q version %d is retired: pick another version (GET /api/inferences/schemas/%s/versions)",
			v.Name, v.Version, v.Name), ErrorType: "Retired"})
		return
	}
	t := v.Template
	if !v.Usable {
		h.count("schema_invalid")
		writeJSON(w, http.StatusBadRequest, problem{Error: fmt.Sprintf("schema %q version %d cannot run: %s: %s",
			v.Name, v.Version, v.Faults[0].Path, v.Faults[0].Message), ErrorType: "Validation", Issues: v.Faults})
		return
	}
	payload, _ := json.Marshal(decisions.DecideRequest{Text: body.Text, Questions: t.Questions})
	target := *h.worker.URL
	target.Path = h.worker.URL.Path + "/decide"
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, target.String(), bytes.NewReader(payload))
	if err != nil {
		h.fail(w, r, err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.client.Do(req)
	if err != nil {
		reason := inferences.WriteUnavailable(w, r.Context(), h.worker, h.timeout, err)
		h.log.WarnContext(r.Context(), "decision worker did not answer", slog.String("worker", h.worker.Name),
			slog.String("url", h.worker.URL.String()), slog.String("reason", reason), slog.Any("err", err))
		h.count(reason)
		return
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	if err != nil {
		h.count("unreachable")
		writeJSON(w, http.StatusBadGateway, problem{Error: "reading the worker's answer: " + err.Error(), ErrorType: "Backend"})
		return
	}
	if resp.StatusCode != http.StatusOK {
		h.count(refusal(resp.StatusCode))
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.WriteHeader(resp.StatusCode)
		w.Write(raw)
		return
	}
	reply, err := decisions.ParseReply(raw, t.Questions)
	if err != nil {
		h.count("bad_reply")
		writeJSON(w, http.StatusBadGateway, problem{Error: err.Error(), ErrorType: "Backend"})
		return
	}
	p, err := h.store.AddPrediction(t, body.Text, raw, reply)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	h.count("ok")
	writeJSON(w, http.StatusCreated, view(p, nil, t))
}

// Decision returns one prediction and its correction: GET /decisions/{id}.
func (h *Handler) Decision(w http.ResponseWriter, r *http.Request) {
	p, c, t, err := h.store.Get(r.PathValue("id"))
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, view(p, c, t))
}

// Recent lists the newest predictions with their corrections: GET /decisions?limit=N.
func (h *Handler) Recent(w http.ResponseWriter, r *http.Request) {
	limit := 20
	if raw := r.URL.Query().Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > maxRecent {
			writeJSON(w, http.StatusBadRequest, problem{Error: fmt.Sprintf("limit is 1-%d", maxRecent), ErrorType: "Validation"})
			return
		}
		limit = n
	}
	out := []Decision{}
	for _, id := range h.store.Recent(limit) {
		p, c, t, err := h.store.Get(id)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		out = append(out, view(p, c, t))
	}
	writeJSON(w, http.StatusOK, map[string]any{"decisions": out})
}

// Correct records the human's answers once: POST /decisions/{id}/correction. A second
// correction is refused with 409 and the first stands.
func (h *Handler) Correct(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Answers map[string]string `json:"answers"`
	}
	if !decode(w, r, maxBody, &body) {
		return
	}
	id := r.PathValue("id")
	if _, err := h.store.Correct(id, body.Answers); err != nil {
		h.fail(w, r, err)
		return
	}
	p, c, t, err := h.store.Get(id)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, view(p, c, t))
}

// Evaluate scores an uploaded labelled set and stores the result: POST /evaluations. One runs
// at a time; a second is refused rather than queued.
func (h *Handler) Evaluate(w http.ResponseWriter, r *http.Request) {
	select {
	case h.scoring <- struct{}{}:
		defer func() { <-h.scoring }()
	default:
		writeJSON(w, http.StatusTooManyRequests, problem{Error: "another evaluation is being scored; send this one when it finishes", ErrorType: "Overloaded"})
		return
	}
	name, rows, err := decodeEvaluation(http.MaxBytesReader(w, r.Body, maxEvalBody))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, problem{Error: fmt.Sprintf("the body is not valid: %v", err), ErrorType: "Validation"})
		return
	}
	e, err := decisions.Evaluate(name, rows)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	stored, err := h.store.AddEvaluation(e)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, stored)
}

// Evaluations lists stored results, newest first: GET /evaluations.
func (h *Handler) Evaluations(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"evaluations": h.store.Evaluations()})
}

// Stats is the capture path's health: decision outcomes since start, per schema version how
// many predictions a human answered, and per file how many lines were set aside at start
// (see decisions.Rejection): GET /stats. A template with traffic and
// a correction rate of zero means the capture path is broken.
func (h *Handler) Stats(w http.ResponseWriter, r *http.Request) {
	h.mu.Lock()
	outcomes := map[string]int{}
	for k, v := range h.outcomes {
		outcomes[k] = v
	}
	h.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"outcomes": outcomes, "schemas": h.store.Tallies(),
		"quarantined": h.store.Quarantined()})
}

// Decision is what the dashboard renders: the stored prediction, its answers best first,
// and the correction when there is one. AnswersReparsed says the answers were read under
// today's rules, the record predating stored answers; AnswersError says they could not be,
// and Answers is then null. One unreadable record never fails a list.
type Decision struct {
	ID              string                `json:"id"`
	CreatedAt       time.Time             `json:"created_at"`
	Schema          schemaRef             `json:"schema"`
	InputText       string                `json:"input_text"`
	ModelID         string                `json:"model_id"`
	ModelRevision   string                `json:"model_revision"`
	Answers         []decisions.Answer    `json:"answers"`
	AnswersReparsed bool                  `json:"answers_reparsed"`
	AnswersError    string                `json:"answers_error,omitempty"`
	Correction      *decisions.Correction `json:"correction"`
}

type schemaRef struct {
	Name    string `json:"name"`
	Version int    `json:"version"`
}

func view(p decisions.Prediction, c *decisions.Correction, t decisions.Template) Decision {
	answers, reparsed, err := decisions.ReadAnswers(p, t)
	d := Decision{ID: p.ID, CreatedAt: p.CreatedAt, Schema: schemaRef{p.SchemaID, p.SchemaVersion},
		InputText: p.InputText, ModelID: p.ModelID, ModelRevision: p.ModelRevision,
		Answers: answers, AnswersReparsed: reparsed, Correction: c}
	if err != nil {
		d.AnswersError = err.Error()
	}
	return d
}

func refusal(status int) string {
	switch status {
	case http.StatusServiceUnavailable:
		return "no_model"
	case http.StatusTooManyRequests:
		return "busy"
	case http.StatusBadRequest:
		return "schema_invalid"
	case http.StatusUnprocessableEntity:
		return "engine_refused"
	default:
		return "engine_error"
	}
}

func (h *Handler) count(outcome string) {
	h.mu.Lock()
	h.outcomes[outcome]++
	h.mu.Unlock()
}

func (h *Handler) fail(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, decisions.ErrNotFound):
		writeJSON(w, http.StatusNotFound, problem{Error: "not found", ErrorType: "NotFound"})
	case errors.Is(err, decisions.ErrAlreadyCorrected):
		writeJSON(w, http.StatusConflict, problem{Error: err.Error(), ErrorType: "Conflict"})
	case errors.Is(err, decisions.ErrUnreadable):
		writeJSON(w, http.StatusUnprocessableEntity, problem{Error: err.Error(), ErrorType: "Unreadable"})
	case errors.Is(err, decisions.ErrInvalid):
		writeJSON(w, http.StatusBadRequest, problem{Error: err.Error(), ErrorType: "Validation"})
	default:
		h.log.ErrorContext(r.Context(), "decisions store failed", slog.Any("err", err))
		writeJSON(w, http.StatusInternalServerError, problem{Error: "the store could not complete the request", ErrorType: "Backend"})
	}
}

// decode reads one JSON object and refuses unknown fields, trailing data and oversized
// bodies, answering 400 itself when it returns false.
func decode(w http.ResponseWriter, r *http.Request, limit int64, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, limit))
	dec.DisallowUnknownFields()
	err := dec.Decode(v)
	if err == nil && dec.More() {
		err = errors.New("trailing data after the JSON object")
	}
	if err != nil {
		writeJSON(w, http.StatusBadRequest, problem{Error: fmt.Sprintf("the body is not valid: %v", err), ErrorType: "Validation"})
		return false
	}
	return true
}

// decodeEvaluation reads {"name", "rows"} a row at a time and stops at the first row past
// MaxRows, so a refused upload never builds its rows in memory. It refuses what decode refuses:
// unknown fields and trailing data.
func decodeEvaluation(body io.Reader) (string, []decisions.Row, error) {
	dec := json.NewDecoder(body)
	dec.DisallowUnknownFields()
	var name string
	var rows []decisions.Row
	if err := delim(dec, '{'); err != nil {
		return "", nil, err
	}
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return "", nil, err
		}
		switch key {
		case "name":
			if err := dec.Decode(&name); err != nil {
				return "", nil, err
			}
		case "rows":
			if err := delim(dec, '['); err != nil {
				return "", nil, err
			}
			for dec.More() {
				if len(rows) == decisions.MaxRows {
					return "", nil, fmt.Errorf("between 1 and %d rows", decisions.MaxRows)
				}
				var row decisions.Row
				if err := dec.Decode(&row); err != nil {
					return "", nil, err
				}
				rows = append(rows, row)
			}
			if err := delim(dec, ']'); err != nil {
				return "", nil, err
			}
		default:
			return "", nil, fmt.Errorf("json: unknown field %q", key)
		}
	}
	if err := delim(dec, '}'); err != nil {
		return "", nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return "", nil, errors.New("trailing data after the JSON object")
	}
	return name, rows, nil
}

func delim(dec *json.Decoder, want json.Delim) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	if tok != want {
		return fmt.Errorf("expected %v, found %v", want, tok)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
