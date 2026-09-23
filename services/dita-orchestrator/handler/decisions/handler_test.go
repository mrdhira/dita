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
	"strings"
	"testing"
	"time"

	"dita-orchestrator/decisions"
	"dita-orchestrator/handler/inferences"
)

const stubReply = `{"model_id":"stub-system-one","model_revision":"STUB-not-a-model","answers":[
	{"name":"severity","probabilities":{"low":0.15,"medium":0.25,"high":0.6},"confidence":0.6},
	{"name":"fraud","probabilities":{"yes":0.3,"no":0.7},"confidence":0.7}]}`

var draft = `{"name":"alert-triage","description":"","questions":[
	{"name":"severity","type":"choice","options":["low","medium","high"]},
	{"name":"fraud","type":"noul","options":["yes","no"]}]}`

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

	code, corrected, body := e.call("POST", "/decisions/"+id+"/correction", `{"answers":{"severity":"high","fraud":"yes"}}`)
	if code != 201 {
		t.Fatalf("correct: %d %s", code, body)
	}
	outcomes := corrected["correction"].(map[string]any)["outcomes"].(map[string]any)
	if outcomes["severity"] != "accepted" || outcomes["fraud"] != "corrected" {
		t.Fatalf("outcomes %v", outcomes)
	}
	code, _, body = e.call("POST", "/decisions/"+id+"/correction", `{"answers":{"severity":"low","fraud":"no"}}`)
	if code != 409 || !strings.Contains(body, "the first stands") {
		t.Fatalf("second correction: %d %s", code, body)
	}

	// A new process over the same directory: the pair is still attached.
	reloaded := newEnv(t, dir, up)
	_, got, _ := reloaded.call("GET", "/decisions/"+id, "")
	c, _ := got["correction"].(map[string]any)
	if c == nil || c["answers"].(map[string]any)["severity"] != "high" || c["answers"].(map[string]any)["fraud"] != "yes" {
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
