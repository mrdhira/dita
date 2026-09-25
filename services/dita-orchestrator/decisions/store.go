package decisions

import (
	"bufio"
	"bytes"
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
// directory marked with anything else is refused rather than guessed at. retirements.jsonl
// arrived without a new marker: it only adds, and a build that predates it ignores it, so a
// rollback makes retired versions selectable again rather than refusing the store.
const FormatMarker = "dita-decisions/1\n"

// Errors a caller branches on.
var (
	ErrNotFound         = errors.New("not found")
	ErrAlreadyCorrected = errors.New("a correction is already recorded for this prediction; the first stands")
	ErrInvalid          = errors.New("invalid")
	ErrUnreadable       = errors.New("unreadable")
)

// Retirement withdraws one template version from new decisions. The version itself, and every
// prediction made under it, is untouched.
type Retirement struct {
	Name      string    `json:"name"`
	Version   int       `json:"version"`
	RetiredAt time.Time `json:"retired_at"`
}

// Version is a template as listed, with the server's one verdict on it. Usable and Faults are
// CheckQuestions, the rules Decide applies: what the list promises is what a decision does.
// AuthoringIssues are ValidateDraft's, for the editor only: a template saved before an authoring
// rule existed breaks it and still runs.
type Version struct {
	Template
	Retired         bool    `json:"retired"`
	Usable          bool    `json:"usable"`
	Faults          []Issue `json:"faults"`
	AuthoringIssues []Issue `json:"authoring_issues"`
}

// Prediction is one recommendation, written once. `Prediction` is the worker's answer as
// returned and `Confidence` its confidence as returned; neither is recomputed. `Answers` is that
// answer as ParseReply read it at write time, so a later, stricter ParseReply cannot empty
// history; a record written before the field has none and is read under today's rules.
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
	Answers       []Answer           `json:"answers,omitempty"`
}

// Correction is the human's answer to a prediction: a second, separate write, never merged
// into the prediction. Outcomes say, per question, whether the top suggestion was kept.
type Correction struct {
	PredictionID string            `json:"prediction_id"`
	CorrectedAt  time.Time         `json:"corrected_at"`
	Answers      map[string]string `json:"answers"`
	Outcomes     map[string]string `json:"outcomes"`
}

// MaxRecord bounds one stored line, newline included. The reader cannot read a longer one back,
// so the writer refuses to write it: a record Open would refuse must never reach the disk.
const MaxRecord = 16 << 20

// ErrPoisoned is returned by every write after an append failed and could not be undone.
var ErrPoisoned = errors.New("the store stopped taking writes: a failed append could not be rolled back; restart to re-read it")

// appendFile is what append needs of a file; *os.File has it, and a test substitutes a failing one.
type appendFile interface {
	Write([]byte) (int, error)
	Sync() error
	Truncate(int64) error
	Stat() (os.FileInfo, error)
	Close() error
}

// Store is the append-only store. Every write is one JSON line, appended and synced before
// memory changes, so what a reader sees is always on disk. The sync is not asserted by any
// test: its absence is only observable across a power loss. One process owns a directory.
type Store struct {
	mu          sync.Mutex
	dir         string
	now         func() time.Time
	open        func(path string) (appendFile, error)
	poisoned    error
	templates   map[string][]Template
	predictions map[string]Prediction
	order       []string
	corrections map[string]Correction
	evaluations []Evaluation
	retirements map[string]Retirement
	rejected    []Rejection
}

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
	s := &Store{dir: dir, now: time.Now, open: openAppend, templates: map[string][]Template{},
		predictions: map[string]Prediction{}, corrections: map[string]Correction{}, retirements: map[string]Retirement{}}
	if err := s.replay(); err != nil {
		return nil, err
	}
	return s, nil
}

// replay reads every file. templates.jsonl, and the retirements that act on it, are fatal: a
// template silently misread becomes the rules the whole history is read under, and the file is
// tiny and written a handful of times in its life, so correctness beats availability there and a
// human repairs it. Predictions, corrections and evaluations are quarantined instead
// (quarantine.go): availability beats correctness where the volume and the uploads are, and a
// dropped line costs one record, reported on /stats and in the log.
func (s *Store) replay() error {
	if err := readLines(s.dir, "templates.jsonl", func(t Template) error {
		if t.Version != len(s.templates[t.Name])+1 {
			return fmt.Errorf("template %s version %d out of sequence", t.Name, t.Version)
		}
		s.templates[t.Name] = append(s.templates[t.Name], t)
		return nil
	}); err != nil {
		return err
	}
	return errors.Join(
		readQuarantined(s, "predictions.jsonl", func(p Prediction) error {
			if p.SchemaVersion < 1 || p.SchemaVersion > len(s.templates[p.SchemaID]) {
				return fmt.Errorf("prediction %s names template %s version %d, which was never written", p.ID, p.SchemaID, p.SchemaVersion)
			}
			if _, dup := s.predictions[p.ID]; dup {
				return fmt.Errorf("prediction %s written twice", p.ID)
			}
			s.predictions[p.ID] = p
			s.order = append(s.order, p.ID)
			return nil
		}),
		readQuarantined(s, "corrections.jsonl", func(c Correction) error {
			if _, ok := s.predictions[c.PredictionID]; !ok {
				return fmt.Errorf("a correction for prediction %s, which was never written", c.PredictionID)
			}
			if _, dup := s.corrections[c.PredictionID]; dup {
				return fmt.Errorf("prediction %s corrected twice", c.PredictionID)
			}
			s.corrections[c.PredictionID] = c
			return nil
		}),
		readQuarantined(s, "evaluations.jsonl", func(e Evaluation) error {
			s.evaluations = append(s.evaluations, e)
			return nil
		}),
		readLines(s.dir, "retirements.jsonl", func(r Retirement) error {
			if r.Version < 1 || r.Version > len(s.templates[r.Name]) {
				return fmt.Errorf("template %s version %d is retired but was never written", r.Name, r.Version)
			}
			key := versionKey(r.Name, r.Version)
			if _, dup := s.retirements[key]; dup {
				return fmt.Errorf("template %s version %d retired twice", r.Name, r.Version)
			}
			s.retirements[key] = r
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
	scanner.Buffer(make([]byte, 1<<20), MaxRecord)
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

func openAppend(path string) (appendFile, error) {
	return os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o640)
}

// encode is one record as its stored line. HTML is not escaped: `<` would otherwise cost six
// bytes on disk, and a line is bounded.
func encode(record any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(record); err != nil {
		return nil, err
	}
	if b.Len() > MaxRecord {
		return nil, fmt.Errorf("%w: the record is %d bytes, a stored line is at most %d", ErrInvalid, b.Len(), MaxRecord)
	}
	return b.Bytes(), nil
}

// append writes one line and syncs it. A write or sync that fails is cut back to the size the
// file had, so the next line never lands after a fragment; if that fails too, the store is
// poisoned and refuses every later write until a restart re-reads what is on disk.
func (s *Store) append(name string, record any) error {
	if s.poisoned != nil {
		return s.poisoned
	}
	line, err := encode(record)
	if err != nil {
		return err
	}
	f, err := s.open(filepath.Join(s.dir, name))
	if err != nil {
		return fmt.Errorf("open %s: %w", name, err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return fmt.Errorf("stat %s: %w", name, err)
	}
	_, err = f.Write(line)
	if err == nil {
		err = f.Sync()
	}
	if err == nil {
		return nil
	}
	if terr := f.Truncate(info.Size()); terr != nil {
		s.poisoned = fmt.Errorf("%w (%s: %v, then truncate: %v)", ErrPoisoned, name, err, terr)
		return s.poisoned
	}
	return fmt.Errorf("append %s: %w", name, err)
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

// Templates returns the latest version of every template, by name, retired or not.
func (s *Store) Templates() []Version {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Version, 0, len(s.templates))
	for _, versions := range s.templates {
		out = append(out, s.version(versions[len(versions)-1]))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Versions returns every version of one template, oldest first.
func (s *Store) Versions(name string) ([]Version, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	versions, ok := s.templates[name]
	if !ok {
		return nil, ErrNotFound
	}
	out := make([]Version, len(versions))
	for i, t := range versions {
		out[i] = s.version(t)
	}
	return out, nil
}

// Template returns one version of one template.
func (s *Store) Template(name string, version int) (Version, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	versions := s.templates[name]
	if version < 1 || version > len(versions) {
		return Version{}, ErrNotFound
	}
	return s.version(versions[version-1]), nil
}

// Retire withdraws a version from new decisions, once: retiring it again returns the first
// retirement and writes nothing.
func (s *Store) Retire(name string, version int) (Retirement, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if version < 1 || version > len(s.templates[name]) {
		return Retirement{}, ErrNotFound
	}
	key := versionKey(name, version)
	if r, done := s.retirements[key]; done {
		return r, nil
	}
	r := Retirement{Name: name, Version: version, RetiredAt: s.now().UTC()}
	if err := s.append("retirements.jsonl", r); err != nil {
		return Retirement{}, err
	}
	s.retirements[key] = r
	return r, nil
}

func (s *Store) version(t Template) Version {
	_, retired := s.retirements[versionKey(t.Name, t.Version)]
	faults := CheckQuestions(t.Questions)
	return Version{Template: t, Retired: retired, Usable: len(faults) == 0, Faults: nonNil(faults),
		AuthoringIssues: nonNil(ValidateDraft(Draft{Name: t.Name, Description: t.Description, Questions: t.Questions}))}
}

func nonNil(issues []Issue) []Issue {
	if issues == nil {
		return []Issue{}
	}
	return issues
}

func versionKey(name string, version int) string { return fmt.Sprintf("%s@%d", name, version) }

// AddPrediction writes a prediction. It is called only after the worker answered.
func (s *Store) AddPrediction(t Template, text string, raw []byte, reply Reply) (Prediction, error) {
	id, err := newID()
	if err != nil {
		return Prediction{}, err
	}
	p := Prediction{ID: id, SchemaID: t.Name, SchemaVersion: t.Version, InputText: text,
		ModelID: reply.ModelID, ModelRevision: reply.ModelRevision, Prediction: json.RawMessage(raw),
		Confidence: reply.Confidence, Answers: reply.Answers}
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
	stored, _, err := ReadAnswers(p, t)
	if err != nil {
		return Correction{}, err
	}
	top := map[string]string{}
	for _, a := range stored {
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

// ReadAnswers is a prediction's answers: as stored at write time, or, for a record older than
// that field, parsed now under today's rules, which reparsed reports. An answer today's rules
// refuse is ErrUnreadable, never an empty list.
func ReadAnswers(p Prediction, t Template) (answers []Answer, reparsed bool, err error) {
	if p.Answers != nil {
		return p.Answers, false, nil
	}
	reply, err := ParseReply(p.Prediction, t.Questions)
	if err != nil {
		return nil, true, fmt.Errorf("%w: prediction %s was stored before answers were kept, and today's rules refuse it: %v", ErrUnreadable, p.ID, err)
	}
	return reply.Answers, true, nil
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
	Accepted    int     `json:"storedaccepted"`
	Changed     int     `json:"storedcorrected"`
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
		key := versionKey(p.SchemaID, p.SchemaVersion)
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
