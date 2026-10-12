package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unicode/utf8"
)

const (
	lockFile = "ask.lock" // held while actions.jsonl is appended to or compacted, here and by `athena ask`
	textMax  = 2048       // bytes of a reply
	bodyMax  = 4096       // bytes of a POST
	// settleAfter: seconds an answer can still be undone. `athena ask queue` and the doorbell see only answers older
	// than this, so athena never acts on one the human took back (athena_lib/asks.py SETTLE agrees).
	settleAfter = 6
)

func newToken() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// actionRequest is a POST's body: one of option, text or discuss for an item, or undo for a seq.
type actionRequest struct {
	Item    string  `json:"item"`
	Option  string  `json:"option"`
	Text    *string `json:"text"`
	Discuss bool    `json:"discuss"`
	Seq     int     `json:"seq"`
	Undo    bool    `json:"undo"`
}

type httpError struct {
	code int
	msg  string
}

func (e *httpError) Error() string { return e.msg }

// action records the human's answer: it checks where the request comes from and that the page offers the answer,
// appends it to actions.jsonl, and re-renders at once so every open page hides the ask. It never runs anything:
// athena reads the queue with `athena ask queue` when the watch rings for it.
func (s *Server) action(w http.ResponseWriter, r *http.Request) {
	seq, err := s.takeAction(r, w)
	if err != nil {
		var he *httpError
		if !errors.As(err, &he) {
			he = &httpError{http.StatusInternalServerError, "could not record it"}
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(he.code)
		json.NewEncoder(w).Encode(map[string]string{"error": he.msg})
		return
	}
	s.Refresh(true)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusAccepted)
	json.NewEncoder(w).Encode(map[string]int{"seq": seq})
}

func (s *Server) takeAction(r *http.Request, w http.ResponseWriter) (int, error) {
	if r.Method != http.MethodPost {
		return 0, &httpError{http.StatusMethodNotAllowed, "POST only"}
	}
	// A cross-origin page cannot send this Origin or a JSON body without a CORS preflight, which is never answered.
	if o := r.Header.Get("Origin"); o == "" || o != "http://"+r.Host {
		return 0, &httpError{http.StatusForbidden, "this page's origin only"}
	}
	if mt, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || mt != "application/json" {
		return 0, &httpError{http.StatusUnsupportedMediaType, "JSON only"}
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Athena-Token")), []byte(s.token)) != 1 {
		return 0, &httpError{http.StatusForbidden, "reload the page"}
	}
	var req actionRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, bodyMax))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return 0, &httpError{http.StatusBadRequest, "not an answer"}
	}
	now := float64(s.now().UnixNano()) / 1e9
	if req.Undo {
		if req.Seq <= 0 || req.Item != "" || req.Option != "" || req.Text != nil || req.Discuss {
			return 0, &httpError{http.StatusBadRequest, "undo names a seq only"}
		}
		return req.Seq, s.appendAction(func(cur map[int]Action, _ int) (map[string]any, error) {
			a, ok := cur[req.Seq]
			if !ok || a.Status != "queued" {
				return nil, &httpError{http.StatusConflict, "athena already has it"}
			}
			if now-a.TS >= settleAfter {
				return nil, &httpError{http.StatusConflict, "too late to undo"}
			}
			return map[string]any{"seq": req.Seq, "ts": now, "status": "cancelled"}, nil
		})
	}
	text := ""
	if req.Text != nil {
		text = strings.TrimSpace(*req.Text)
		if text == "" || len(text) > textMax || !utf8.ValidString(text) {
			return 0, &httpError{http.StatusBadRequest, fmt.Sprintf("a reply is 1 to %d bytes of text", textMax)}
		}
	}
	answers := 0
	for _, given := range []bool{req.Option != "", req.Text != nil, req.Discuss} {
		if given {
			answers++
		}
	}
	if answers != 1 || req.Seq != 0 {
		return 0, &httpError{http.StatusBadRequest, "one answer per POST"}
	}
	_, view, _ := s.snapshot()
	acts := view.actionable[req.Item]
	if acts == nil || !acts.allows(req.Option, text, req.Discuss) {
		return 0, &httpError{http.StatusUnprocessableEntity, "the page does not offer that answer"}
	}
	var seq int
	err := s.appendAction(func(_ map[int]Action, max int) (map[string]any, error) {
		seq = max + 1
		line := map[string]any{"seq": seq, "ts": now, "item": req.Item, "status": "queued"}
		switch {
		case req.Discuss:
			line["discuss"] = true
		case req.Text != nil:
			line["text"] = text
		default:
			line["option"] = req.Option
		}
		return line, nil
	})
	return seq, err
}

// appendAction appends the line next builds from the folded queue and its highest seq, under ask.lock, and syncs it.
func (s *Server) appendAction(next func(cur map[int]Action, max int) (map[string]any, error)) error {
	lock, err := os.OpenFile(filepath.Join(s.store.dir, lockFile), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	cur, max, err := readActions(filepath.Join(s.store.dir, actionsFile))
	if err != nil {
		return err
	}
	line, err := next(cur, max)
	if err != nil {
		return err
	}
	data, err := json.Marshal(line)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(s.store.dir, actionsFile), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(data, '\n')); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// readActions folds actions.jsonl by seq, with the highest seq any line names (a compacted file keeps that line).
func readActions(path string) (map[int]Action, int, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[int]Action{}, 0, nil
	} else if err != nil {
		return nil, 0, err
	}
	var lines []map[string]any
	max := 0
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for sc.Scan() {
		var m map[string]any
		if json.Unmarshal(sc.Bytes(), &m) != nil || m == nil {
			continue
		}
		if n := int(num(m, "seq")); n > max {
			max = n
		}
		lines = append(lines, m)
	}
	cur := map[int]Action{}
	for _, a := range foldActions(lines) {
		cur[a.Seq] = a
	}
	return cur, max, nil
}
