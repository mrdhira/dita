package main

import (
	"os"
	"testing"
)

// The framing and the ops are the dip package's job and are tested there against the
// shared corpus. What is left here is the example's own presentation logic, which is what
// someone copying this file will read first.

func TestEnvOr(t *testing.T) {
	// env is a pointer so the table can say "set to the empty string", which is a
	// different case from "not set at all" and the one a container sets by declaring a
	// variable with no value. A plain string cannot tell them apart.
	empty := ""
	socket := "/tmp/a.sock"
	cases := []struct {
		name     string
		key      string
		env      *string
		fallback string
		want     string
	}{
		{"unset key falls back", "DIP_EXAMPLE_UNSET", nil, "/run/dita/x.sock", "/run/dita/x.sock"},
		{"set key wins", "DIP_EXAMPLE_SET", &socket, "/run/dita/x.sock", "/tmp/a.sock"},
		{"empty value falls back", "DIP_EXAMPLE_EMPTY", &empty, "/run/dita/x.sock", "/run/dita/x.sock"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.env != nil {
				t.Setenv(tc.key, *tc.env)
				if _, set := os.LookupEnv(tc.key); !set {
					t.Fatalf("%s is not set, so this case is the unset case over again", tc.key)
				}
			}
			if got := envOr(tc.key, tc.fallback); got != tc.want {
				t.Errorf("envOr(%q, %q) = %q, want %q", tc.key, tc.fallback, got, tc.want)
			}
		})
	}
}

func TestOrNone(t *testing.T) {
	evicted := "rapidocr-ppocrv5"
	cases := []struct {
		name  string
		value *string
		want  string
	}{
		{"nil reads as nothing", nil, "nothing"},
		{"a model name passes through", &evicted, "rapidocr-ppocrv5"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := orNone(tc.value); got != tc.want {
				t.Errorf("orNone() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestHumanBytes(t *testing.T) {
	cases := []struct {
		name string
		n    int
		want string
	}{
		{"a system model downloads nothing", 0, "-"},
		{"under a megabyte reads in KB", 900 * 1024, "900 KB"},
		{"a megabyte reads in MB", 1 << 20, "1 MB"},
		{"the ocr model", 21510548, "20 MB"},
		{"manga-ocr", 460965376, "439 MB"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := humanBytes(tc.n); got != tc.want {
				t.Errorf("humanBytes(%d) = %q, want %q", tc.n, got, tc.want)
			}
		})
	}
}

func TestRunRequiresAnImage(t *testing.T) {
	// The one error run() can return before touching the socket, so it is the one this
	// test can reach without a live worker.
	if err := run("/nonexistent.sock", "", ""); err == nil {
		t.Fatal("run without -image returned nil, want an error")
	}
}
