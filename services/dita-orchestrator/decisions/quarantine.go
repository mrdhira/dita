package decisions

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Quarantined files are the record files: predictions and corrections, where the volume is and
// disk damage or a hand edit reaches first, and evaluations, which come from uploads. A line that
// cannot be read there costs one record. Refusing the store over it would take every route down
// until someone edits the volume, so the line is set aside instead, visibly. templates.jsonl
// stays fatal (see replay).
var quarantined = []string{"predictions.jsonl", "corrections.jsonl", "evaluations.jsonl"}

// Rejection is one line set aside at Open: its exact bytes are in File + ".rejected".
type Rejection struct {
	File   string
	Line   int
	Reason string
}

// Rejections are the lines set aside when the store was opened, in file order.
func (s *Store) Rejections() []Rejection { return append([]Rejection(nil), s.rejected...) }

// Quarantined counts the lines set aside at Open, per quarantined file; every file is present.
func (s *Store) Quarantined() map[string]int {
	out := map[string]int{}
	for _, name := range quarantined {
		out[name] = 0
	}
	for _, r := range s.rejected {
		out[r.File]++
	}
	return out
}

// readQuarantined replays name a line at a time. A line that is too long, is not JSON, or that
// apply refuses is copied byte for byte to name+".rejected" (once, however many times the store
// reopens: the bad line stays in the data file, which is never rewritten), recorded, and skipped;
// the lines after it are still read. Only a failure to read the file or to write the sidecar is
// an error: a line that cannot be preserved is not skipped.
func readQuarantined[T any](s *Store, name string, apply func(T) error) error {
	path := filepath.Join(s.dir, name)
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("open %s: %w", name, err)
	}
	defer f.Close()
	sidecar := &rejectFile{path: path + ".rejected", data: f}
	defer sidecar.close()

	rd := bufio.NewReaderSize(f, 64<<10)
	var offset int64
	for number := 1; ; number++ {
		start, line, size, hash, complete, err := nextLine(rd, offset)
		if err != nil {
			return fmt.Errorf("read %s: %w", name, err)
		}
		if size == 0 {
			break
		}
		offset += size
		reason := ""
		if line == nil {
			reason = fmt.Sprintf("longer than the %d bytes a stored line may be", MaxRecord)
		} else {
			var record T
			if err := json.Unmarshal(line, &record); err != nil {
				reason = err.Error()
			} else if err := apply(record); err != nil {
				reason = err.Error()
			}
		}
		if reason != "" {
			if err := sidecar.keep(start, size, hash); err != nil {
				return fmt.Errorf("set aside %s:%d: %w", name, number, err)
			}
			s.rejected = append(s.rejected, Rejection{File: name, Line: number, Reason: reason})
		}
		if !complete {
			// A last line with no newline is a torn append; end it, so the next record starts
			// on a line of its own instead of joining the fragment.
			if err := endLine(path); err != nil {
				return fmt.Errorf("end the last line of %s: %w", name, err)
			}
			break
		}
	}
	return sidecar.sync()
}

// nextLine reads one line starting at offset. line is its content without the newline, or nil
// when it is longer than MaxRecord (then it is only hashed, never held). hash is over the
// content plus one newline, the form it takes in a sidecar.
func nextLine(rd *bufio.Reader, offset int64) (start int64, line []byte, size int64, hash [32]byte, complete bool, err error) {
	h := sha256.New()
	held := []byte{}
	for {
		chunk, rerr := rd.ReadSlice('\n')
		size += int64(len(chunk))
		h.Write(bytes.TrimSuffix(chunk, []byte("\n")))
		if held != nil {
			held = append(held, chunk...)
			if len(held) > MaxRecord {
				held = nil
			}
		}
		complete = len(chunk) > 0 && chunk[len(chunk)-1] == '\n'
		if rerr == bufio.ErrBufferFull {
			continue
		}
		if rerr != nil && rerr != io.EOF {
			return 0, nil, 0, hash, false, rerr
		}
		break
	}
	h.Write([]byte("\n"))
	copy(hash[:], h.Sum(nil))
	return offset, bytes.TrimSuffix(held, []byte("\n")), size, hash, complete, nil
}

func endLine(path string) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write([]byte("\n")); err != nil {
		return err
	}
	return f.Sync()
}

// rejectFile is a data file's sidecar, opened on the first line set aside.
type rejectFile struct {
	path string
	data *os.File
	out  *os.File
	has  map[[32]byte]bool
}

func (r *rejectFile) keep(start, size int64, hash [32]byte) error {
	if r.out == nil {
		has, err := sidecarHashes(r.path)
		if err != nil {
			return err
		}
		out, err := os.OpenFile(r.path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o640)
		if err != nil {
			return err
		}
		r.out, r.has = out, has
	}
	if r.has[hash] {
		return nil
	}
	if _, err := io.Copy(r.out, io.NewSectionReader(r.data, start, size)); err != nil {
		return err
	}
	var tail [1]byte
	if _, err := r.data.ReadAt(tail[:], start+size-1); err != nil {
		return err
	}
	if tail[0] != '\n' {
		if _, err := r.out.Write([]byte("\n")); err != nil {
			return err
		}
	}
	r.has[hash] = true
	return nil
}

func (r *rejectFile) sync() error {
	if r.out == nil {
		return nil
	}
	return r.out.Sync()
}

func (r *rejectFile) close() {
	if r.out != nil {
		r.out.Close()
	}
}

// sidecarHashes are the lines a sidecar already holds, so a reopen does not copy them again.
func sidecarHashes(path string) (map[[32]byte]bool, error) {
	has := map[[32]byte]bool{}
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return has, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	rd := bufio.NewReaderSize(f, 64<<10)
	for {
		_, _, size, hash, _, err := nextLine(rd, 0)
		if err != nil {
			return nil, err
		}
		if size == 0 {
			return has, nil
		}
		has[hash] = true
	}
}
