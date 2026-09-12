// Package conformance locates the shared DIP conformance corpus and reads cases out of it.
//
// The corpus is specs/dip/conformance/ at the repository root: language-neutral JSON that
// this package's tests and the Python implementation's tests both run, so a disagreement
// between the two shows up in a test rather than on the wire. It deliberately belongs to
// neither implementation, which means it sits outside this module and a checkout of this
// module alone may not have it.
//
// Handling that absence honestly is the whole reason this package exists:
//
//   - DIP_CONFORMANCE_CORPUS names the directory outright. When it is set, every failure is
//     fatal -- a wrong path, a missing file, malformed JSON -- and nothing skips. A run that
//     was told where the corpus is cannot lose the cases by accident, which is what CI and
//     the tree that owns the corpus should use.
//   - With it unset, the directory is found by walking up from the working directory. Not
//     finding it is ErrAbsent, and Load turns that into a skip: a checkout without the
//     corpus cannot run it, and reporting a failure there would say the implementation
//     disagrees with a corpus nobody read.
//   - Finding the directory but not a file inside it is fatal, never a skip. A partial
//     corpus is corruption rather than absence.
//
// One caveat the callers have to carry, because this package cannot: `go test` without -v
// prints nothing at all for a package whose tests skip -- not even output written straight
// to stderr, which the tool buffers and discards for a package that passes. A skip here is
// therefore invisible at the default verbosity, so the package Makefile reports the corpus
// status on every run and the coverage floor stays where a full checkout puts it.
package conformance

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// EnvDir is the environment variable that names the corpus directory outright, skipping the
// walk up from the working directory. Setting it also means absence is never tolerated.
const EnvDir = "DIP_CONFORMANCE_CORPUS"

// RelPath is where the corpus sits relative to the repository root.
var RelPath = filepath.Join("specs", "dip", "conformance")

// ErrAbsent reports that nothing said where the corpus is: EnvDir is unset and no ancestor
// of the working directory holds RelPath. It is the one condition a caller may skip on.
var ErrAbsent = errors.New("conformance corpus absent")

// Dir reports the corpus directory. The boolean is false only for ErrAbsent's condition.
// A directory named by EnvDir is returned whether or not it exists, so that a wrong path
// fails at the read rather than quietly becoming a skip.
func Dir() (string, bool) {
	if named := os.Getenv(EnvDir); named != "" {
		return named, true
	}
	dir, err := os.Getwd()
	if err != nil {
		return "", false
	}
	for {
		candidate := filepath.Join(dir, RelPath)
		if info, err := os.Stat(candidate); err == nil && info.IsDir() {
			return candidate, true
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", false
		}
		dir = parent
	}
}

// Read returns the bytes of one corpus file. It reports ErrAbsent when the corpus was never
// located, and a read error when it was located but the file will not open -- two different
// answers, because only the first is a checkout that legitimately cannot run these cases.
//
// Load is what tests call; this is the half that has no *testing.T in it, so the policy
// above can be tested rather than merely asserted in a comment.
func Read(name string) ([]byte, error) {
	dir, located := Dir()
	if !located {
		return nil, fmt.Errorf("%w: no %s above the working directory and %s is unset",
			ErrAbsent, RelPath, EnvDir)
	}
	path := filepath.Join(dir, name)
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return raw, nil
}

// Load decodes one corpus file into into. An absent corpus skips the calling test with a
// message naming what did not run and how to run it; anything else is fatal.
func Load(tb testing.TB, name string, into any) {
	tb.Helper()
	raw, err := Read(name)
	switch {
	case errors.Is(err, ErrAbsent):
		tb.Skipf("%v, so the cases in %s did not run. Set %s to a %s directory to run them.",
			err, name, EnvDir, RelPath)
	case err != nil:
		tb.Fatalf("conformance corpus: %v", err)
	}
	if err := json.Unmarshal(raw, into); err != nil {
		tb.Fatalf("decode %s: %v", name, err)
	}
}
