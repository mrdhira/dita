package dip_test

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/mrdhira/dita/packages/golibs/dip"
	"github.com/mrdhira/dita/packages/golibs/dip/internal/conformance"
)

// types.go is generated from specs/dip/dip.schema.json, so what has to be tested is not the
// code but the agreement: whole response bodies, as the other implementation sends them,
// decoded with the types this package generates. framing.json proves bytes reassemble and
// dispatch.json proves an op is known; neither ever hands a body to a generated type, which
// is how a generated validator that refused every real infer response reached both
// implementations unnoticed.
//
// Nothing here needs to be inside the package: every generated type is exported.

type responsesCorpus struct {
	Corpus   string         `json:"corpus"`
	Protocol int            `json:"protocol"`
	Note     string         `json:"note"`
	Cases    []responseCase `json:"cases"`
}

type responseCase struct {
	Name string          `json:"name"`
	Why  string          `json:"why"`
	Type string          `json:"type"`
	Body json.RawMessage `json:"body"`
}

// responseTypes maps the corpus `type` field onto the generated type it names. A case
// naming something absent from this table is a failure and never a skip: a case that
// quietly does not run leaves the corpus looking green while proving nothing.
var responseTypes = map[string]func() any{
	"InferResponse":     func() any { return new(dip.InferResponse) },
	"HandshakeResponse": func() any { return new(dip.HandshakeResponse) },
	"ListResponse":      func() any { return new(dip.ListResponse) },
	"LoadResponse":      func() any { return new(dip.LoadResponse) },
	"UnloadResponse":    func() any { return new(dip.UnloadResponse) },
	"ProbeResponse":     func() any { return new(dip.ProbeResponse) },
	"ErrorResponse":     func() any { return new(dip.ErrorResponse) },
}

// TestGeneratedTypesRequireTheirFields is this package's own account of the generated
// decoders, held separately from the corpus below: the corpus lives outside this module and
// a checkout without it skips those cases, which would leave every generated UnmarshalJSON
// untested here. The corpus proves Go accepts what Python sends; this proves the generated
// types refuse a body missing a required field rather than decoding it to a zero value,
// which is the only reason to generate them at all.
func TestGeneratedTypesRequireTheirFields(t *testing.T) {
	cases := []struct {
		name    string
		target  func() any
		body    string
		wantErr string
	}{
		{
			name:   "an error response decodes",
			target: func() any { return new(dip.ErrorResponse) },
			body:   `{"ok":false,"error":{"code":"no_model_loaded","message":"nothing is resident"}}`,
		},
		{
			name:    "an error body with no code is refused",
			target:  func() any { return new(dip.ErrorResponse) },
			body:    `{"ok":false,"error":{"message":"nothing is resident"}}`,
			wantErr: "field code in ErrorBody: required",
		},
		{
			name:    "an error code this version does not define is refused",
			target:  func() any { return new(dip.ErrorBody) },
			body:    `{"code":"teleport_failed","message":"prose"}`,
			wantErr: "invalid value",
		},
		{
			name:   "a prologue decodes",
			target: func() any { return new(dip.Prologue) },
			body:   `{"protocol":2,"control_len":14,"payload_len":0}`,
		},
		{
			name:    "a prologue with no payload_len is refused",
			target:  func() any { return new(dip.Prologue) },
			body:    `{"protocol":2,"control_len":14}`,
			wantErr: "payload_len",
		},
		{
			name:   "limits decode",
			target: func() any { return new(dip.Limits) },
			body:   `{"max_chunk":65536,"max_control":8388608,"max_payload":67108864,"idle_timeout_s":300,"message_timeout_s":30}`,
		},
		{
			name:    "limits missing max_chunk are refused",
			target:  func() any { return new(dip.Limits) },
			body:    `{"max_control":8388608,"max_payload":67108864,"idle_timeout_s":300,"message_timeout_s":30}`,
			wantErr: "max_chunk",
		},
		{
			name:   "a line with a four-corner box decodes",
			target: func() any { return new(dip.Line) },
			body:   `{"text":"hello","confidence":0.97,"box":[[1,2],[3,2],[3,4],[1,4]]}`,
		},
		{
			name:    "a line with no text is refused",
			target:  func() any { return new(dip.Line) },
			body:    `{"confidence":0.97,"box":[[1,2],[3,2],[3,4],[1,4]]}`,
			wantErr: "field text in Line: required",
		},
		{
			name:   "a resident model decodes",
			target: func() any { return new(dip.Resident) },
			body:   `{"id":"rapidocr-ppocrv5","engine":"rapidocr","langs":["ja","en"],"resident_seconds":12.5}`,
		},
		{
			name:    "a resident model with no engine is refused",
			target:  func() any { return new(dip.Resident) },
			body:    `{"id":"rapidocr-ppocrv5","langs":["ja"],"resident_seconds":12.5}`,
			wantErr: "engine",
		},
		{
			name:   "a probe response decodes",
			target: func() any { return new(dip.ProbeResponse) },
			body:   `{"ok":true,"probe":"readyz","status":"pass","uptime_s":1.5,"reasons":[]}`,
		},
		{
			name:    "a probe naming an op that is not a probe is refused",
			target:  func() any { return new(dip.ProbeResponse) },
			body:    `{"ok":true,"probe":"infer","status":"pass","uptime_s":1.5,"reasons":[]}`,
			wantErr: "invalid value",
		},
		{
			name:    "a probe status outside pass and fail is refused",
			target:  func() any { return new(dip.ProbeResponse) },
			body:    `{"ok":true,"probe":"readyz","status":"degraded","uptime_s":1.5,"reasons":[]}`,
			wantErr: "invalid value",
		},
		{
			name:   "a handshake request decodes under either name",
			target: func() any { return new(dip.HandshakeRequest) },
			body:   `{"op":"version"}`,
		},
		{
			name:    "a handshake request naming another op is refused",
			target:  func() any { return new(dip.HandshakeRequest) },
			body:    `{"op":"list"}`,
			wantErr: "invalid value",
		},
		{
			name:   "a load request decodes",
			target: func() any { return new(dip.LoadRequest) },
			body:   `{"op":"load","id":"rapidocr-ppocrv5"}`,
		},
		{
			name:   "an infer request decodes",
			target: func() any { return new(dip.InferRequest) },
			body:   `{"op":"infer"}`,
		},
		{
			name:   "a list request decodes",
			target: func() any { return new(dip.ListRequest) },
			body:   `{"op":"list"}`,
		},
		{
			name:   "an unload request decodes",
			target: func() any { return new(dip.UnloadRequest) },
			body:   `{"op":"unload"}`,
		},
		{
			name:   "a probe request decodes",
			target: func() any { return new(dip.ProbeRequest) },
			body:   `{"op":"startupz"}`,
		},
		{
			name:   "a model summary decodes",
			target: func() any { return new(dip.ModelSummary) },
			body: `{"id":"rapidocr-ppocrv5","description":"a model","engine":"rapidocr","langs":["ja"],` +
				`"source_type":"huggingface","files":4,"bytes":21510548,"unverified_files":[]}`,
		},
		{
			name:    "a model summary from a source this version does not know is refused",
			target:  func() any { return new(dip.ModelSummary) },
			body:    `{"id":"x","description":"d","engine":"e","langs":["ja"],"source_type":"ftp","files":1,"bytes":1,"unverified_files":[]}`,
			wantErr: "invalid value",
		},
		{
			name:   "an unload response that evicted nothing decodes",
			target: func() any { return new(dip.UnloadResponse) },
			body:   `{"ok":true,"unloaded":null}`,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			target := tc.target()
			err := json.Unmarshal([]byte(tc.body), target)

			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("the generated %T refused a valid body: %v", target, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("the generated %T accepted %s, which the schema does not describe",
					target, tc.body)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("refusal %q does not mention %q", err, tc.wantErr)
			}
		})
	}
}

func TestResponseCorpus(t *testing.T) {
	var corpus responsesCorpus
	conformance.Load(t, "responses.json", &corpus)

	if corpus.Corpus != "dip-responses" {
		t.Fatalf("corpus is %q, this test reads dip-responses", corpus.Corpus)
	}
	if corpus.Protocol != dip.ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, dip.ProtocolVersion)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}
	assertCorpusCanBite(t, corpus)

	for _, tc := range corpus.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			newTarget, known := responseTypes[tc.Type]
			if !known {
				t.Fatalf("corpus names type %q, this reader does not know it: add it here rather "+
					"than letting the case go unrun", tc.Type)
			}

			target := newTarget()
			if err := json.Unmarshal(tc.Body, target); err != nil {
				t.Fatalf("the generated %s refused a body the other implementation sends: %v",
					tc.Type, err)
			}

			again, err := json.Marshal(target)
			if err != nil {
				t.Fatalf("re-marshal the decoded %s: %v", tc.Type, err)
			}
			assertNothingDropped(t, tc.Body, again)

			switch decoded := target.(type) {
			case *dip.InferResponse:
				assertBoxesSurvived(t, tc.Body, decoded)
			case *dip.HandshakeResponse:
				assertResidentSurvived(t, tc.Body, decoded.Resident)
				assertLoadingSurvived(t, tc.Body, decoded.Loading)
			case *dip.ListResponse:
				assertResidentSurvived(t, tc.Body, decoded.Resident)
				assertLoadingSurvived(t, tc.Body, decoded.Loading)
				if got, want := len(decoded.Models), len(bodyField(t, tc.Body).Models); got != want {
					t.Errorf("decoded %d models, the body carries %d", got, want)
				}
			case *dip.ProbeResponse:
				assertReasonsSurvived(t, tc.Body, decoded.Reasons)
				assertResidentSurvived(t, tc.Body, decoded.Resident)
				assertLoadingSurvived(t, tc.Body, decoded.Loading)
			case *dip.LoadResponse:
				if decoded.Id == "" {
					t.Error("a load response must name the model it loaded")
				}
			case *dip.UnloadResponse, *dip.ErrorResponse:
				// Nothing structural beyond the round trip, which already compared every field.
			default:
				t.Fatalf("no field assertions for %T", decoded)
			}
		})
	}
}

// TestResponseCorpusReaderRejectsAWrongShape is the negative control for the test above:
// a decode that cannot fail would pass the whole corpus while proving nothing. It takes
// the first localised body in the corpus, flattens `box` into the [x, y, x, y, ...] shape
// the schema does not describe, and requires the generated type to refuse it.
func TestResponseCorpusReaderRejectsAWrongShape(t *testing.T) {
	var corpus responsesCorpus
	conformance.Load(t, "responses.json", &corpus)

	for _, tc := range corpus.Cases {
		if tc.Type != "InferResponse" {
			continue
		}
		var body map[string]any
		if err := json.Unmarshal(tc.Body, &body); err != nil {
			t.Fatalf("decode the corpus body: %v", err)
		}
		lines, _ := body["lines"].([]any)
		flattened := false
		for _, line := range lines {
			fields, _ := line.(map[string]any)
			corners, ok := fields["box"].([]any)
			if !ok {
				continue
			}
			flat := make([]any, 0, len(corners)*2)
			for _, corner := range corners {
				point, _ := corner.([]any)
				flat = append(flat, point...)
			}
			fields["box"] = flat
			flattened = true
		}
		if !flattened {
			continue
		}

		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("re-marshal the flattened body: %v", err)
		}
		var target dip.InferResponse
		if err := json.Unmarshal(raw, &target); err == nil {
			t.Fatalf("the generated InferResponse accepted a flattened box %s, so decoding the "+
				"corpus proves nothing about the shape of box", raw)
		}
		return
	}
	t.Fatal("no corpus case carries a box, so the corpus can no longer reproduce the bug it exists for")
}

// assertCorpusCanBite states in the test what the corpus has to contain for the cases to
// mean anything. Shrink the corpus to bodies with no box, no resident and no reasons and
// this fails, rather than every case passing for the wrong reason.
func assertCorpusCanBite(t *testing.T, corpus responsesCorpus) {
	t.Helper()
	var fourCornerBoxes, residents, reasons int
	for _, tc := range corpus.Cases {
		body := bodyField(t, tc.Body)
		for _, line := range body.Lines {
			if len(line.Box) == 4 && len(line.Box[0]) == 2 {
				fourCornerBoxes++
			}
		}
		if len(body.Resident) > 0 && string(body.Resident) != "null" {
			residents++
		}
		reasons += len(body.Reasons)
	}
	if fourCornerBoxes == 0 {
		t.Fatal("no case carries a four-corner box of two-number points, which is exactly the " +
			"shape the generated validator used to refuse")
	}
	if residents == 0 {
		t.Fatal("no case carries a non-null resident, so nothing here proves resident survives")
	}
	if reasons == 0 {
		t.Fatal("no case carries a reason, so nothing here proves reasons survive")
	}
	if _, known := responseTypes[""]; known {
		t.Fatal("the type table answers to the empty type name, so an untyped case would pass")
	}
}

// bodyView is the raw read of a body, used to say what the typed decode was supposed to
// preserve without asking the generated types what they think they preserved.
type bodyView struct {
	Lines []struct {
		Text       string      `json:"text"`
		Confidence *float64    `json:"confidence"`
		Box        [][]float64 `json:"box"`
	} `json:"lines"`
	Models   []json.RawMessage `json:"models"`
	Reasons  []string          `json:"reasons"`
	Resident json.RawMessage   `json:"resident"`
	Loading  *string           `json:"loading"`
}

func bodyField(t *testing.T, body json.RawMessage) bodyView {
	t.Helper()
	var view bodyView
	if err := json.Unmarshal(body, &view); err != nil {
		t.Fatalf("read the corpus body: %v", err)
	}
	return view
}

func assertBoxesSurvived(t *testing.T, body json.RawMessage, decoded *dip.InferResponse) {
	t.Helper()
	view := bodyField(t, body)
	if len(decoded.Lines) != len(view.Lines) {
		t.Fatalf("decoded %d lines, the body carries %d", len(decoded.Lines), len(view.Lines))
	}
	for i, want := range view.Lines {
		got := decoded.Lines[i]
		if got.Text != want.Text {
			t.Errorf("line %d text is %q, the body says %q", i, got.Text, want.Text)
		}
		if want.Box == nil {
			if got.Box != nil {
				t.Errorf("line %d has no box in the body, decoded to %v", i, got.Box)
			}
			continue
		}
		if len(got.Box) != len(want.Box) {
			t.Fatalf("line %d decoded %d corners, the body carries %d", i, len(got.Box), len(want.Box))
		}
		for j, corner := range want.Box {
			if len(got.Box[j]) != len(corner) {
				t.Fatalf("line %d corner %d decoded to %d numbers, the body carries %d",
					i, j, len(got.Box[j]), len(corner))
			}
			if !reflect.DeepEqual([]float64(got.Box[j]), corner) {
				t.Errorf("line %d corner %d is %v, the body says %v", i, j, got.Box[j], corner)
			}
		}
	}
}

func assertResidentSurvived(t *testing.T, body json.RawMessage, decoded any) {
	t.Helper()
	view := bodyField(t, body)
	if len(view.Resident) == 0 || string(view.Resident) == "null" {
		if decoded != nil {
			t.Errorf("the body has no resident, it decoded to %v", decoded)
		}
		return
	}
	if decoded == nil {
		t.Fatalf("the body carries resident %s, the decoded value dropped it", view.Resident)
	}
	fields, ok := decoded.(map[string]any)
	if !ok {
		t.Fatalf("resident decoded to %T, not an object", decoded)
	}
	var want map[string]any
	if err := json.Unmarshal(view.Resident, &want); err != nil {
		t.Fatalf("read the corpus resident: %v", err)
	}
	if !reflect.DeepEqual(fields, want) {
		t.Errorf("resident decoded to %v, the body says %v", fields, want)
	}
}

// assertLoadingSurvived names the field, which makes deleting Loading from the generated
// type a compile error rather than a silent loss: a null the type tags omitempty is
// exempted from the round trip below, so the cases carrying `loading: null` would not
// notice on their own. Three corpus cases carry a model id instead -- a handshake, a list
// and a readyz all taken during a cold load -- and those are where the comparison below
// has a value to disagree about.
func assertLoadingSurvived(t *testing.T, body json.RawMessage, decoded *string) {
	t.Helper()
	want := bodyField(t, body).Loading
	switch {
	case want == nil && decoded != nil:
		t.Errorf("the body has no loading model, it decoded to %q", *decoded)
	case want != nil && decoded == nil:
		t.Errorf("the body says loading %q, the decoded value dropped it", *want)
	case want != nil && *want != *decoded:
		t.Errorf("loading decoded to %q, the body says %q", *decoded, *want)
	}
}

func assertReasonsSurvived(t *testing.T, body json.RawMessage, decoded []string) {
	t.Helper()
	view := bodyField(t, body)
	if !reflect.DeepEqual(decoded, view.Reasons) {
		t.Errorf("reasons decoded to %v, the body says %v", decoded, view.Reasons)
	}
}

// assertNothingDropped compares the body with what the decoded value marshals back to. A
// field the type does not know is dropped here in silence otherwise, which is how a
// response loses `box` or `unloaded` without anyone noticing. The one exemption is a null
// the generated type tags omitempty: absence and null carry the same meaning on this wire.
func assertNothingDropped(t *testing.T, body, again json.RawMessage) {
	t.Helper()
	var want, got any
	if err := json.Unmarshal(body, &want); err != nil {
		t.Fatalf("read the corpus body: %v", err)
	}
	if err := json.Unmarshal(again, &got); err != nil {
		t.Fatalf("read the re-marshalled body: %v", err)
	}
	compareJSON(t, "", want, got)
}

func compareJSON(t *testing.T, path string, want, got any) {
	t.Helper()
	switch wantValue := want.(type) {
	case map[string]any:
		gotValue, ok := got.(map[string]any)
		if !ok {
			t.Errorf("%s is %T after the round trip, the body says object", at(path), got)
			return
		}
		for _, key := range sortedKeys(wantValue) {
			child, present := gotValue[key]
			if !present {
				if wantValue[key] == nil {
					continue // omitempty on a null: the same meaning on this wire.
				}
				t.Errorf("%s was dropped by the round trip; the body says %v",
					at(path+"."+key), wantValue[key])
				continue
			}
			compareJSON(t, path+"."+key, wantValue[key], child)
		}
		for _, key := range sortedKeys(gotValue) {
			if _, present := wantValue[key]; !present {
				t.Errorf("%s appeared in the round trip, the body has no such field",
					at(path+"."+key))
			}
		}
	case []any:
		gotValue, ok := got.([]any)
		if !ok {
			t.Errorf("%s is %T after the round trip, the body says array", at(path), got)
			return
		}
		if len(wantValue) != len(gotValue) {
			t.Errorf("%s has %d items after the round trip, the body has %d",
				at(path), len(gotValue), len(wantValue))
			return
		}
		for i := range wantValue {
			compareJSON(t, fmt.Sprintf("%s[%d]", path, i), wantValue[i], gotValue[i])
		}
	default:
		if !reflect.DeepEqual(want, got) {
			t.Errorf("%s is %v after the round trip, the body says %v", at(path), got, want)
		}
	}
}

func at(path string) string {
	if path == "" {
		return "the body"
	}
	return "field " + path[1:]
}

func sortedKeys(fields map[string]any) []string {
	keys := make([]string, 0, len(fields))
	for key := range fields {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}
