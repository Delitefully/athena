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
	textMax  = 2000       // characters of a reply, as the page's reply box counts them (maxlength)
	bodyMax  = 16 << 10   // bytes of a POST: a reply of textMax characters, escaped
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

// actionRequest is a POST's body: one of option, text or discuss for an item, or undo for a seq. A go (option "go")
// also names the head commit the page showed, and is taken only while the PR is still at it.
type actionRequest struct {
	Item    string  `json:"item"`
	Option  string  `json:"option"`
	Head    string  `json:"head"`
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
	if strings.HasPrefix(req.Item, pillItem) && !prKeyText.MatchString(strings.TrimPrefix(req.Item, pillItem)) {
		return 0, &httpError{http.StatusBadRequest, "not a PR"}
	}
	if req.Undo && req.Seq == 0 && strings.HasPrefix(req.Item, pillItem) {
		if req.Option != "" || req.Text != nil || req.Discuss {
			return 0, &httpError{http.StatusBadRequest, "undo names a seq, or a dismissed PR"}
		}
		return 0, s.undismiss(strings.TrimPrefix(req.Item, pillItem))
	}
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
		if text == "" || !utf8.ValidString(text) || utf8.RuneCountInString(text) > textMax {
			return 0, &httpError{http.StatusBadRequest, fmt.Sprintf("a reply is 1 to %d characters", textMax)}
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
	if req.Head != "" && (req.Option != "go" || !headText.MatchString(req.Head)) {
		return 0, &httpError{http.StatusBadRequest, "a head goes with a go only"}
	}
	_, view, _ := s.snapshot()
	acts := view.actionable[req.Item]
	if acts == nil || !acts.allows(req.Option, text, req.Discuss) {
		return 0, &httpError{http.StatusUnprocessableEntity, "the page does not offer that answer"}
	}
	if req.Option == "go" && (acts.Head == "" || req.Head != acts.Head) {
		return 0, &httpError{http.StatusConflict, "the PR moved on: look again"}
	}
	if p := view.pills[req.Item]; p != nil && req.Option == "dismiss" {
		return 0, s.dismiss(p, now) // the dash's own: athena is not told
	}
	var seq int
	err := s.appendAction(func(cur map[int]Action, max int) (map[string]any, error) {
		// The page check above ran outside the lock: an ask a second tab answered meanwhile is refused here, and so is
		// a PR's button answer (Re-check, Request review, Merge) already queued. An ask shown again because its answer
		// is stuck takes a new one; a worker's row, or a Discuss, takes any number.
		for _, a := range cur {
			if (acts.Hides || sentText[req.Option] != "" && a.Option == req.Option) && a.Item == req.Item && a.Status == "queued" && now-a.TS < stuckAfter {
				return nil, &httpError{http.StatusConflict, "already answered"}
			}
		}
		seq = max + 1
		line := map[string]any{"seq": seq, "ts": now, "item": req.Item, "status": "queued"}
		switch {
		case req.Discuss:
			line["discuss"] = true
		case req.Text != nil:
			line["text"] = text
		default:
			line["option"] = req.Option
			if req.Option == "go" {
				line["head"] = req.Head
			}
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

// dismissAge: seconds a dismissal is kept; by then its PR has a new head or is long gone.
const dismissAge = 30 * 24 * 3600

// dismiss takes a PR off the Awaiting review row, as it is now: dismissed-prs.json keeps its head and review state,
// and the PR shows again once either changes. Nothing goes to actions.jsonl, so the doorbell never rings for it.
func (s *Server) dismiss(p *Pill, now float64) error {
	return s.editDismissed(func(prs map[string]any) error {
		prs[p.Key] = map[string]any{"head": p.head, "review": p.Label, "ts": now}
		return nil
	}, now)
}

// undismiss is the Undo of a dismiss: the PR shows again.
func (s *Server) undismiss(key string) error {
	return s.editDismissed(func(prs map[string]any) error {
		if _, ok := prs[key]; !ok {
			return &httpError{http.StatusConflict, "not dismissed"}
		}
		delete(prs, key)
		return nil
	}, float64(s.now().UnixNano())/1e9)
}

// editDismissed rewrites dismissed-prs.json through edit, dropping dismissals older than dismissAge, atomically.
func (s *Server) editDismissed(edit func(prs map[string]any) error, now float64) error {
	s.dismissing.Lock()
	defer s.dismissing.Unlock()
	path := filepath.Join(s.store.dir, dismissFile)
	var file struct {
		PRs map[string]any `json:"prs"`
	}
	if data, err := os.ReadFile(path); err == nil {
		json.Unmarshal(data, &file) // an unreadable file starts over: a dismissal is a convenience
	}
	prs := map[string]any{}
	for k, v := range file.PRs {
		if d, ok := v.(map[string]any); ok && prKeyText.MatchString(k) && now-num(d, "ts") < dismissAge {
			prs[k] = d
		}
	}
	if err := edit(prs); err != nil {
		return err
	}
	data, err := json.MarshalIndent(map[string]any{"prs": prs}, "", "  ")
	if err != nil {
		return err
	}
	tmp := fmt.Sprintf("%s.tmp%d", path, os.Getpid())
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
