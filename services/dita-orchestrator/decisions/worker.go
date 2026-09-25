package decisions

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// This file is the only place that knows the worker's answer shape. inferences-system-one
// answers in it (docs/inferences/system-one/[1]technical-requirement.md, "The contract"), and
// adds act_probability, its escalate head: optional, checked when present, never stored apart
// from the raw answer.

// DecideRequest is the body sent to the worker's POST /decide.
type DecideRequest struct {
	Text      string     `json:"text"`
	Questions []Question `json:"questions"`
}

// CheckQuestions is the runtime verdict on a template: the faults for which the worker's
// `parse_decide` would refuse it, so a template it refuses costs no worker slot. It is not
// ValidateDraft, and must not become it: a template saved before an authoring rule existed still
// runs. Each question reports its first fault in the worker's order, so the first fault is the
// one the worker names. specs/decisions/worker-cases.json holds both sides to the same path.
func CheckQuestions(questions []Question) []Issue {
	if len(questions) < 1 || len(questions) > MaxQuestions {
		return []Issue{{Path: "questions", Message: fmt.Sprintf("between 1 and %d questions", MaxQuestions)}}
	}
	var faults []Issue
	seen := map[string]bool{}
	for i, q := range questions {
		at := fmt.Sprintf("questions.%d", i)
		if path, message := checkQuestion(q); path != "" {
			faults = append(faults, Issue{Path: at + path, Message: message})
		} else if seen[q.Name] {
			faults = append(faults, Issue{Path: at + ".name", Message: fmt.Sprintf("question %q appears twice", q.Name)})
		}
		seen[q.Name] = true
	}
	return faults
}

func checkQuestion(q Question) (path, message string) {
	switch {
	case q.Name == "":
		return ".name", "a question needs a name"
	case q.Type != "choice" && q.Type != "score" && q.Type != "noul":
		return ".type", "type must be one of choice, score, noul"
	case len(q.Options) < MinOptions || len(q.Options) > MaxOptions || slices.ContainsFunc(q.Options, blank):
		return ".options", fmt.Sprintf("%d to %d options, none blank", MinOptions, MaxOptions)
	case len(q.Options) != len(set(q.Options)):
		return ".options", "an option appears twice"
	case q.Type == "noul" && !falseTrue(q.Options):
		return ".options", fmt.Sprintf("a noul is answered only as false and true, so its options are exactly those, not %q", q.Options)
	case q.Range == nil:
		return "", ""
	case q.Type != "score":
		return ".range", "only a score question has a range"
	case !(q.Range.Min < q.Range.Max):
		return ".range", "min must be below max"
	}
	levels, ok := numeric(q.Options)
	if ok && (!sort.Float64sAreSorted(levels) || levels[0] < q.Range.Min || levels[len(levels)-1] > q.Range.Max) {
		return ".range", fmt.Sprintf("the range %v to %v contradicts the levels %q: they must rise within it", q.Range.Min, q.Range.Max, q.Options)
	}
	return "", ""
}

// numeric reads options as numbers the way the worker's float() does for plain spellings; a
// single word makes the levels words, which no range can contradict.
func numeric(options []string) ([]float64, bool) {
	levels := make([]float64, len(options))
	for i, o := range options {
		v, err := strconv.ParseFloat(strings.TrimSpace(o), 64)
		if err != nil {
			return nil, false
		}
		levels[i] = v
	}
	return levels, true
}

func blank(s string) bool { return strings.TrimSpace(s) == "" }

func set(options []string) map[string]bool {
	out := map[string]bool{}
	for _, o := range options {
		out[o] = true
	}
	return out
}

type workerAnswer struct {
	Name           string             `json:"name"`
	Probabilities  map[string]float64 `json:"probabilities"`
	Confidence     *float64           `json:"confidence"`
	ActProbability *float64           `json:"act_probability"`
}

type workerReply struct {
	ModelID       string         `json:"model_id"`
	ModelRevision string         `json:"model_revision"`
	Answers       []workerAnswer `json:"answers"`
}

// OptionScore is one option and the probability the worker gave it.
type OptionScore struct {
	Option      string  `json:"option"`
	Probability float64 `json:"probability"`
}

// Answer is one question's answer as the dashboard renders it: every option, best first,
// and the worker's own confidence. Never a bare value.
type Answer struct {
	Question   string        `json:"question"`
	Type       string        `json:"type"`
	Options    []OptionScore `json:"options"`
	Confidence float64       `json:"confidence"`
}

// Reply is a worker answer checked against the schema it was asked.
type Reply struct {
	ModelID       string
	ModelRevision string
	Answers       []Answer
	Confidence    map[string]float64
}

// ParseReply reads the worker's body and refuses one that does not answer the questions it
// was asked, so a malformed answer can never be stored as a prediction.
func ParseReply(body []byte, questions []Question) (Reply, error) {
	var raw workerReply
	if err := json.Unmarshal(body, &raw); err != nil {
		return Reply{}, fmt.Errorf("the worker's answer is not the expected JSON: %w", err)
	}
	if raw.ModelRevision == "" {
		return Reply{}, fmt.Errorf("the worker's answer names no model_revision")
	}
	byName := map[string]workerAnswer{}
	for _, a := range raw.Answers {
		if _, dup := byName[a.Name]; dup {
			return Reply{}, fmt.Errorf("the worker answered %q twice", a.Name)
		}
		byName[a.Name] = a
	}
	if len(byName) != len(questions) {
		return Reply{}, fmt.Errorf("the worker answered %d questions, %d were asked", len(byName), len(questions))
	}
	reply := Reply{ModelID: raw.ModelID, ModelRevision: raw.ModelRevision, Confidence: map[string]float64{}}
	for _, q := range questions {
		a, ok := byName[q.Name]
		if !ok {
			return Reply{}, fmt.Errorf("the worker did not answer %q", q.Name)
		}
		if a.Confidence == nil || !unit(*a.Confidence) {
			return Reply{}, fmt.Errorf("the worker's confidence for %q is missing or outside [0, 1]", q.Name)
		}
		if a.ActProbability != nil && !unit(*a.ActProbability) {
			return Reply{}, fmt.Errorf("the worker's act_probability for %q is outside [0, 1]", q.Name)
		}
		if len(a.Probabilities) != len(q.Options) {
			return Reply{}, fmt.Errorf("the worker scored %d options for %q, it has %d", len(a.Probabilities), q.Name, len(q.Options))
		}
		scores := make([]OptionScore, 0, len(q.Options))
		for _, o := range q.Options {
			p, ok := a.Probabilities[o]
			if !ok || !unit(p) {
				return Reply{}, fmt.Errorf("the worker's probability for %q / %q is missing or outside [0, 1]", q.Name, o)
			}
			scores = append(scores, OptionScore{Option: o, Probability: p})
		}
		sort.SliceStable(scores, func(i, j int) bool { return scores[i].Probability > scores[j].Probability })
		reply.Answers = append(reply.Answers, Answer{Question: q.Name, Type: q.Type, Options: scores, Confidence: *a.Confidence})
		reply.Confidence[q.Name] = *a.Confidence
	}
	return reply, nil
}

func unit(v float64) bool { return !math.IsNaN(v) && v >= 0 && v <= 1 }
