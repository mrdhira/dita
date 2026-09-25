package decisions

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"dita-orchestrator/decisions"
	"dita-orchestrator/handler/inferences"
)

const stubReply = `{"model_id":"stub-system-one","model_revision":"STUB-not-a-model","answers":[
	{"name":"severity","probabilities":{"low":0.15,"medium":0.25,"high":0.6},"confidence":0.6},
	{"name":"fraud","probabilities":{"true":0.3,"false":0.7},"confidence":0.7}]}`

var draft = `{"name":"alert-triage","description":"","questions":[
	{"name":"severity","type":"choice","options":["low","medium","high"],"criteria":"How severe is it?"},
	{"name":"fraud","type":"noul","options":["true","false"],"criteria":"Is this fraud?"}]}`

type env struct {
	t      *testing.T
	dir    string
	mux    http.Handler
	worker *url.URL
}

// newEnv serves every decisions route over a real store in a temp directory, with the
// system-one worker at workerURL.
func newEnv(t *testing.T, dir string, workerURL *url.URL) *env {
	t.Helper()
	store, err := decisions.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	h := New(store, inferences.Worker{Name: inferences.SystemOne, URL: workerURL}, 300*time.Millisecond,
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	mux := http.NewServeMux()
	mux.HandleFunc("GET /schemas", h.Templates)
	mux.HandleFunc("POST /schemas", h.SaveTemplate)
	mux.HandleFunc("GET /schemas/{name}/versions", h.Versions)
	mux.HandleFunc("GET /schemas/{name}/versions/{version}", h.Template)
	mux.HandleFunc("POST /schemas/{name}/versions/{version}/retire", h.Retire)
	mux.HandleFunc("POST /decisions", h.Decide)
	mux.HandleFunc("GET /decisions", h.Recent)
	mux.HandleFunc("GET /decisions/{id}", h.Decision)
	mux.HandleFunc("POST /decisions/{id}/correction", h.Correct)
	mux.HandleFunc("POST /evaluations", h.Evaluate)
	mux.HandleFunc("GET /evaluations", h.Evaluations)
	mux.HandleFunc("GET /stats", h.Stats)
	return &env{t: t, dir: dir, mux: mux, worker: workerURL}
}

func (e *env) call(method, path, body string) (int, map[string]any, string) {
	e.t.Helper()
	rec := httptest.NewRecorder()
	e.mux.ServeHTTP(rec, httptest.NewRequest(method, path, strings.NewReader(body)))
	var doc map[string]any
	json.Unmarshal(rec.Body.Bytes(), &doc)
	return rec.Code, doc, rec.Body.String()
}

func (e *env) predictionsOnDisk() int {
	b, err := os.ReadFile(filepath.Join(e.dir, "predictions.jsonl"))
	if os.IsNotExist(err) {
		return 0
	}
	if err != nil {
		e.t.Fatal(err)
	}
	return bytes.Count(b, []byte("\n"))
}

func worker(t *testing.T, status int, body string) *url.URL {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/decide" {
			t.Errorf("the worker was asked for %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL)
	return u
}

func closedPort(t *testing.T) *url.URL {
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	u, _ := url.Parse("http://" + l.Addr().String())
	l.Close()
	return u
}

const decide = `{"text":"Alert 4411: three failed logins then a transfer","schema":{"name":"alert-triage","version":1}}`

func TestNothingIsWrittenUnlessTheWorkerAnswered(t *testing.T) {
	noModel := `{"error":"no model is loaded; the orchestrator has not loaded one yet","error_type":"Unhealthy"}`
	cases := []struct {
		name    string
		worker  *url.URL
		status  int
		body    string
		outcome string
	}{
		{"no model resident: the worker's 503, unchanged", worker(t, 503, noModel), 503, noModel, "no_model"},
		{"a schema the worker refuses: its 400", worker(t, 400, `{"error":"bad schema"}`), 400, `{"error":"bad schema"}`, "schema_invalid"},
		{"an engine refusal: its 422", worker(t, 422, `{"error":"refused"}`), 422, `{"error":"refused"}`, "engine_refused"},
		{"the worker's one slot is taken: its 429", worker(t, 429, `{"error":"Model is overloaded","error_type":"Overloaded"}`), 429, "Overloaded", "busy"},
		{"the worker is not running", closedPort(t), 503, `"reason":"not_running"`, "not_running"},
		{"an answer to a different question", worker(t, 200, `{"model_revision":"r","answers":[]}`), 502, "answered 0 questions", "bad_reply"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEnv(t, t.TempDir(), c.worker)
			if code, _, _ := e.call("POST", "/schemas", draft); code != 201 {
				t.Fatalf("saving the schema: %d", code)
			}
			code, _, body := e.call("POST", "/decisions", decide)
			if code != c.status || !strings.Contains(body, c.body) {
				t.Fatalf("got %d %s, want %d containing %s", code, body, c.status, c.body)
			}
			if n := e.predictionsOnDisk(); n != 0 {
				t.Fatalf("%d predictions written for a decision that never happened", n)
			}
			_, stats, _ := e.call("GET", "/stats", "")
			if stats["outcomes"].(map[string]any)[c.outcome] != 1.0 {
				t.Fatalf("outcome %s not counted: %v", c.outcome, stats["outcomes"])
			}
		})
	}
}

func TestPasteDecideCorrectReload(t *testing.T) {
	dir := t.TempDir()
	up := worker(t, 200, stubReply)
	e := newEnv(t, dir, up)
	e.call("POST", "/schemas", draft)

	code, made, body := e.call("POST", "/decisions", decide)
	if code != 201 {
		t.Fatalf("decide: %d %s", code, body)
	}
	id := made["id"].(string)
	answers := made["answers"].([]any)
	first := answers[0].(map[string]any)
	options := first["options"].([]any)
	if first["question"] != "severity" || first["confidence"] != 0.6 || len(options) != 3 ||
		options[0].(map[string]any)["option"] != "high" || options[1].(map[string]any)["option"] != "medium" {
		t.Fatalf("answer not every option best first with its confidence: %v", first)
	}
	if made["correction"] != nil || made["model_revision"] != "STUB-not-a-model" {
		t.Fatalf("new prediction %v", made)
	}

	code, corrected, body := e.call("POST", "/decisions/"+id+"/correction", `{"answers":{"severity":"high","fraud":"true"}}`)
	if code != 201 {
		t.Fatalf("correct: %d %s", code, body)
	}
	outcomes := corrected["correction"].(map[string]any)["outcomes"].(map[string]any)
	if outcomes["severity"] != "accepted" || outcomes["fraud"] != "corrected" {
		t.Fatalf("outcomes %v", outcomes)
	}
	code, _, body = e.call("POST", "/decisions/"+id+"/correction", `{"answers":{"severity":"low","fraud":"false"}}`)
	if code != 409 || !strings.Contains(body, "the first stands") {
		t.Fatalf("second correction: %d %s", code, body)
	}

	// A new process over the same directory: the pair is still attached.
	reloaded := newEnv(t, dir, up)
	_, got, _ := reloaded.call("GET", "/decisions/"+id, "")
	c, _ := got["correction"].(map[string]any)
	if c == nil || c["answers"].(map[string]any)["severity"] != "high" || c["answers"].(map[string]any)["fraud"] != "true" {
		t.Fatalf("after reload the correction is %v", got["correction"])
	}
	_, recent, _ := reloaded.call("GET", "/decisions?limit=5", "")
	if list := recent["decisions"].([]any); len(list) != 1 || list[0].(map[string]any)["id"] != id {
		t.Fatalf("recent %v", recent)
	}
	_, stats, _ := reloaded.call("GET", "/stats", "")
	tally := stats["schemas"].([]any)[0].(map[string]any)
	if tally["predictions"] != 1.0 || tally["corrected"] != 1.0 || tally["correction_rate"] != 1.0 {
		t.Fatalf("tally %v", tally)
	}
}

func TestWhatTheRoutesRefuse(t *testing.T) {
	e := newEnv(t, t.TempDir(), worker(t, 200, stubReply))
	e.call("POST", "/schemas", draft)
	cases := []struct {
		name, method, path, body string
		status                   int
		mentions                 string
	}{
		{"an invalid schema, with zod's paths", "POST", "/schemas", `{"name":"X","questions":[]}`, 400, `"path":"questions"`},
		{"an unknown field", "POST", "/schemas", `{"name":"ab","questions":[],"extra":1}`, 400, "unknown field"},
		{"blank text", "POST", "/decisions", `{"text":"   ","schema":{"name":"alert-triage","version":1}}`, 400, `"path":"text"`},
		{"an unknown template", "POST", "/decisions", `{"text":"x","schema":{"name":"nope","version":1}}`, 404, "not found"},
		{"an unknown version", "GET", "/schemas/alert-triage/versions/9", "", 404, "not found"},
		{"a version that is not a number", "GET", "/schemas/alert-triage/versions/one", "", 400, "number"},
		{"a correction to nothing", "POST", "/decisions/nope/correction", `{"answers":{}}`, 404, "not found"},
		{"a limit out of range", "GET", "/decisions?limit=0", "", 400, "limit"},
		{"a limit over the page", "GET", "/decisions?limit=51", "", 400, "limit is 1-50"},
		{"the largest page", "GET", "/decisions?limit=50", "", 200, "decisions"},
		{"trailing data after a correction", "POST", "/decisions/nope/correction", `{"answers":{}} {}`, 400, "trailing data"},
		{"trailing data after an evaluation", "POST", "/evaluations", `{"name":"x","rows":[]} {}`, 400, "trailing data"},
		{"an unknown evaluation field", "POST", "/evaluations", `{"name":"x","rows":[],"model":"m"}`, 400, "unknown field"},
		{"an evaluation whose rows are not a list", "POST", "/evaluations", `{"name":"x","rows":{}}`, 400, "expected ["},
		{"an evaluation with a label it does not score", "POST", "/evaluations",
			`{"name":"x","rows":[{"label":"c","probabilities":{"a":0.5,"b":0.5}}]}`, 400, "labelled"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			code, _, body := e.call(c.method, c.path, c.body)
			if code != c.status || !strings.Contains(body, c.mentions) {
				t.Fatalf("got %d %s, want %d mentioning %s", code, body, c.status, c.mentions)
			}
		})
	}
}

func TestTemplatesAndEvaluationsRoundTrip(t *testing.T) {
	dir := t.TempDir()
	e := newEnv(t, dir, worker(t, 200, stubReply))
	for want := 1; want <= 2; want++ {
		code, tpl, _ := e.call("POST", "/schemas", draft)
		if code != 201 || tpl["version"] != float64(want) {
			t.Fatalf("save %d: %d %v", want, code, tpl)
		}
	}
	_, versions, _ := e.call("GET", "/schemas/alert-triage/versions", "")
	if n := len(versions["versions"].([]any)); n != 2 {
		t.Fatalf("%d versions", n)
	}
	code, eval, body := e.call("POST", "/evaluations", `{"name":"alerts","rows":[
		{"label":"a","probabilities":{"a":0.9,"b":0.1}},{"label":"b","probabilities":{"a":0.2,"b":0.8}}]}`)
	if code != 201 || eval["accuracy"] != 1.0 || eval["baseline"] == nil {
		t.Fatalf("evaluate: %d %s", code, body)
	}
	_, list, _ := newEnv(t, dir, e.worker).call("GET", "/evaluations", "")
	if n := len(list["evaluations"].([]any)); n != 1 {
		t.Fatalf("%d evaluations after reload", n)
	}
}

// counting is a worker that answers stubReply and counts what it was asked.
func counting(t *testing.T, calls *atomic.Int32) *url.URL {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(stubReply))
	}))
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL)
	return u
}

func TestARetiredVersionIsRefusedWithoutAskingTheWorker(t *testing.T) {
	var calls atomic.Int32
	e := newEnv(t, t.TempDir(), counting(t, &calls))
	e.call("POST", "/schemas", draft)
	e.call("POST", "/schemas", draft)

	code, first, body := e.call("POST", "/schemas/alert-triage/versions/1/retire", "")
	if code != 200 || first["name"] != "alert-triage" || first["version"] != 1.0 || first["retired"] != true || first["retired_at"] == nil {
		t.Fatalf("retire: %d %s", code, body)
	}
	code, again, _ := e.call("POST", "/schemas/alert-triage/versions/1/retire", "")
	if code != 200 || again["retired_at"] != first["retired_at"] {
		t.Fatalf("retiring twice: %d %v, want 200 and the first timestamp %v", code, again["retired_at"], first["retired_at"])
	}

	_, versions, _ := e.call("GET", "/schemas/alert-triage/versions", "")
	list := versions["versions"].([]any)
	if list[0].(map[string]any)["retired"] != true || list[1].(map[string]any)["retired"] != false {
		t.Fatalf("versions %v", list)
	}
	_, latest, _ := e.call("GET", "/schemas", "")
	if tpl := latest["templates"].([]any)[0].(map[string]any); tpl["version"] != 2.0 || tpl["retired"] != false {
		t.Fatalf("templates %v", tpl)
	}

	code, refused, body := e.call("POST", "/decisions", decide)
	if code != 410 || refused["error_type"] != "Retired" || !strings.Contains(body, "version 1 is retired: pick another version") {
		t.Fatalf("deciding on a retired version: %d %s", code, body)
	}
	if calls.Load() != 0 || e.predictionsOnDisk() != 0 {
		t.Fatalf("a retired version reached the worker %d times", calls.Load())
	}
	code, _, body = e.call("POST", "/decisions", strings.Replace(decide, `"version":1`, `"version":2`, 1))
	if code != 201 || calls.Load() != 1 {
		t.Fatalf("the live version: %d %s after %d worker calls", code, body, calls.Load())
	}

	for _, c := range []struct {
		path     string
		status   int
		mentions string
	}{
		{"/schemas/alert-triage/versions/3/retire", 404, "not found"},
		{"/schemas/nope/versions/1/retire", 404, "not found"},
		{"/schemas/alert-triage/versions/one/retire", 400, "number"},
	} {
		if code, _, body := e.call("POST", c.path, ""); code != c.status || !strings.Contains(body, c.mentions) {
			t.Fatalf("POST %s: %d %s, want %d", c.path, code, body, c.status)
		}
	}
}

// alert-triage as the deployed store holds it: v1 saved before the noul rule, v2 before criteria
// were required. The worker refuses v1 and runs v2.
const legacyTemplates = `{"name":"alert-triage","version":1,"description":"","questions":[{"name":"severity","type":"choice","options":["info","warning","critical"]},{"name":"escalate","type":"noul","options":["yes","no","unknown"]}],"created_at":"2026-09-20T10:00:00Z"}
{"name":"alert-triage","version":2,"description":"","questions":[{"name":"severity","type":"choice","options":["low","medium","high"]},{"name":"fraud","type":"noul","options":["true","false"]}],"created_at":"2026-09-21T10:00:00Z"}
`

func TestTheListSaysWhatADecisionWillDo(t *testing.T) {
	dir := t.TempDir()
	if _, err := decisions.Open(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "templates.jsonl"), []byte(legacyTemplates), 0o640); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	e := newEnv(t, dir, counting(t, &calls))

	_, listed, _ := e.call("GET", "/schemas/alert-triage/versions", "")
	versions := listed["versions"].([]any)
	_, latest, _ := e.call("GET", "/schemas", "")
	for _, v := range append(append([]any{}, versions...), latest["templates"].([]any)...) {
		fields := v.(map[string]any)
		for _, key := range []string{"retired", "usable", "faults", "authoring_issues"} {
			if _, ok := fields[key]; !ok {
				t.Fatalf("version %v is listed without %q", fields["version"], key)
			}
		}
		for _, list := range []string{"faults", "authoring_issues"} {
			for _, issue := range fields[list].([]any) {
				if kv := issue.(map[string]any); len(kv) != 2 || kv["path"] == nil || kv["message"] == nil {
					t.Fatalf("%s entry %v is not {path, message}", list, kv)
				}
			}
		}
	}
	v1, v2 := versions[0].(map[string]any), versions[1].(map[string]any)
	if v1["usable"] != false || v1["retired"] != false || v1["faults"].([]any)[0].(map[string]any)["path"] != "questions.1.options" {
		t.Fatalf("v1 %v", v1)
	}
	if v2["usable"] != true || len(v2["faults"].([]any)) != 0 {
		t.Fatalf("v2, which the worker runs, is listed as %v %v", v2["usable"], v2["faults"])
	}
	authoring := v2["authoring_issues"].([]any)
	if len(authoring) != 2 || authoring[0].(map[string]any)["path"] != "questions.0.criteria" {
		t.Fatalf("v2's authoring issues %v, want the two missing criteria", authoring)
	}

	code, refused, body := e.call("POST", "/decisions", decide)
	issues, _ := refused["issues"].([]any)
	if code != 400 || refused["error_type"] != "Validation" || len(issues) == 0 ||
		issues[0].(map[string]any)["path"] != "questions.1.options" {
		t.Fatalf("deciding on v1: %d %s", code, body)
	}
	if calls.Load() != 0 || e.predictionsOnDisk() != 0 {
		t.Fatalf("the worker was asked %d times for what the list already says it refuses", calls.Load())
	}
	_, stats, _ := e.call("GET", "/stats", "")
	if stats["outcomes"].(map[string]any)["schema_invalid"] != 1.0 {
		t.Fatalf("outcomes %v", stats["outcomes"])
	}

	code, _, body = e.call("POST", "/decisions", strings.Replace(decide, `"version":1`, `"version":2`, 1))
	if code != 201 || calls.Load() != 1 {
		t.Fatalf("v2, listed usable, did not run: %d %s", code, body)
	}
}

func sameJSON(t *testing.T, a, b string) bool {
	t.Helper()
	var x, y any
	if json.Unmarshal([]byte(a), &x) != nil || json.Unmarshal([]byte(b), &y) != nil {
		return false
	}
	return reflect.DeepEqual(x, y)
}

func evaluationBody(rows int) string {
	var b strings.Builder
	b.WriteString(`{"name":"bulk","rows":[`)
	for i := range rows {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(`{"label":"a","probabilities":{"a":0.9,"b":0.1}}`)
	}
	b.WriteString("]}")
	return b.String()
}

func TestAnEvaluationIsBoundedBeforeItIsBuilt(t *testing.T) {
	e := newEnv(t, t.TempDir(), worker(t, 200, stubReply))
	if code, _, body := e.call("POST", "/evaluations", evaluationBody(decisions.MaxRows)); code != 201 {
		t.Fatalf("exactly MaxRows rows: %d %s", code, body)
	}
	if code, _, body := e.call("POST", "/evaluations", evaluationBody(decisions.MaxRows+1)); code != 400 || !strings.Contains(body, "between 1 and 10000 rows") {
		t.Fatalf("MaxRows+1 rows: %d %s", code, body)
	}

	huge := evaluationBody(3 * decisions.MaxRows)
	reader := &countingReader{r: strings.NewReader(huge)}
	if _, _, err := decodeEvaluation(reader); err == nil {
		t.Fatal("three times MaxRows decoded")
	}
	if reader.n >= len(huge)/2 {
		t.Fatalf("read %d of %d bytes: the rows were built before the cap was checked", reader.n, len(huge))
	}
}

type countingReader struct {
	r *strings.Reader
	n int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += n
	return n, err
}

// The limits are written here as numbers, not read from the constants, so raising a constant
// fails this test instead of growing its fixture.
func TestBodiesOverTheirLimitAreRefusedUnread(t *testing.T) {
	e := newEnv(t, t.TempDir(), worker(t, 200, stubReply))
	e.call("POST", "/schemas", draft)
	sized := func(prefix, suffix string, size int) string {
		return prefix + strings.Repeat("x", size-len(prefix)-len(suffix)) + suffix
	}
	for _, c := range []struct {
		path, prefix, suffix string
		limit                int
	}{
		{"/decisions", `{"text":"`, `","schema":{"name":"alert-triage","version":1}}`, 1 << 20},
		{"/evaluations", `{"name":"`, `","rows":[]}`, 8 << 20},
	} {
		over := sized(c.prefix, c.suffix, c.limit+1)
		if code, _, body := e.call("POST", c.path, over); code != 400 || !strings.Contains(body, "too large") {
			t.Fatalf("POST %s of %d bytes, one over %d: %d %.200s", c.path, len(over), c.limit, code, body)
		}
		under := sized(c.prefix, c.suffix, c.limit)
		if _, _, body := e.call("POST", c.path, under); strings.Contains(body, "too large") {
			t.Fatalf("POST %s of exactly %d bytes was refused as too large", c.path, len(under))
		}
	}
}

func TestOneEvaluationAtATime(t *testing.T) {
	store, _ := decisions.Open(t.TempDir())
	h := New(store, inferences.Worker{Name: inferences.SystemOne, URL: closedPort(t)}, time.Second, slog.New(slog.NewTextHandler(io.Discard, nil)))
	post := func() *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.Evaluate(rec, httptest.NewRequest("POST", "/evaluations", strings.NewReader(evaluationBody(2))))
		return rec
	}
	h.scoring <- struct{}{}
	if rec := post(); rec.Code != 429 || !strings.Contains(rec.Body.String(), "Overloaded") {
		t.Fatalf("while one is scored: %d %s", rec.Code, rec.Body)
	}
	<-h.scoring
	if rec := post(); rec.Code != 201 {
		t.Fatalf("once it finished: %d %s", rec.Code, rec.Body)
	}
	if rec := post(); rec.Code != 201 {
		t.Fatalf("the slot was not given back: %d %s", rec.Code, rec.Body)
	}
}

func TestOneUnreadableRecordNeverFailsTheHistory(t *testing.T) {
	dir := t.TempDir()
	e := newEnv(t, dir, worker(t, 200, stubReply))
	e.call("POST", "/schemas", draft)
	e.call("POST", "/decisions", decide)
	f, _ := os.OpenFile(filepath.Join(dir, "predictions.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"id":"old","schema_id":"alert-triage","schema_version":1,"prediction":{"model_revision":"r","answers":[]}}` + "\n")
	f.Close()
	reloaded := newEnv(t, dir, e.worker)

	_, stats, _ := reloaded.call("GET", "/stats", "")
	if q := stats["quarantined"].(map[string]any); q["predictions.jsonl"] != 0.0 || q["corrections.jsonl"] != 0.0 || q["evaluations.jsonl"] != 0.0 {
		t.Fatalf("a readable-but-stale record was quarantined: %v", q)
	}
	code, recent, body := reloaded.call("GET", "/decisions?limit=5", "")
	list, _ := recent["decisions"].([]any)
	if code != 200 || len(list) != 2 {
		t.Fatalf("history with one unreadable record: %d %s", code, body)
	}
	old, fresh := list[0].(map[string]any), list[1].(map[string]any)
	if old["answers"] != nil || old["answers_reparsed"] != true || !strings.Contains(old["answers_error"].(string), "today's rules refuse it") {
		t.Fatalf("the unreadable record renders as %v", old)
	}
	if fresh["answers_reparsed"] != false || fresh["answers_error"] != nil || len(fresh["answers"].([]any)) != 2 {
		t.Fatalf("the readable record renders as %v", fresh)
	}
	if code, _, body := reloaded.call("POST", "/decisions/old/correction", `{"answers":{"severity":"low","fraud":"true"}}`); code != 422 || !strings.Contains(body, "Unreadable") {
		t.Fatalf("correcting it: %d %s", code, body)
	}
}

// A directory where the file should be makes every append fail, for root as well.
func TestACorrectionThatCannotBeWrittenIsNotReportedAsMade(t *testing.T) {
	dir := t.TempDir()
	e := newEnv(t, dir, worker(t, 200, stubReply))
	e.call("POST", "/schemas", draft)
	_, made, _ := e.call("POST", "/decisions", decide)
	id := made["id"].(string)
	if err := os.Mkdir(filepath.Join(dir, "corrections.jsonl"), 0o750); err != nil {
		t.Fatal(err)
	}
	if code, _, body := e.call("POST", "/decisions/"+id+"/correction", `{"answers":{"severity":"high","fraud":"true"}}`); code != 500 || !strings.Contains(body, `"error_type":"Backend"`) {
		t.Fatalf("a correction that was never written: %d %s", code, body)
	}
	if _, got, _ := e.call("GET", "/decisions/"+id, ""); got["correction"] != nil {
		t.Fatalf("a correction that was never written is attached: %v", got["correction"])
	}
}

func TestAQuarantinedLineIsCountedOnStats(t *testing.T) {
	dir := t.TempDir()
	e := newEnv(t, dir, worker(t, 200, stubReply))
	e.call("POST", "/schemas", draft)
	e.call("POST", "/decisions", decide)
	f, _ := os.OpenFile(filepath.Join(dir, "predictions.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString("{damaged\n")
	f.Close()
	e.call("POST", "/evaluations", `{"name":"kept","rows":[{"label":"a","probabilities":{"a":0.9,"b":0.1}}]}`)
	f, _ = os.OpenFile(filepath.Join(dir, "evaluations.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString("{damaged too\n")
	f.Close()

	reloaded := newEnv(t, dir, e.worker)
	_, stats, body := reloaded.call("GET", "/stats", "")
	q, _ := stats["quarantined"].(map[string]any)
	if q["predictions.jsonl"] != 1.0 || q["corrections.jsonl"] != 0.0 || q["evaluations.jsonl"] != 1.0 {
		t.Fatalf("stats after one damaged line in each of two files: %s", body)
	}
	if _, evals, _ := reloaded.call("GET", "/evaluations", ""); len(evals["evaluations"].([]any)) != 1 {
		t.Fatalf("the good evaluation is not served: %v", evals)
	}
	if _, recent, _ := reloaded.call("GET", "/decisions", ""); len(recent["decisions"].([]any)) != 1 {
		t.Fatalf("the good history is not served: %v", recent)
	}
}
