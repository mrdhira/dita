package conformance_test

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/mrdhira/dita/packages/golibs/dip/internal/conformance"
)

// The skip policy is the reason this package exists, so it is tested rather than described.
// Read carries the whole decision and has no *testing.T in it, which is what makes that
// possible: testing.TB cannot be implemented outside the testing package, so a Load that
// decided for itself could only be asserted by reading it.

const sampleCorpus = `{"corpus": "dip-framing", "protocol": 2}`

// tree builds a temp directory with an optional corpus in it and returns its root.
func tree(t *testing.T, withCorpus bool) string {
	t.Helper()
	root := t.TempDir()
	if withCorpus {
		dir := filepath.Join(root, conformance.RelPath)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatalf("create the corpus directory: %v", err)
		}
		if err := os.WriteFile(filepath.Join(dir, "framing.json"), []byte(sampleCorpus), 0o644); err != nil {
			t.Fatalf("write the corpus file: %v", err)
		}
	}
	// A working directory several levels down, so a located corpus is one the walk up
	// found rather than one that happened to be in the current directory.
	deep := filepath.Join(root, "packages", "golibs", "dip")
	if err := os.MkdirAll(deep, 0o755); err != nil {
		t.Fatalf("create the working directory: %v", err)
	}
	return root
}

func TestReadLocatesTheCorpus(t *testing.T) {
	cases := []struct {
		name        string
		withCorpus  bool
		env         func(root string) string
		wantFound   bool
		wantAbsent  bool
		wantReadErr bool
	}{
		{
			name:       "the walk up finds a corpus above the working directory",
			withCorpus: true,
			wantFound:  true,
		},
		{
			name:       "no corpus anywhere above is absence, not a failure",
			withCorpus: false,
			wantFound:  false,
			wantAbsent: true,
		},
		{
			name:       "an empty environment variable reads as unset",
			withCorpus: false,
			env:        func(string) string { return "" },
			wantFound:  false,
			wantAbsent: true,
		},
		{
			name:       "the environment variable wins over the walk up",
			withCorpus: true,
			env:        func(root string) string { return filepath.Join(root, conformance.RelPath) },
			wantFound:  true,
		},
		{
			name:        "a corpus directory with the file missing is fatal, never absent",
			withCorpus:  false,
			env:         func(root string) string { return filepath.Join(root, conformance.RelPath) },
			wantFound:   true,
			wantReadErr: true,
		},
		{
			name:        "an environment variable naming nothing is fatal, never absent",
			withCorpus:  true,
			env:         func(root string) string { return filepath.Join(root, "no", "such", "corpus") },
			wantFound:   true,
			wantReadErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := tree(t, tc.withCorpus)
			value := ""
			if tc.env != nil {
				value = tc.env(root)
			}
			// Always set, so an ambient value in the developer's shell cannot decide the
			// case. Empty is the package's own spelling of unset.
			t.Setenv(conformance.EnvDir, value)
			t.Chdir(filepath.Join(root, "packages", "golibs", "dip"))

			if _, found := conformance.Dir(); found != tc.wantFound {
				t.Errorf("Dir() located = %v, want %v", found, tc.wantFound)
			}

			raw, err := conformance.Read("framing.json")
			switch {
			case tc.wantAbsent:
				if !errors.Is(err, conformance.ErrAbsent) {
					t.Fatalf("Read() = %v, want ErrAbsent", err)
				}
			case tc.wantReadErr:
				if err == nil {
					t.Fatal("Read() succeeded, want a read failure")
				}
				if errors.Is(err, conformance.ErrAbsent) {
					t.Fatalf("Read() reported absence for a located corpus, which a caller "+
						"would skip on: %v", err)
				}
			default:
				if err != nil {
					t.Fatalf("Read(): %v", err)
				}
				if string(raw) != sampleCorpus {
					t.Errorf("Read() returned %q, want %q", raw, sampleCorpus)
				}
			}
		})
	}
}

// TestLoadDecodes covers the half of Load that a test can reach. The skip half cannot be
// asserted here -- testing.TB has an unexported method, so there is no fake to hand it --
// and it is exercised for real by the dip package's own suite in a checkout without the
// corpus.
func TestLoadDecodes(t *testing.T) {
	cases := []struct {
		name string
		file string
		want string
	}{
		{name: "a corpus header decodes", file: "framing.json", want: "dip-framing"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := tree(t, true)
			t.Setenv(conformance.EnvDir, filepath.Join(root, conformance.RelPath))

			var header struct {
				Corpus   string `json:"corpus"`
				Protocol int    `json:"protocol"`
			}
			conformance.Load(t, tc.file, &header)

			if header.Corpus != tc.want {
				t.Errorf("corpus is %q, want %q", header.Corpus, tc.want)
			}
			if header.Protocol == 0 {
				t.Error("protocol decoded to zero, so Load did not decode the body")
			}
		})
	}
}
