package decisions

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func TestTheSharedSchemaCases(t *testing.T) {
	raw, err := os.ReadFile("../../../specs/decisions/schema-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []struct {
			Name     string   `json:"name"`
			Template Draft    `json:"template"`
			Issues   []string `json:"issues"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) < 20 {
		t.Fatalf("only %d cases read: the fixture did not load", len(file.Cases))
	}
	for _, c := range file.Cases {
		t.Run(c.Name, func(t *testing.T) {
			got := []string{}
			for _, issue := range ValidateDraft(c.Template) {
				got = append(got, issue.Path)
			}
			sort.Strings(got)
			if !reflect.DeepEqual(got, append([]string{}, c.Issues...)) {
				t.Fatalf("issue paths %v, the shared case says %v", got, c.Issues)
			}
		})
	}
}

var triage = Draft{Name: "alert-triage", Questions: []Question{
	{Name: "severity", Type: "choice", Options: []string{"low", "medium", "high"}},
	{Name: "fraud", Type: "noul", Options: []string{"true", "false"}},
}}

// reply is what the stub worker answers for triage: medium, then no.
var reply = []byte(`{"model_id":"stub","model_revision":"rev-1","answers":[
	{"name":"severity","probabilities":{"low":0.2,"medium":0.7,"high":0.1},"confidence":0.7},
	{"name":"fraud","probabilities":{"true":0.4,"false":0.6},"confidence":0.6}]}`)

func predict(t *testing.T, s *Store, tpl Template) Prediction {
	t.Helper()
	parsed, err := ParseReply(reply, tpl.Questions)
	if err != nil {
		t.Fatal(err)
	}
	p, err := s.AddPrediction(tpl, "pasted state", reply, parsed)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestASecondCorrectionIsRefusedAndTheFirstStandsOnDisk(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	tpl, _ := s.SaveTemplate(triage)
	p := predict(t, s, tpl)

	first, err := s.Correct(p.ID, map[string]string{"severity": "medium", "fraud": "true"})
	if err != nil {
		t.Fatal(err)
	}
	if want := map[string]string{"severity": "accepted", "fraud": "corrected"}; !reflect.DeepEqual(first.Outcomes, want) {
		t.Fatalf("outcomes %v, want %v", first.Outcomes, want)
	}
	_, err = s.Correct(p.ID, map[string]string{"severity": "low", "fraud": "false"})
	if !errors.Is(err, ErrAlreadyCorrected) {
		t.Fatalf("second correction: %v, want ErrAlreadyCorrected", err)
	}

	// A new process reads the same pair: the first correction, untouched, beside its prediction.
	again, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	got, c, _, err := again.Get(p.ID)
	if err != nil || c == nil {
		t.Fatalf("after reopening: %v %v", err, c)
	}
	if c.Answers["severity"] != "medium" || c.Answers["fraud"] != "true" {
		t.Fatalf("the first correction did not stand: %v", c.Answers)
	}
	// As returned means the worker's content, not its whitespace: JSON lines are compact.
	var want bytes.Buffer
	json.Compact(&want, reply)
	if string(got.Prediction) != want.String() || got.ModelRevision != "rev-1" {
		t.Fatalf("the prediction changed: %s", got.Prediction)
	}
	lines := strings.Count(read(t, dir, "corrections.jsonl"), "\n")
	if lines != 1 {
		t.Fatalf("corrections.jsonl has %d lines, want exactly the first", lines)
	}
}

func TestACorrectionIsCheckedAgainstTheVersionItWasMadeUnder(t *testing.T) {
	s, _ := Open(t.TempDir())
	v1, _ := s.SaveTemplate(triage)
	p := predict(t, s, v1)
	v2draft := triage
	v2draft.Questions = []Question{{Name: "severity", Type: "choice", Options: []string{"critical", "minor"}}}
	if _, err := s.SaveTemplate(v2draft); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name    string
		answers map[string]string
		err     error
	}{
		{"an option only the newer version has", map[string]string{"severity": "critical", "fraud": "false"}, ErrInvalid},
		{"a question left out", map[string]string{"severity": "low"}, ErrInvalid},
		{"an unknown prediction", nil, ErrNotFound},
		{"every answer from the version it was made under", map[string]string{"severity": "high", "fraud": "false"}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			id := p.ID
			if c.err == ErrNotFound {
				id = "no-such-id"
			}
			_, err := s.Correct(id, c.answers)
			if !errors.Is(err, c.err) && !(err == nil && c.err == nil) {
				t.Fatalf("err %v, want %v", err, c.err)
			}
		})
	}
}

func TestTemplatesAreVersionedNeverRewritten(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	v1, _ := s.SaveTemplate(triage)
	changed := triage
	changed.Description = "second"
	v2, _ := s.SaveTemplate(changed)
	if v1.Version != 1 || v2.Version != 2 {
		t.Fatalf("versions %d, %d", v1.Version, v2.Version)
	}
	again, _ := Open(dir)
	old, err := again.Template("alert-triage", 1)
	if err != nil || old.Description != "" {
		t.Fatalf("version 1 changed: %+v %v", old, err)
	}
	if latest := again.Templates(); len(latest) != 1 || latest[0].Version != 2 {
		t.Fatalf("latest %+v", latest)
	}
	if _, err := again.SaveTemplate(Draft{Name: "Bad"}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("an invalid draft was saved: %v", err)
	}
}

func TestAStoreItCannotTrustIsRefused(t *testing.T) {
	cases := []struct {
		name, file, content, mentions string
	}{
		{"another layout's marker", "FORMAT", "dita-decisions/2\n", "this build reads"},
		{"a line that does not parse", "predictions.jsonl", "{\"id\":\"a\"}\n{not json\n", "predictions.jsonl:2"},
		{"a prediction corrected twice on disk", "corrections.jsonl",
			"{\"prediction_id\":\"a\"}\n{\"prediction_id\":\"a\"}\n", "corrected twice"},
		{"a template version out of sequence", "templates.jsonl", "{\"name\":\"x\",\"version\":2}\n", "out of sequence"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			if _, err := Open(dir); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, c.file), []byte(c.content), 0o640); err != nil {
				t.Fatal(err)
			}
			if _, err := Open(dir); err == nil || !strings.Contains(err.Error(), c.mentions) {
				t.Fatalf("err %v, want one mentioning %q", err, c.mentions)
			}
		})
	}
}

func TestParseReplyRefusesAnAnswerToADifferentQuestion(t *testing.T) {
	ok := func(s string) string { return strings.ReplaceAll(s, "'", `"`) }
	cases := []struct {
		name, body, mentions string
	}{
		{"not JSON", "<html>", "not the expected JSON"},
		{"no revision", ok(`{'answers':[]}`), "model_revision"},
		{"a question missing", ok(`{'model_revision':'r','answers':[{'name':'severity','probabilities':{'low':0.2,'medium':0.7,'high':0.1},'confidence':0.7}]}`), "answered 1 questions, 2 were asked"},
		{"an option missing", ok(`{'model_revision':'r','answers':[{'name':'severity','probabilities':{'low':0.3,'medium':0.7},'confidence':0.7},{'name':'fraud','probabilities':{'true':0.4,'false':0.6},'confidence':0.6}]}`), "scored 2 options"},
		{"a probability above 1", ok(`{'model_revision':'r','answers':[{'name':'severity','probabilities':{'low':1.2,'medium':0.7,'high':0.1},'confidence':0.7},{'name':'fraud','probabilities':{'true':0.4,'false':0.6},'confidence':0.6}]}`), "outside [0, 1]"},
		{"no confidence", ok(`{'model_revision':'r','answers':[{'name':'severity','probabilities':{'low':0.2,'medium':0.7,'high':0.1}},{'name':'fraud','probabilities':{'true':0.4,'false':0.6},'confidence':0.6}]}`), "confidence"},
		{"an act_probability above 1", ok(`{'model_revision':'r','answers':[{'name':'severity','probabilities':{'low':0.2,'medium':0.7,'high':0.1},'confidence':0.7,'act_probability':1.5},{'name':'fraud','probabilities':{'true':0.4,'false':0.6},'confidence':0.6}]}`), "act_probability"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, err := ParseReply([]byte(c.body), triage.Questions); err == nil || !strings.Contains(err.Error(), c.mentions) {
				t.Fatalf("err %v, want one mentioning %q", err, c.mentions)
			}
		})
	}
	got, err := ParseReply(reply, triage.Questions)
	if err != nil {
		t.Fatal(err)
	}
	if o := got.Answers[0].Options; o[0].Option != "medium" || o[1].Option != "low" || o[2].Option != "high" {
		t.Fatalf("options not best first: %+v", o)
	}
	if got.Confidence["fraud"] != 0.6 {
		t.Fatalf("confidence recomputed: %v", got.Confidence)
	}
}

// systemOneReply is inferences-system-one's answer as it serves it: unrounded probabilities
// that sum to 1, confidence the top probability, its escalate head beside them, and a noul
// keyed false and true, the only labels that worker answers a noul in.
var systemOneQuestions = []Question{
	{Name: "severity", Type: "choice", Options: []string{"low", "medium", "high"}},
	{Name: "needs_human", Type: "noul", Options: []string{"false", "true"}},
}

var systemOneReply = []byte(`{"model_id":"laya-multilingual","model_revision":"b4a904d1a2a54c822b829e24291d4b8f280fe43e",
	"answers":[{"name":"severity","probabilities":{"low":0.011218,"medium":0.850674,"high":0.138108},"confidence":0.850674,"act_probability":0.9999},
	{"name":"needs_human","probabilities":{"true":0.017179,"false":0.982821},"confidence":0.982821,"act_probability":1}]}`)

func TestParseReplyReadsTheSystemOneWorkersAnswer(t *testing.T) {
	got, err := ParseReply(systemOneReply, systemOneQuestions)
	if err != nil {
		t.Fatal(err)
	}
	if got.ModelID != "laya-multilingual" || got.ModelRevision != "b4a904d1a2a54c822b829e24291d4b8f280fe43e" {
		t.Fatalf("model %q at %q", got.ModelID, got.ModelRevision)
	}
	if o := got.Answers[0].Options; o[0].Option != "medium" || o[0].Probability != 0.850674 || o[2].Option != "low" {
		t.Fatalf("severity not every option best first: %+v", o)
	}
	if got.Confidence["severity"] != 0.850674 || got.Confidence["needs_human"] != 0.982821 {
		t.Fatalf("confidence not as served: %v", got.Confidence)
	}
}

func TestEvaluate(t *testing.T) {
	rows := []Row{
		{Label: "a", Probabilities: map[string]float64{"a": 0.9, "b": 0.1}},
		{Label: "a", Probabilities: map[string]float64{"a": 0.6, "b": 0.4}},
		{Label: "b", Probabilities: map[string]float64{"a": 0.7, "b": 0.3}},
		{Label: "a", Probabilities: map[string]float64{"a": 0.2, "b": 0.8}},
	}
	e, err := Evaluate("four rows", rows)
	if err != nil {
		t.Fatal(err)
	}
	// Worked by hand: predictions a, a, a, b against labels a, a, b, a.
	near := func(name string, got, want float64) {
		if math.Abs(got-want) > 1e-9 {
			t.Errorf("%s = %v, want %v", name, got, want)
		}
	}
	near("accuracy", e.Accuracy, 0.5)
	near("brier", e.Brier, (0.02+0.32+0.98+1.28)/4)
	// Bins: 0.9 (hit), 0.6 (hit), 0.7 (miss), 0.8 (miss); each bin holds one row.
	near("ece", e.ECE, (0.1+0.4+0.7+0.8)/4)
	near("baseline accuracy", e.Baseline.Accuracy, 0.75)
	near("baseline brier", e.Baseline.Brier, 1-(0.75*0.75+0.25*0.25))
	if e.Baseline.Class != "a" || e.BeatsBase {
		t.Fatalf("baseline %+v, beats %v", e.Baseline, e.BeatsBase)
	}
	if c := e.Classes[1]; c.Class != "b" || c.Support != 1 || c.Predicted != 1 || c.Correct != 0 || *c.Recall != 0 {
		t.Fatalf("class b %+v", c)
	}

	// 0.71 and 0.79 share the [0.7, 0.8) bin: one hit, one miss, so the bin's gap is
	// |0.5 - 0.75|. Split into two bins the same rows would give (0.29 + 0.79) / 2.
	binned, err := Evaluate("one bin", []Row{
		{Label: "a", Probabilities: map[string]float64{"a": 0.71, "b": 0.29}},
		{Label: "b", Probabilities: map[string]float64{"a": 0.79, "b": 0.21}},
	})
	if err != nil {
		t.Fatal(err)
	}
	near("ece within one bin", binned.ECE, 0.25)

	bad := []struct {
		name     string
		rows     []Row
		mentions string
	}{
		{"no rows", nil, "between 1 and"},
		{"a label it does not score", []Row{{Label: "c", Probabilities: map[string]float64{"a": 0.5, "b": 0.5}}}, "labelled \"c\""},
		{"probabilities that do not sum to 1", []Row{{Label: "a", Probabilities: map[string]float64{"a": 0.5, "b": 0.2}}}, "sum to"},
		{"rows scoring different classes", []Row{rows[0], {Label: "a", Probabilities: map[string]float64{"a": 1}}}, "scores 1 classes"},
	}
	for _, c := range bad {
		t.Run(c.name, func(t *testing.T) {
			if _, err := Evaluate("x", c.rows); err == nil || !strings.Contains(err.Error(), c.mentions) {
				t.Fatalf("err %v, want one mentioning %q", err, c.mentions)
			}
		})
	}
}

func read(t *testing.T, dir, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestCriteriaReachTheWorkerAsWritten(t *testing.T) {
	asked := DecideRequest{Text: "oom", Questions: []Question{
		{Name: "severity", Type: "choice", Options: []string{"info", "critical"}, Criteria: "How severe is this alert?"},
		{Name: "needs_human", Type: "noul", Options: []string{"false", "true"}},
	}}
	body, err := json.Marshal(asked)
	if err != nil {
		t.Fatal(err)
	}
	var sent struct {
		Questions []map[string]any `json:"questions"`
	}
	if err := json.Unmarshal(body, &sent); err != nil {
		t.Fatal(err)
	}
	if got := sent.Questions[0]["criteria"]; got != "How severe is this alert?" {
		t.Fatalf("criteria sent as %v", got)
	}
	if _, present := sent.Questions[1]["criteria"]; present {
		t.Fatalf("an empty criteria must be omitted, the worker then falls back to the name: %s", body)
	}
}
