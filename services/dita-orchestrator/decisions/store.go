package decisions

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// FormatMarker names the on-disk layout. Opening an empty directory writes it; opening a
// directory marked with anything else is refused rather than guessed at.
const FormatMarker = "dita-decisions/1\n"

// Errors a caller branches on.
var (
	ErrNotFound         = errors.New("not found")
	ErrAlreadyCorrected = errors.New("a correction is already recorded for this prediction; the first stands")
	ErrInvalid          = errors.New("invalid")
)

// Prediction is one recommendation, written once. `Prediction` is the worker's answer as
// returned and `Confidence` its confidence as returned; neither is recomputed.
type Prediction struct {
	ID            string             `json:"id"`
	CreatedAt     time.Time          `json:"created_at"`
	SchemaID      string             `json:"schema_id"`
	SchemaVersion int                `json:"schema_version"`
	InputText     string             `json:"input_text"`
	ModelID       string             `json:"model_id"`
	ModelRevision string             `json:"model_revision"`
	Prediction    json.RawMessage    `json:"prediction"`
	Confidence    map[string]float64 `json:"confidence"`
}

// Correction is the human's answer to a prediction: a second, separate write, never merged
// into the prediction. Outcomes say, per question, whether the top suggestion was kept.
type Correction struct {
	PredictionID string            `json:"prediction_id"`
	CorrectedAt  time.Time         `json:"corrected_at"`
	Answers      map[string]string `json:"answers"`
	Outcomes     map[string]string `json:"outcomes"`
}

// Store is the append-only store. Every write is one JSON line, appended and synced before
// memory changes, so what a reader sees is always on disk. One process owns a directory.
type Store struct {
	mu          sync.Mutex
	dir         string
	now         func() time.Time
	templates   map[string][]Template
	predictions map[string]Prediction
	order       []string
	corrections map[string]Correction
	evaluations []Evaluation
}

var files = []string{"templates.jsonl", "predictions.jsonl", "corrections.jsonl", "evaluations.jsonl"}

// Open reads every record under dir into memory, creating the layout in an empty directory.
// A line that does not parse, or a second correction for one prediction, is an error: the
// store is refused rather than silently repaired.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("create %s: %w", dir, err)
	}
	marker := filepath.Join(dir, "FORMAT")
	switch got, err := os.ReadFile(marker); {
	case errors.Is(err, os.ErrNotExist):
		if err := os.WriteFile(marker, []byte(FormatMarker), 0o640); err != nil {
			return nil, fmt.Errorf("write %s: %w", marker, err)
		}
	case err != nil:
		return nil, fmt.Errorf("read %s: %w", marker, err)
	case string(got) != FormatMarker:
		return nil, fmt.Errorf("%s says %q, this build reads %q", marker, strings.TrimSpace(string(got)), strings.TrimSpace(FormatMarker))
	}
	s := &Store{dir: dir, now: time.Now, templates: map[string][]Template{},
		predictions: map[string]Prediction{}, corrections: map[string]Correction{}}
	if err := s.replay(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Store) replay() error {
	return errors.Join(
		readLines(s.dir, "templates.jsonl", func(t Template) error {
			if t.Version != len(s.templates[t.Name])+1 {
				return fmt.Errorf("template %s version %d out of sequence", t.Name, t.Version)
			}
			s.templates[t.Name] = append(s.templates[t.Name], t)
			return nil
		}),
		readLines(s.dir, "predictions.jsonl", func(p Prediction) error {
			if _, dup := s.predictions[p.ID]; dup {
				return fmt.Errorf("prediction %s written twice", p.ID)
			}
			s.predictions[p.ID] = p
			s.order = append(s.order, p.ID)
			return nil
		}),
		readLines(s.dir, "corrections.jsonl", func(c Correction) error {
			if _, dup := s.corrections[c.PredictionID]; dup {
				return fmt.Errorf("prediction %s corrected twice", c.PredictionID)
			}
			s.corrections[c.PredictionID] = c
			return nil
		}),
		readLines(s.dir, "evaluations.jsonl", func(e Evaluation) error {
			s.evaluations = append(s.evaluations, e)
			return nil
		}),
	)
}

func readLines[T any](dir, name string, apply func(T) error) error {
	f, err := os.Open(filepath.Join(dir, name))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("open %s: %w", name, err)
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1<<20), 16<<20)
	for line := 1; scanner.Scan(); line++ {
		var record T
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			return fmt.Errorf("%s:%d: %w", name, line, err)
		}
		if err := apply(record); err != nil {
			return fmt.Errorf("%s:%d: %w", name, line, err)
		}
	}
	return scanner.Err()
}

func (s *Store) append(name string, record any) error {
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(s.dir, name), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o640)
	if err != nil {
		return fmt.Errorf("open %s: %w", name, err)
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("append %s: %w", name, err)
	}
	if err := f.Sync(); err != nil {
		return fmt.Errorf("sync %s: %w", name, err)
	}
	return nil
}

// SaveTemplate writes a new version of a template; an existing version is never touched.
func (s *Store) SaveTemplate(d Draft) (Template, error) {
	if issues := ValidateDraft(d); len(issues) > 0 {
		return Template{}, fmt.Errorf("%w: %s", ErrInvalid, issues[0].Message)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	t := Template{Name: d.Name, Version: len(s.templates[d.Name]) + 1, Description: d.Description,
		Questions: d.Questions, CreatedAt: s.now().UTC()}
	if err := s.append("templates.jsonl", t); err != nil {
		return Template{}, err
	}
	s.templates[d.Name] = append(s.templates[d.Name], t)
	return t, nil
}

// Templates returns the latest version of every template, by name.
func (s *Store) Templates() []Template {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Template, 0, len(s.templates))
	for _, versions := range s.templates {
		out = append(out, versions[len(versions)-1])
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Versions returns every version of one template, oldest first.
func (s *Store) Versions(name string) ([]Template, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	versions, ok := s.templates[name]
	if !ok {
		return nil, ErrNotFound
	}
	return append([]Template(nil), versions...), nil
}

// Template returns one version of one template.
func (s *Store) Template(name string, version int) (Template, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	versions := s.templates[name]
	if version < 1 || version > len(versions) {
		return Template{}, ErrNotFound
	}
	return versions[version-1], nil
}

// AddPrediction writes a prediction. It is called only after the worker answered.
func (s *Store) AddPrediction(t Template, text string, raw []byte, reply Reply) (Prediction, error) {
	id, err := newID()
	if err != nil {
		return Prediction{}, err
	}
	p := Prediction{ID: id, SchemaID: t.Name, SchemaVersion: t.Version, InputText: text,
		ModelID: reply.ModelID, ModelRevision: reply.ModelRevision, Prediction: json.RawMessage(raw),
		Confidence: reply.Confidence}
	s.mu.Lock()
	defer s.mu.Unlock()
	p.CreatedAt = s.now().UTC()
	if err := s.append("predictions.jsonl", p); err != nil {
		return Prediction{}, err
	}
	s.predictions[p.ID] = p
	s.order = append(s.order, p.ID)
	return p, nil
}

// Correct records the human's answers against a prediction, once. Every question must be
// answered with one of its options, checked against the schema version the prediction was
// made under, not whatever version is newest now.
//
// Whether an answer kept the top suggestion is derived here, from the stored prediction,
// never taken from the caller.
func (s *Store) Correct(id string, answers map[string]string) (Correction, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.predictions[id]
	if !ok {
		return Correction{}, ErrNotFound
	}
	if _, done := s.corrections[id]; done {
		return Correction{}, ErrAlreadyCorrected
	}
	t := s.templates[p.SchemaID][p.SchemaVersion-1]
	reply, err := ParseReply(p.Prediction, t.Questions)
	if err != nil {
		return Correction{}, fmt.Errorf("prediction %s no longer parses: %w", id, err)
	}
	top := map[string]string{}
	for _, a := range reply.Answers {
		top[a.Question] = a.Options[0].Option
	}
	if len(answers) != len(t.Questions) {
		return Correction{}, fmt.Errorf("%w: answer every question, %d of %d given", ErrInvalid, len(answers), len(t.Questions))
	}
	outcomes := map[string]string{}
	for _, q := range t.Questions {
		v, ok := answers[q.Name]
		if !ok || !contains(q.Options, v) {
			return Correction{}, fmt.Errorf("%w: %q needs one of its options", ErrInvalid, q.Name)
		}
		outcomes[q.Name] = "corrected"
		if v == top[q.Name] {
			outcomes[q.Name] = "accepted"
		}
	}
	c := Correction{PredictionID: id, CorrectedAt: s.now().UTC(), Answers: answers, Outcomes: outcomes}
	if err := s.append("corrections.jsonl", c); err != nil {
		return Correction{}, err
	}
	s.corrections[id] = c
	return c, nil
}

// Get returns a prediction and its correction, if one was recorded.
func (s *Store) Get(id string) (Prediction, *Correction, Template, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.predictions[id]
	if !ok {
		return Prediction{}, nil, Template{}, ErrNotFound
	}
	return p, s.correction(id), s.templates[p.SchemaID][p.SchemaVersion-1], nil
}

// Recent returns up to limit predictions, newest first.
func (s *Store) Recent(limit int) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var ids []string
	for i := len(s.order) - 1; i >= 0 && len(ids) < limit; i-- {
		ids = append(ids, s.order[i])
	}
	return ids
}

func (s *Store) correction(id string) *Correction {
	if c, ok := s.corrections[id]; ok {
		return &c
	}
	return nil
}

// AddEvaluation writes one evaluation result.
func (s *Store) AddEvaluation(e Evaluation) (Evaluation, error) {
	id, err := newID()
	if err != nil {
		return Evaluation{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	e.ID, e.CreatedAt = id, s.now().UTC()
	if err := s.append("evaluations.jsonl", e); err != nil {
		return Evaluation{}, err
	}
	s.evaluations = append(s.evaluations, e)
	return e, nil
}

// Evaluations returns every stored evaluation, newest first.
func (s *Store) Evaluations() []Evaluation {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Evaluation, len(s.evaluations))
	for i, e := range s.evaluations {
		out[len(out)-1-i] = e
	}
	return out
}

// Tally is the capture path's health per schema version: how many predictions there were,
// how many a human answered, and how many answers kept the top suggestion.
type Tally struct {
	Schema      string  `json:"schema"`
	Version     int     `json:"version"`
	Predictions int     `json:"predictions"`
	Corrected   int     `json:"corrected"`
	Accepted    int     `json:"answers_accepted"`
	Changed     int     `json:"answers_corrected"`
	Rate        float64 `json:"correction_rate"`
}

// Tallies summarises the store per schema version.
func (s *Store) Tallies() []Tally {
	s.mu.Lock()
	defer s.mu.Unlock()
	by := map[string]*Tally{}
	var keys []string
	for _, id := range s.order {
		p := s.predictions[id]
		key := fmt.Sprintf("%s@%d", p.SchemaID, p.SchemaVersion)
		t, ok := by[key]
		if !ok {
			t = &Tally{Schema: p.SchemaID, Version: p.SchemaVersion}
			by[key] = t
			keys = append(keys, key)
		}
		t.Predictions++
		if c, ok := s.corrections[id]; ok {
			t.Corrected++
			for _, o := range c.Outcomes {
				if o == "accepted" {
					t.Accepted++
				} else {
					t.Changed++
				}
			}
		}
	}
	sort.Strings(keys)
	out := make([]Tally, 0, len(keys))
	for _, k := range keys {
		t := by[k]
		t.Rate = float64(t.Corrected) / float64(t.Predictions)
		out = append(out, *t)
	}
	return out
}

func contains(options []string, v string) bool {
	for _, o := range options {
		if o == v {
			return true
		}
	}
	return false
}

func newID() (string, error) {
	b := make([]byte, 12)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate an id: %w", err)
	}
	return hex.EncodeToString(b), nil
}
