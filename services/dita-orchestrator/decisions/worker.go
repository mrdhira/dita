package decisions

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
)

// The worker's answer shape is not settled (the requirement's open question), so this file
// is the only place that knows it. The shape below is the documented stub contract; when
// inferences-system-one lands, this adapter changes and nothing else does.

// DecideRequest is the body sent to the worker's POST /decide.
type DecideRequest struct {
	Text      string     `json:"text"`
	Questions []Question `json:"questions"`
}

type workerAnswer struct {
	Name          string             `json:"name"`
	Probabilities map[string]float64 `json:"probabilities"`
	Confidence    *float64           `json:"confidence"`
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
