// Package decisions is the dashboard's store and rules: versioned schema templates, the
// append-only prediction and correction pair, and evaluation. It speaks no HTTP.
package decisions

import (
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Limits shared with the dashboard's zod schema; specs/decisions/schema-cases.json holds
// both to the same verdicts.
const (
	MaxQuestions   = 20
	MinOptions     = 2
	MaxOptions     = 20
	MaxOptionLen   = 100
	MaxDescription = 500
	MaxText        = 20000
)

// QuestionTypes are the worker's three question kinds.
var QuestionTypes = []string{"noul", "choice", "score"}

var (
	templateName = regexp.MustCompile(`^[a-z][a-z0-9-]{1,63}$`)
	questionName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
)

// Range bounds a score question.
type Range struct {
	Min float64 `json:"min"`
	Max float64 `json:"max"`
}

// Question is one question in the `POST /decide` shape. Criteria is what the worker's model
// reads as the question's instructions; without it the model sees only the name.
type Question struct {
	Name     string   `json:"name"`
	Type     string   `json:"type"`
	Options  []string `json:"options"`
	Range    *Range   `json:"range,omitempty"`
	Criteria string   `json:"criteria,omitempty"`
}

// Draft is what a caller submits to create a template version.
type Draft struct {
	Name        string     `json:"name"`
	Description string     `json:"description"`
	Questions   []Question `json:"questions"`
}

// Template is one immutable version of a named schema.
type Template struct {
	Name        string     `json:"name"`
	Version     int        `json:"version"`
	Description string     `json:"description"`
	Questions   []Question `json:"questions"`
	CreatedAt   time.Time  `json:"created_at"`
}

// Issue is one validation failure, addressed the way zod addresses it: a dotted path.
type Issue struct {
	Path    string `json:"path"`
	Message string `json:"message"`
}

// ValidateDraft applies the template rules. An empty result means valid.
func ValidateDraft(d Draft) []Issue {
	var issues []Issue
	add := func(path, format string, args ...any) {
		issues = append(issues, Issue{Path: path, Message: fmt.Sprintf(format, args...)})
	}
	if !templateName.MatchString(d.Name) {
		add("name", "a template name is 2-64 lowercase letters, digits or dashes, starting with a letter")
	}
	if utf8.RuneCountInString(d.Description) > MaxDescription {
		add("description", "at most %d characters", MaxDescription)
	}
	if len(d.Questions) < 1 || len(d.Questions) > MaxQuestions {
		add("questions", "between 1 and %d questions", MaxQuestions)
	}
	seen := map[string]bool{}
	for i, q := range d.Questions {
		at := fmt.Sprintf("questions.%d", i)
		if !questionName.MatchString(q.Name) {
			add(at+".name", "a question name is lowercase letters, digits or underscores, starting with a letter")
		} else if seen[q.Name] {
			add(at+".name", "question %q appears twice", q.Name)
		}
		seen[q.Name] = true
		if !validType(q.Type) {
			add(at+".type", "type must be one of %s", strings.Join(QuestionTypes, ", "))
		}
		if len(q.Options) < MinOptions || len(q.Options) > MaxOptions {
			add(at+".options", "between %d and %d options", MinOptions, MaxOptions)
		} else if q.Type == "noul" && !falseTrue(q.Options) {
			add(at+".options", "a noul is answered only as false and true, so its options are exactly those")
		}
		options := map[string]bool{}
		for j, o := range q.Options {
			switch {
			case strings.TrimSpace(o) == "" || utf8.RuneCountInString(o) > MaxOptionLen:
				add(fmt.Sprintf("%s.options.%d", at, j), "an option is 1-%d characters and not blank", MaxOptionLen)
			case options[o]:
				add(fmt.Sprintf("%s.options.%d", at, j), "option %q appears twice", o)
			}
			options[o] = true
		}
		if q.Range != nil {
			if q.Type != "score" {
				add(at+".range", "only a score question has a range")
			} else if !(q.Range.Min < q.Range.Max) {
				add(at+".range", "min must be below max")
			}
		}
	}
	return issues
}

// ValidateText applies the rule for pasted state text.
func ValidateText(text string) []Issue {
	if n := utf8.RuneCountInString(text); strings.TrimSpace(text) == "" || n > MaxText {
		return []Issue{{Path: "text", Message: fmt.Sprintf("the text is 1-%d characters and not blank", MaxText)}}
	}
	return nil
}

func falseTrue(options []string) bool {
	return len(options) == 2 && ((options[0] == "false" && options[1] == "true") || (options[0] == "true" && options[1] == "false"))
}

func validType(t string) bool {
	for _, known := range QuestionTypes {
		if t == known {
			return true
		}
	}
	return false
}
