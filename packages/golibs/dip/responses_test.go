package dip

import (
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"testing"
)

// The third corpus, specs/dip/conformance/responses.json: whole response bodies decoded
// with the types this package generates from the IDL. framing.json proves bytes
// reassemble and dispatch.json proves an op is known; neither ever hands a body to a
// generated type, which is how a generated validator that refused every real infer
// response reached both implementations unnoticed.

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
	"InferResponse":     func() any { return new(InferResponse) },
	"HandshakeResponse": func() any { return new(HandshakeResponse) },
	"ListResponse":      func() any { return new(ListResponse) },
	"LoadResponse":      func() any { return new(LoadResponse) },
	"UnloadResponse":    func() any { return new(UnloadResponse) },
	"ProbeResponse":     func() any { return new(ProbeResponse) },
	"ErrorResponse":     func() any { return new(ErrorResponse) },
}

func TestResponseCorpus(t *testing.T) {
	var corpus responsesCorpus
	loadCorpus(t, "responses.json", &corpus)

	if corpus.Corpus != "dip-responses" {
		t.Fatalf("corpus is %q, this test reads dip-responses", corpus.Corpus)
	}
	if corpus.Protocol != ProtocolVersion {
		t.Fatalf("corpus is protocol %d, this package speaks %d", corpus.Protocol, ProtocolVersion)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}
	assertCorpusCanBite(t, corpus)

	for _, testCase := range corpus.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			newTarget, known := responseTypes[testCase.Type]
			if !known {
				t.Fatalf("corpus names type %q, this reader does not know it: add it here rather "+
					"than letting the case go unrun", testCase.Type)
			}

			target := newTarget()
			if err := json.Unmarshal(testCase.Body, target); err != nil {
				t.Fatalf("the generated %s refused a body the other implementation sends: %v",
					testCase.Type, err)
			}

			again, err := json.Marshal(target)
			if err != nil {
				t.Fatalf("re-marshal the decoded %s: %v", testCase.Type, err)
			}
			assertNothingDropped(t, testCase.Body, again)

			switch decoded := target.(type) {
			case *InferResponse:
				assertBoxesSurvived(t, testCase.Body, decoded)
			case *HandshakeResponse:
				assertResidentSurvived(t, testCase.Body, decoded.Resident)
				assertLoadingSurvived(t, testCase.Body, decoded.Loading)
			case *ListResponse:
				assertResidentSurvived(t, testCase.Body, decoded.Resident)
				assertLoadingSurvived(t, testCase.Body, decoded.Loading)
				if got, want := len(decoded.Models), len(bodyField(t, testCase.Body).Models); got != want {
					t.Errorf("decoded %d models, the body carries %d", got, want)
				}
			case *ProbeResponse:
				assertReasonsSurvived(t, testCase.Body, decoded.Reasons)
				assertResidentSurvived(t, testCase.Body, decoded.Resident)
				assertLoadingSurvived(t, testCase.Body, decoded.Loading)
			case *LoadResponse:
				if decoded.Id == "" {
					t.Error("a load response must name the model it loaded")
				}
			case *UnloadResponse, *ErrorResponse:
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
	loadCorpus(t, "responses.json", &corpus)

	for _, testCase := range corpus.Cases {
		if testCase.Type != "InferResponse" {
			continue
		}
		var body map[string]any
		if err := json.Unmarshal(testCase.Body, &body); err != nil {
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
		var target InferResponse
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
	for _, testCase := range corpus.Cases {
		body := bodyField(t, testCase.Body)
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

func assertBoxesSurvived(t *testing.T, body json.RawMessage, decoded *InferResponse) {
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

// assertLoadingSurvived names the field, which is the only thing proving it exists: every
// case in the corpus carries `loading: null`, and a null the type tags omitempty is
// exempted from the round trip below, so deleting Loading from the generated type would
// otherwise be invisible. Naming it makes that deletion a compile error. A case with a
// non-null loading would be the stronger fix and belongs in the corpus.
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
	sort.Strings(keys)
	return keys
}
