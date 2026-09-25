package decisions

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
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
	{Name: "severity", Type: "choice", Options: []string{"low", "medium", "high"}, Criteria: "How severe is it?"},
	{Name: "fraud", Type: "noul", Options: []string{"true", "false"}, Criteria: "Is this fraud?"},
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
	v2draft.Questions = []Question{{Name: "severity", Type: "choice", Options: []string{"critical", "minor"}, Criteria: "How bad?"}}
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
		{"an extra answer beside every real one", map[string]string{"severity": "high", "fraud": "false", "extra": "x"}, ErrInvalid},
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

// The files that hold the rules are fatal: a misread template becomes the rules every
// prediction is read under.
func TestAStoreWhoseRulesCannotBeTrustedIsRefused(t *testing.T) {
	cases := []struct {
		name     string
		files    map[string]string
		mentions string
	}{
		{"another layout's marker", map[string]string{"FORMAT": "dita-decisions/2\n"}, "this build reads"},
		{"a template line that does not parse", map[string]string{"templates.jsonl": `{"name":"x","version":1}` + "\n{not json\n"}, "templates.jsonl:2"},
		{"a template version out of sequence", map[string]string{"templates.jsonl": `{"name":"x","version":2}` + "\n"}, "out of sequence"},
		{"a retirement of a version never written", map[string]string{"retirements.jsonl": `{"name":"x","version":1}` + "\n"},
			"never written"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			if _, err := Open(dir); err != nil {
				t.Fatal(err)
			}
			for name, content := range c.files {
				if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o640); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := Open(dir); err == nil || !strings.Contains(err.Error(), c.mentions) {
				t.Fatalf("err %v, want one mentioning %q", err, c.mentions)
			}
		})
	}
}

// A record line that cannot be read costs that record, never the store: it is kept byte for
// byte in a sidecar, counted, and the lines after it are still read.
func TestAnUnreadableRecordIsQuarantinedNotFatal(t *testing.T) {
	const template = `{"name":"x","version":1,"questions":[]}` + "\n"
	p := func(id string, version int) string {
		return fmt.Sprintf(`{"id":%q,"schema_id":"x","schema_version":%d}`, id, version)
	}
	c := func(id string) string { return fmt.Sprintf(`{"prediction_id":%q}`, id) }
	cases := []struct {
		name, file, bad string
		predictions, ok []string
		corrections     string
		wantPredictions int
		wantCorrected   []string
	}{
		{name: "a line that does not parse", file: "predictions.jsonl", bad: `{"id":"b" invalid}`,
			predictions: []string{p("a", 1), "BAD", p("c", 1)}, wantPredictions: 2},
		{name: "a duplicate id: the first stands", file: "predictions.jsonl", bad: `{"id":"a","schema_id":"x","schema_version":1,"input_text":"second"}`,
			predictions: []string{p("a", 1), "BAD", p("c", 1)}, wantPredictions: 2},
		{name: "a template version never written", file: "predictions.jsonl", bad: p("b", 7),
			predictions: []string{p("a", 1), "BAD", p("c", 1)}, wantPredictions: 2},
		{name: "a correction with no prediction", file: "corrections.jsonl", bad: c("ghost"),
			predictions: []string{p("a", 1), p("c", 1)}, corrections: c("a") + "\nBAD\n" + c("c") + "\n",
			wantPredictions: 2, wantCorrected: []string{"a", "c"}},
		{name: "a second correction: the first stands", file: "corrections.jsonl", bad: `{"prediction_id":"a","answers":{"q":"second"}}`,
			predictions: []string{p("a", 1), p("c", 1)}, corrections: c("a") + "\nBAD\n" + c("c") + "\n",
			wantPredictions: 2, wantCorrected: []string{"a", "c"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			Open(dir)
			predictions := strings.ReplaceAll(strings.Join(tc.predictions, "\n")+"\n", "BAD", tc.bad)
			files := map[string]string{"templates.jsonl": template, "predictions.jsonl": predictions,
				"corrections.jsonl": strings.ReplaceAll(tc.corrections, "BAD", tc.bad)}
			for name, content := range files {
				os.WriteFile(filepath.Join(dir, name), []byte(content), 0o640)
			}
			for open := 1; open <= 2; open++ {
				s, err := Open(dir)
				if err != nil {
					t.Fatalf("open %d: the store refused itself over one record: %v", open, err)
				}
				if n := len(s.Recent(10)); n != tc.wantPredictions {
					t.Fatalf("open %d: %d predictions served, want %d", open, n, tc.wantPredictions)
				}
				if _, _, _, err := s.Get("c"); err != nil {
					t.Fatalf("open %d: the good line after the rejected one was not read: %v", open, err)
				}
				for _, id := range tc.wantCorrected {
					if _, corr, _, _ := s.Get(id); corr == nil {
						t.Fatalf("open %d: correction of %s after the rejected line was not read", open, id)
					}
				}
				if q := s.Quarantined(); q[tc.file] != 1 || len(q) != len(quarantined) {
					t.Fatalf("open %d: quarantined %v, want 1 in %s", open, q, tc.file)
				}
				if r := s.Rejections(); len(r) != 1 || r[0].Line != 2 || r[0].Reason == "" {
					t.Fatalf("open %d: rejections %+v, want line 2 with a reason", open, r)
				}
				if got := read(t, dir, tc.file+".rejected"); got != tc.bad+"\n" {
					t.Fatalf("open %d: the sidecar holds %q, want exactly the rejected bytes once", open, got)
				}
			}
		})
	}
}

func TestATornLastLineIsSetAsideAndTheNextAppendStartsClean(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	tpl, _ := s.SaveTemplate(triage)
	predict(t, s, tpl)
	f, _ := os.OpenFile(filepath.Join(dir, "predictions.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"id":"torn","schema_id":"alert`)
	f.Close()

	reopened, err := Open(dir)
	if err != nil || reopened.Quarantined()["predictions.jsonl"] != 1 {
		t.Fatalf("a torn tail: %v, quarantined %v", err, reopened.Quarantined())
	}
	if got := read(t, dir, "predictions.jsonl.rejected"); got != `{"id":"torn","schema_id":"alert`+"\n" {
		t.Fatalf("sidecar %q", got)
	}
	predict(t, reopened, tpl)
	again, err := Open(dir)
	if err != nil || len(again.Recent(10)) != 2 || again.Quarantined()["predictions.jsonl"] != 1 {
		t.Fatalf("the append after a torn tail was lost: %v, %d served", err, len(again.Recent(10)))
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

func TestARetiredVersionStaysRetiredAndIsNeverRewritten(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	s.SaveTemplate(triage)
	s.SaveTemplate(triage)
	clock := time.Date(2026, 9, 25, 10, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return clock }

	first, err := s.Retire("alert-triage", 1)
	if err != nil || first.RetiredAt != clock {
		t.Fatalf("retire: %+v %v", first, err)
	}
	clock = clock.Add(time.Hour)
	again, err := s.Retire("alert-triage", 1)
	if err != nil || again.RetiredAt != first.RetiredAt {
		t.Fatalf("retiring twice gave %+v %v, want the first retirement back", again, err)
	}
	if lines := strings.Count(read(t, dir, "retirements.jsonl"), "\n"); lines != 1 {
		t.Fatalf("retirements.jsonl has %d lines, want 1", lines)
	}
	for _, c := range []struct {
		name    string
		version int
	}{{"an unknown version", 3}, {"version zero", 0}} {
		if _, err := s.Retire("alert-triage", c.version); !errors.Is(err, ErrNotFound) {
			t.Fatalf("%s: %v, want ErrNotFound", c.name, err)
		}
	}
	if _, err := s.Retire("nope", 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("an unknown template: %v", err)
	}

	reopened, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	versions, _ := reopened.Versions("alert-triage")
	if !versions[0].Retired || versions[1].Retired {
		t.Fatalf("after reopening, retired flags %v %v, want true false", versions[0].Retired, versions[1].Retired)
	}
	if v, _ := reopened.Template("alert-triage", 1); !v.Retired || len(v.Questions) != 2 {
		t.Fatalf("version 1 after retirement: %+v", v)
	}
	if latest := reopened.Templates(); latest[0].Version != 2 || latest[0].Retired {
		t.Fatalf("latest %+v", latest[0])
	}
	if lines := strings.Count(read(t, dir, "templates.jsonl"), "\n"); lines != 2 {
		t.Fatalf("templates.jsonl has %d lines: retiring rewrote a template", lines)
	}

	if err := os.WriteFile(filepath.Join(dir, "retirements.jsonl"), []byte(read(t, dir, "retirements.jsonl")+
		`{"name":"alert-triage","version":1}`+"\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir); err == nil || !strings.Contains(err.Error(), "retired twice") {
		t.Fatalf("a version retired twice on disk was opened: %v", err)
	}
}

// The worker boundary, read by this suite and the worker's: see specs/decisions/worker-cases.json.
func TestTheSharedWorkerCases(t *testing.T) {
	raw, err := os.ReadFile("../../../specs/decisions/worker-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Requests []struct {
			Name     string          `json:"name"`
			Request  json.RawMessage `json:"request"`
			Accepted bool            `json:"accepted"`
			Refused  *struct {
				ErrorType string `json:"error_type"`
				Path      string `json:"path"`
			} `json:"refused"`
		} `json:"requests"`
		Replies []struct {
			Name      string          `json:"name"`
			Questions []Question      `json:"questions"`
			Reply     string          `json:"reply"`
			Answers   json.RawMessage `json:"answers"`
			Error     string          `json:"error"`
		} `json:"replies"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	ranged := map[bool]int{}
	for _, c := range file.Requests {
		if bytes.Contains(c.Request, []byte(`"range"`)) {
			ranged[c.Accepted]++
		}
	}
	if ranged[true] == 0 || ranged[false] == 0 || !strings.Contains(string(raw), `act_probability\": 1.5`) || len(file.Replies) < 5 {
		t.Fatalf("the fixture must hold a score range accepted and refused (%v) and an act_probability out of range", ranged)
	}
	for _, c := range file.Requests {
		t.Run("request/"+c.Name, func(t *testing.T) {
			if c.Accepted == (c.Refused != nil) {
				t.Fatal("a request case is either accepted or refused")
			}
			var req DecideRequest
			if err := json.Unmarshal(c.Request, &req); err != nil {
				t.Fatal(err)
			}
			rebuilt, _ := json.Marshal(req)
			if !sameJSON(t, rebuilt, c.Request) {
				t.Fatalf("the adapter would send %s, not the fixture's %s", rebuilt, c.Request)
			}
			faults := CheckQuestions(req.Questions)
			if c.Accepted {
				if len(faults) != 0 {
					t.Fatalf("the worker runs it, CheckQuestions refuses it: %v", faults)
				}
				return
			}
			if c.Refused.ErrorType != "Validation" || len(faults) == 0 || faults[0].Path != c.Refused.Path {
				t.Fatalf("CheckQuestions says %v, the worker refuses at %s", faults, c.Refused.Path)
			}
		})
	}
	for _, c := range file.Replies {
		t.Run("reply/"+c.Name, func(t *testing.T) {
			got, err := ParseReply([]byte(c.Reply), c.Questions)
			if c.Error != "" {
				if err == nil || !strings.Contains(err.Error(), c.Error) {
					t.Fatalf("err %v, want one mentioning %q", err, c.Error)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			read, _ := json.Marshal(got.Answers)
			if !sameJSON(t, read, c.Answers) {
				t.Fatalf("read %s, want %s", read, c.Answers)
			}
		})
	}
}

func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatal(err)
	}
	return reflect.DeepEqual(x, y)
}

func TestTheWriterNeverWritesALineTheReaderRefuses(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	base, err := encode(Evaluation{ID: "e"})
	if err != nil {
		t.Fatal(err)
	}
	largest := Evaluation{ID: "e", Name: strings.Repeat("x", MaxRecord-len(base))}
	line, err := encode(largest)
	if err != nil || len(line) != MaxRecord {
		t.Fatalf("the largest record encodes to %d bytes (%v), want exactly MaxRecord %d", len(line), err, MaxRecord)
	}
	if err := os.WriteFile(filepath.Join(dir, "evaluations.jsonl"), line, 0o640); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(dir)
	if err != nil {
		t.Fatalf("the reader refuses a line the writer accepts: %v", err)
	}
	if got := reopened.Evaluations(); len(got) != 1 || len(got[0].Name) != len(largest.Name) {
		t.Fatalf("read back %d evaluations", len(got))
	}

	largest.Name += "x"
	if err := s.append("evaluations.jsonl", largest); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a record one byte over MaxRecord: %v, want ErrInvalid", err)
	}
	if n := len(read(t, dir, "evaluations.jsonl")); n != MaxRecord {
		t.Fatalf("the refused record changed the file to %d bytes", n)
	}

	if escaped, _ := encode(map[string]string{"t": "<&>"}); !bytes.Contains(escaped, []byte("<&>")) {
		t.Fatalf("stored text is escaped: %s", escaped)
	}
}

func TestAFailedAppendLeavesMemoryAndDiskAsTheyWere(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root writes a read-only file")
	}
	dir := t.TempDir()
	s, _ := Open(dir)
	tpl, _ := s.SaveTemplate(triage)
	first := predict(t, s, tpl)
	path := filepath.Join(dir, "predictions.jsonl")
	if err := os.Chmod(path, 0o440); err != nil {
		t.Fatal(err)
	}
	parsed, _ := ParseReply(reply, tpl.Questions)
	if _, err := s.AddPrediction(tpl, "second", reply, parsed); err == nil {
		t.Fatal("a prediction was accepted into a file that cannot be written")
	}
	if ids := s.Recent(10); len(ids) != 1 || ids[0] != first.ID {
		t.Fatalf("memory changed although the append failed: %v", ids)
	}
}

// tornFile writes half of what it is given and fails; truncate fails too when told to.
type tornFile struct {
	*os.File
	truncateFails bool
}

func (f tornFile) Write(b []byte) (int, error) {
	n, _ := f.File.Write(b[:len(b)/2])
	return n, errors.New("disk full")
}

func (f tornFile) Truncate(size int64) error {
	if f.truncateFails {
		return errors.New("i/o error")
	}
	return f.File.Truncate(size)
}

func TestATornAppendIsCutBackOrStopsTheStore(t *testing.T) {
	for _, c := range []struct {
		name          string
		truncateFails bool
	}{{"cut back, and the next append lands whole", false}, {"not cut back: poisoned until restart", true}} {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			s, _ := Open(dir)
			tpl, _ := s.SaveTemplate(triage)
			predict(t, s, tpl)
			before := len(read(t, dir, "predictions.jsonl"))
			s.open = func(path string) (appendFile, error) {
				f, err := openAppend(path)
				return tornFile{f.(*os.File), c.truncateFails}, err
			}
			parsed, _ := ParseReply(reply, tpl.Questions)
			if _, err := s.AddPrediction(tpl, "torn", reply, parsed); err == nil {
				t.Fatal("a torn append reported success")
			}
			s.open = openAppend
			_, err := s.AddPrediction(tpl, "after", reply, parsed)
			if c.truncateFails {
				if !errors.Is(err, ErrPoisoned) || len(s.Recent(10)) != 1 {
					t.Fatalf("after a fragment that could not be removed: %v, %d in memory", err, len(s.Recent(10)))
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := len(read(t, dir, "predictions.jsonl")); got <= before {
				t.Fatalf("file %d bytes, was %d", got, before)
			}
			reopened, err := Open(dir)
			if err != nil || len(reopened.Recent(10)) != 2 {
				t.Fatalf("after the cut-back the store reopens with %v: %v", reopened, err)
			}
		})
	}
}

func TestAnEvaluationStoresOnlyBoundedText(t *testing.T) {
	row := func(class string) []Row {
		return []Row{{Label: class, Probabilities: map[string]float64{class: 0.9, "b": 0.1}}}
	}
	cases := []struct {
		name, run string
		rows      []Row
		ok        bool
	}{
		{"a name of exactly 200 characters", strings.Repeat("n", MaxEvaluationName), row("a"), true},
		{"a name of 201 characters", strings.Repeat("n", MaxEvaluationName+1), row("a"), false},
		{"a blank name", "  ", row("a"), false},
		{"the audit's store-killer: three million '<'", strings.Repeat("<", 3_000_000), row("a"), false},
		{"a class of exactly 100 characters", "run", row(strings.Repeat("c", MaxOptionLen)), true},
		{"a class of 101 characters", "run", row(strings.Repeat("c", MaxOptionLen+1)), false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := Evaluate(c.run, c.rows)
			if c.ok != (err == nil) || (err != nil && !errors.Is(err, ErrInvalid)) {
				t.Fatalf("err %v, want ok=%v", err, c.ok)
			}
		})
	}
}

func TestTheSharedTextCases(t *testing.T) {
	raw, err := os.ReadFile("../../../specs/decisions/schema-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Texts []struct {
			Name   string   `json:"name"`
			Repeat string   `json:"repeat"`
			Times  int      `json:"times"`
			Issues []string `json:"issues"`
		} `json:"texts"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	at := map[int]bool{}
	for _, c := range file.Texts {
		at[utf8.RuneCountInString(strings.Repeat(c.Repeat, c.Times))] = true
	}
	if !at[MaxText] || !at[MaxText+1] {
		t.Fatalf("the fixture must hold a text of MaxText (%d) and MaxText+1 characters", MaxText)
	}
	for _, c := range file.Texts {
		t.Run(c.Name, func(t *testing.T) {
			got := []string{}
			for _, issue := range ValidateText(strings.Repeat(c.Repeat, c.Times)) {
				got = append(got, issue.Path)
			}
			if !reflect.DeepEqual(got, append([]string{}, c.Issues...)) {
				t.Fatalf("issue paths %v, the shared case says %v", got, c.Issues)
			}
		})
	}
}

func TestAnswersAreReadAsTheyWereWritten(t *testing.T) {
	tpl := Template{Name: "alert-triage", Version: 1, Questions: triage.Questions}
	kept, err := ParseReply(reply, tpl.Questions)
	if err != nil {
		t.Fatal(err)
	}
	refusedToday := json.RawMessage(`{"model_revision":"r","answers":[]}`)
	cases := []struct {
		name     string
		p        Prediction
		reparsed bool
		err      error
	}{
		{"stored answers stand though today's rules refuse the raw reply", Prediction{ID: "a", Prediction: refusedToday, Answers: kept.Answers}, false, nil},
		{"a record from before stored answers is read under today's rules", Prediction{ID: "b", Prediction: reply}, true, nil},
		{"one today's rules refuse is unreadable, not empty", Prediction{ID: "c", Prediction: refusedToday}, true, ErrUnreadable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			answers, reparsed, err := ReadAnswers(c.p, tpl)
			if reparsed != c.reparsed || !errors.Is(err, c.err) || (err == nil && !reflect.DeepEqual(answers, kept.Answers)) {
				t.Fatalf("answers %v reparsed %v err %v", answers, reparsed, err)
			}
		})
	}

	dir := t.TempDir()
	s, _ := Open(dir)
	saved, _ := s.SaveTemplate(triage)
	p := predict(t, s, saved)
	if !strings.Contains(read(t, dir, "predictions.jsonl"), `"answers":[{"question":"severity"`) {
		t.Fatal("a new prediction does not store its parsed answers")
	}
	legacy := `{"id":"old","schema_id":"alert-triage","schema_version":1,"prediction":{"model_revision":"r","answers":[]}}` + "\n"
	f, _ := os.OpenFile(filepath.Join(dir, "predictions.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(legacy)
	f.Close()
	reopened, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.Correct("old", map[string]string{"severity": "low", "fraud": "true"}); !errors.Is(err, ErrUnreadable) {
		t.Fatalf("correcting an unreadable record: %v, want ErrUnreadable", err)
	}
	if _, err := reopened.Correct(p.ID, map[string]string{"severity": "low", "fraud": "true"}); err != nil {
		t.Fatalf("a readable record beside it: %v", err)
	}
}

// Evaluations come from uploads, so their file is where damage is likeliest to be written.
func TestAnUnreadableEvaluationIsQuarantinedNotFatal(t *testing.T) {
	dir := t.TempDir()
	Open(dir)
	const bad = `{"id":"e2","name":"torn`
	lines := `{"id":"e1","name":"first"}` + "\n" + bad + "\n" + `{"id":"e3","name":"after"}` + "\n"
	if err := os.WriteFile(filepath.Join(dir, "evaluations.jsonl"), []byte(lines), 0o640); err != nil {
		t.Fatal(err)
	}
	for open := 1; open <= 2; open++ {
		s, err := Open(dir)
		if err != nil {
			t.Fatalf("open %d: one damaged evaluation refused the store: %v", open, err)
		}
		got := s.Evaluations()
		if len(got) != 2 || got[0].Name != "after" || got[1].Name != "first" {
			t.Fatalf("open %d: served %+v, want first and the line after the damaged one", open, got)
		}
		if q := s.Quarantined(); q["evaluations.jsonl"] != 1 || q["predictions.jsonl"] != 0 {
			t.Fatalf("open %d: quarantined %v", open, q)
		}
		if r := s.Rejections(); len(r) != 1 || r[0].File != "evaluations.jsonl" || r[0].Line != 2 || r[0].Reason == "" {
			t.Fatalf("open %d: rejections %+v", open, r)
		}
		if side := read(t, dir, "evaluations.jsonl.rejected"); side != bad+"\n" {
			t.Fatalf("open %d: sidecar %q, want exactly the damaged bytes once", open, side)
		}
	}
}
