// Package conformance locates the shared DIP conformance corpus and reads cases out of it.
//
// The corpus is specs/dip/conformance/ at the repository root. It belongs to neither
// implementation, so it sits outside this module and a checkout of this module alone may not
// have it. DIP_CONFORMANCE_CORPUS names it outright and makes every failure fatal; unset,
// the directory is found by walking up and not finding it is a skip. A missing file inside a
// located corpus is always fatal: that is corruption, not absence.
package conformance

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// EnvDir names the corpus directory outright. Setting it means absence is never tolerated.
const EnvDir = "DIP_CONFORMANCE_CORPUS"

// RelPath is where the corpus sits relative to the repository root.
var RelPath = filepath.Join("specs", "dip", "conformance")

// ErrAbsent reports that nothing said where the corpus is. It is the one condition a caller
// may skip on.
var ErrAbsent = errors.New("conformance corpus absent")

// Dir reports the corpus directory. A directory named by EnvDir is returned whether or not
// it exists, so a wrong path fails at the read rather than quietly becoming a skip.
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
// located, and a read error when it was located but the file will not open.
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

// Load decodes one corpus file into into. An absent corpus skips the calling test; anything
// else is fatal.
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
