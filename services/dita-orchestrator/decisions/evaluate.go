package decisions

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// Bins is the number of equal-width confidence bins behind ECE.
const Bins = 10

// MaxRows bounds one evaluation upload.
const MaxRows = 10000

// MaxEvaluationName bounds a run's name; the name and the class keys are the only uploaded
// text an evaluation stores.
const MaxEvaluationName = 200

// Row is one labelled example: the true label and the model's probability per option.
type Row struct {
	Label         string             `json:"label"`
	Probabilities map[string]float64 `json:"probabilities"`
}

// ClassScore is one class's slice of the result.
type ClassScore struct {
	Class     string   `json:"class"`
	Support   int      `json:"support"`
	Predicted int      `json:"predicted"`
	Correct   int      `json:"correct"`
	Precision *float64 `json:"precision"`
	Recall    *float64 `json:"recall"`
}

// Baseline is the majority-class predictor: always the most frequent label, with the label
// frequencies as its probabilities. A model that does not beat it is not deployed.
type Baseline struct {
	Class    string  `json:"class"`
	Accuracy float64 `json:"accuracy"`
	Brier    float64 `json:"brier"`
}

// Evaluation is one stored run.
type Evaluation struct {
	ID        string       `json:"id"`
	CreatedAt time.Time    `json:"created_at"`
	Name      string       `json:"name"`
	Rows      int          `json:"rows"`
	Accuracy  float64      `json:"accuracy"`
	Brier     float64      `json:"brier"`
	ECE       float64      `json:"ece"`
	Classes   []ClassScore `json:"classes"`
	Baseline  Baseline     `json:"baseline"`
	BeatsBase bool         `json:"beats_baseline"`
}

// Evaluate scores rows. Every row must score the same options, and its label must be one of
// them; probabilities must lie in [0, 1] and sum to 1 within 0.02.
func Evaluate(name string, rows []Row) (Evaluation, error) {
	if strings.TrimSpace(name) == "" || utf8.RuneCountInString(name) > MaxEvaluationName {
		return Evaluation{}, fmt.Errorf("%w: the name is 1-%d characters and not blank", ErrInvalid, MaxEvaluationName)
	}
	if len(rows) == 0 || len(rows) > MaxRows {
		return Evaluation{}, fmt.Errorf("%w: between 1 and %d rows", ErrInvalid, MaxRows)
	}
	var classes []string
	for k := range rows[0].Probabilities {
		classes = append(classes, k)
	}
	sort.Strings(classes)
	if len(classes) < MinOptions || len(classes) > MaxOptions {
		return Evaluation{}, fmt.Errorf("%w: between %d and %d classes", ErrInvalid, MinOptions, MaxOptions)
	}
	index := map[string]int{}
	for i, c := range classes {
		if strings.TrimSpace(c) == "" || utf8.RuneCountInString(c) > MaxOptionLen {
			return Evaluation{}, fmt.Errorf("%w: a class is 1-%d characters and not blank", ErrInvalid, MaxOptionLen)
		}
		index[c] = i
	}
	support := make([]int, len(classes))
	predicted := make([]int, len(classes))
	correct := make([]int, len(classes))
	var hits, brier float64
	binCount := make([]int, Bins)
	binHits := make([]float64, Bins)
	binConf := make([]float64, Bins)
	for r, row := range rows {
		if len(row.Probabilities) != len(classes) {
			return Evaluation{}, fmt.Errorf("%w: row %d scores %d classes, row 1 scores %d", ErrInvalid, r+1, len(row.Probabilities), len(classes))
		}
		y, ok := index[row.Label]
		if !ok {
			return Evaluation{}, fmt.Errorf("%w: row %d is labelled %q, which it does not score", ErrInvalid, r+1, row.Label)
		}
		best, conf, sum := 0, -1.0, 0.0
		for i, c := range classes {
			p, ok := row.Probabilities[c]
			if !ok || !unit(p) {
				return Evaluation{}, fmt.Errorf("%w: row %d has no probability in [0, 1] for %q", ErrInvalid, r+1, c)
			}
			sum += p
			if p > conf {
				best, conf = i, p
			}
			target := 0.0
			if i == y {
				target = 1
			}
			brier += (p - target) * (p - target)
		}
		if math.Abs(sum-1) > 0.02 {
			return Evaluation{}, fmt.Errorf("%w: row %d's probabilities sum to %.3f, not 1", ErrInvalid, r+1, sum)
		}
		support[y]++
		predicted[best]++
		b := min(int(conf*Bins), Bins-1)
		binCount[b]++
		binConf[b] += conf
		if best == y {
			hits++
			correct[y]++
			binHits[b]++
		}
	}
	n := float64(len(rows))
	e := Evaluation{Name: name, Rows: len(rows), Accuracy: hits / n, Brier: brier / n}
	for b := range Bins {
		if binCount[b] > 0 {
			k := float64(binCount[b])
			e.ECE += k / n * math.Abs(binHits[b]/k-binConf[b]/k)
		}
	}
	majority, prior2 := 0, 0.0
	for i, c := range classes {
		if support[i] > support[majority] {
			majority = i
		}
		q := float64(support[i]) / n
		prior2 += q * q
		e.Classes = append(e.Classes, ClassScore{Class: c, Support: support[i], Predicted: predicted[i],
			Correct: correct[i], Precision: ratio(correct[i], predicted[i]), Recall: ratio(correct[i], support[i])})
	}
	// The prior q scores each row sum_k (q_k - y_k)^2, which averages to 1 - sum_k q_k^2.
	e.Baseline = Baseline{Class: classes[majority], Accuracy: float64(support[majority]) / n, Brier: 1 - prior2}
	e.BeatsBase = e.Accuracy > e.Baseline.Accuracy && e.Brier < e.Baseline.Brier
	return e, nil
}

func ratio(a, b int) *float64 {
	if b == 0 {
		return nil
	}
	v := float64(a) / float64(b)
	return &v
}
