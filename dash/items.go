package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"unicode/utf8"
)

// Items (plan §3): athena_lib/asks.py writes them; this file reads what it adds to an ask, and keeps the few writes
// that must work while HQ is busy or down: Done, Snooze and their undo, straight into needs-you.json under ask.lock.

const (
	// A default applies on its own (decision 4) this long after its item was filed, or awayAfter once the human has
	// been away that long: no prompt in HQ and no answer here.
	defaultAfter = 12 * 3600
	awayAfter    = 2 * 3600
	undoDefault  = "undo-default" // the answer that takes back a default athena applied (asks.py UNDO_DEFAULT)

	healthStale = 180 // seconds after which health.json is too old to call anything ok: doctor writes it every minute
	doneToday   = 24 * 3600
	needsShown  = 7 // rows Needs you shows before the rest fold behind "n more"
)

// itemKind reads an item's kind, mapping one from before items as athena_lib/asks.py normal_kind does: a question is
// a decision, a note a result, or a decision when it offers an option that asks for a reply.
func itemKind(kind string, options []Option) string {
	if itemKinds[kind] {
		return kind
	}
	if kind == "note" {
		for _, o := range options {
			if o.Do != "dismiss" {
				return "decision"
			}
		}
		return "result"
	}
	return "decision"
}

// parseItemFields reads what an item adds to an ask, dropping what athena item add would refuse: a long name or
// words, an unknown whose-move or stake, a link that is not http(s), a default on a call with stakes.
func parseItemFields(a *Ask, im map[string]any) {
	a.Name = strings.Join(strings.Fields(str(im, "name")), " ")
	if a.Name == "" || utf8.RuneCountInString(a.Name) > nameMax {
		a.Name = clip(a.Name, nameMax)
	}
	a.NameDerived = im["name_derived"] == true
	a.Next = str(im, "next")
	if !itemNext[a.Next] {
		a.Next = nextByKind[a.Kind]
	}
	a.Due = num(im, "due")
	a.Words = clip(str(im, "words"), wordsMax)
	if list, ok := im["workers"].([]any); ok {
		for _, v := range list {
			if w, ok := v.(string); ok && askID.MatchString(w) && len(a.Workers) < workersMax {
				a.Workers = append(a.Workers, w)
			}
		}
	}
	if list, ok := im["links"].([]any); ok {
		for _, v := range list {
			if lm, ok := v.(map[string]any); ok && httpLink.MatchString(str(lm, "url")) && len(a.Links) < linksMax {
				title := clip(strings.TrimSpace(str(lm, "title")), 80)
				if title == "" {
					title = str(lm, "url")
				}
				a.Links = append(a.Links, Link{Title: title, URL: str(lm, "url")})
			}
		}
	}
	a.Rec = str(im, "rec")
	if !a.offers(a.Rec) {
		a.Rec = ""
	}
	if list, ok := im["stakes"].([]any); ok {
		for _, v := range list {
			if st, ok := v.(string); ok && itemStakes[st] {
				a.Stakes = append(a.Stakes, st)
			}
		}
	}
	a.Auto = im["auto"] == true && a.Rec != "" && len(a.Stakes) == 0 && a.Kind == "decision"
	a.Created, a.Updated = num(im, "created"), num(im, "updated")
	if a.Updated < a.Created {
		a.Updated = a.Created
	}
	a.Resolved, a.ResolvedNote, a.ResolvedBy = num(im, "resolved"), clip(str(im, "resolved_note"), 200), str(im, "resolved_by")
	if sm := obj(im, "snooze"); sm != nil {
		a.SnoozeUntil, a.SnoozedAt = num(sm, "until"), num(sm, "at")
	}
	if am := obj(im, "applied"); am != nil && str(am, "option") != "" {
		a.Applied = &Applied{Option: str(am, "option"), Label: clip(str(am, "label"), labelMax+8), TS: num(am, "ts")}
	}
	if list, ok := im["log"].([]any); ok {
		for _, v := range list {
			if lm, ok := v.(map[string]any); ok && str(lm, "text") != "" {
				a.Log = append(a.Log, LogEntry{TS: num(lm, "ts"), Text: clip(str(lm, "text"), 200)})
			}
		}
		if len(a.Log) > logMax {
			a.Log = a.Log[len(a.Log)-logMax:]
		}
	}
}

func (a Ask) offers(option string) bool {
	for _, o := range a.Options {
		if o.ID == option {
			return true
		}
	}
	return false
}

func (a Ask) option(id string) Option {
	for _, o := range a.Options {
		if o.ID == id {
			return o
		}
	}
	return Option{ID: id, Label: id}
}

// Health is athena doctor's last check (health.json, written by doctor every minute).
type Health struct {
	TS       float64
	OK       bool
	Problems []Problem
}

type Problem struct {
	Check, Detail string
	Since         float64
}

func parseHealth(m map[string]any) *Health {
	h := &Health{TS: num(m, "ts"), OK: m["ok"] == true}
	if list, ok := m["problems"].([]any); ok {
		for _, v := range list {
			if pm, ok := v.(map[string]any); ok && (str(pm, "check") != "" || str(pm, "detail") != "") {
				h.Problems = append(h.Problems, Problem{Check: clip(str(pm, "check"), 40), Detail: clip(str(pm, "detail"), 160), Since: num(pm, "since")})
			}
		}
	}
	if len(h.Problems) > 0 {
		h.OK = false
	}
	return h
}

// HealthView is the page's one dot: ok (green), bad (red, with what is wrong), or unknown (grey: no health.json
// yet, or one older than healthStale, which is never read as ok).
type HealthView struct {
	State string // ok, bad, unknown
	Text  string
	Title string // every problem, for the tooltip
}

func healthView(h *Health, now float64) HealthView {
	switch {
	case h == nil:
		return HealthView{"unknown", "health unknown", "athena doctor has not reported yet"}
	case now-h.TS > healthStale:
		return HealthView{"unknown", "health unknown", "athena doctor's last check was at " + unix(h.TS).Local().Format("Mon 15:04") + "; it runs every minute"}
	case h.OK:
		return HealthView{"ok", "all ok", "athena doctor's last check: " + unix(h.TS).Local().Format("15:04")}
	}
	var lines []string
	for _, p := range h.Problems {
		line := p.Check
		if p.Detail != "" {
			line = strings.TrimSpace(p.Check + ": " + p.Detail)
		}
		lines = append(lines, strings.TrimPrefix(line, ": "))
	}
	text := "something is wrong"
	if len(lines) > 0 {
		text = clip(lines[0], 60)
		if len(lines) > 1 {
			text += fmt.Sprintf(" · +%d more", len(lines)-1)
		}
	}
	return HealthView{"bad", text, strings.Join(lines, "\n")}
}

// lastRequest: when the human last typed into HQ, from the last line of requests.jsonl (read from its end).
func lastRequest(path string) float64 {
	f, err := os.Open(path)
	if err != nil {
		return 0
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return 0
	}
	const tail = 64 << 10
	start := fi.Size() - tail
	if start < 0 {
		start = 0
	}
	buf := make([]byte, fi.Size()-start)
	if _, err := f.ReadAt(buf, start); err != nil && !errors.Is(err, io.EOF) {
		return 0
	}
	var last float64
	for _, line := range strings.Split(string(buf), "\n") {
		var m map[string]any
		if json.Unmarshal([]byte(line), &m) == nil && str(m, "id") != "" {
			last = math.Max(last, num(m, "ts"))
		}
	}
	return last
}

// applyAt is when a default applies on its own: defaultAfter after its item was filed, or, while the human has been
// away (no activity for awayAfter), awayAfter after the later of its filing and their last activity.
func applyAt(a Ask, lastSeen, now float64) float64 {
	at := a.Created + defaultAfter
	if lastSeen > 0 && now-lastSeen >= awayAfter {
		at = math.Min(at, math.Max(a.Created, lastSeen)+awayAfter)
	}
	return at
}

// dueDefaults are the items whose default applies now: an open decision that offers its recommended option, marked
// auto, with no stake (security, customer, money, one-way: never, whatever the file says), not snoozed, not applied
// yet, and with no answer of the human's or athena's still queued or taken.
func dueDefaults(s Snapshot, now float64) []Ask {
	answered := map[string]bool{}
	for _, a := range s.Actions {
		if a.Status != "cancelled" {
			answered[a.Item] = true
		}
	}
	var out []Ask
	for _, a := range s.Asks {
		if !a.Auto || len(a.Stakes) > 0 || a.Kind != "decision" || !a.offers(a.Rec) || !a.Open() || a.Snoozed(now) ||
			a.Applied != nil || answered[a.ID] || now < applyAt(a, s.LastSeen, now) {
			continue
		}
		out = append(out, a)
	}
	return out
}

// applyDefaults queues each due default's recommended option for athena, as the human's own answer would be, marked
// auto. athena acts on it as on any answer, and the item then waits in Ready for you with Done and Undo.
func (s *Server) applyDefaults(snap Snapshot, now float64) int {
	n := 0
	for _, a := range dueDefaults(snap, now) {
		err := s.appendAction(func(cur map[int]Action, max int) (map[string]any, error) {
			for _, c := range cur {
				if c.Item == a.ID && c.Status != "cancelled" {
					return nil, errAnswered
				}
			}
			return map[string]any{"seq": max + 1, "ts": now, "item": a.ID, "option": a.Rec, "auto": true, "status": "queued"}, nil
		})
		if err == nil {
			n++
		} else if !errors.Is(err, errAnswered) {
			fmt.Fprintf(os.Stderr, "default for %s not applied: %v\n", a.ID, err)
		}
	}
	return n
}

var errAnswered = errors.New("answered meanwhile")

// itemRequest is a POST to /api/item: the dash's own writes, which work while HQ is busy or down.
type itemRequest struct {
	Item  string  `json:"item"`
	Do    string  `json:"do"`    // done, snooze, reopen, wake
	Until float64 `json:"until"` // snooze: Unix seconds
}

const snoozeMax = 60 * 24 * 3600

// itemAction marks an item done, snoozes it, or undoes either. It never reaches athena: nothing is queued.
func (s *Server) itemAction(w http.ResponseWriter, r *http.Request) {
	err := s.takeItemAction(r, w)
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		var he *httpError
		if !errors.As(err, &he) {
			he = &httpError{http.StatusInternalServerError, "could not record it"}
		}
		w.WriteHeader(he.code)
		json.NewEncoder(w).Encode(map[string]string{"error": he.msg})
		return
	}
	s.Refresh(true)
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]bool{"ok": true})
}

func (s *Server) takeItemAction(r *http.Request, w http.ResponseWriter) error {
	if err := s.checkPost(r); err != nil {
		return err
	}
	var req itemRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, bodyMax))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil || !askID.MatchString(req.Item) {
		return &httpError{http.StatusBadRequest, "not an item action"}
	}
	now := float64(s.now().UnixNano()) / 1e9
	switch req.Do {
	case "done", "reopen", "wake":
		if req.Until != 0 {
			return &httpError{http.StatusBadRequest, "until goes with snooze"}
		}
	case "snooze":
		if req.Until <= now || req.Until > now+snoozeMax {
			return &httpError{http.StatusBadRequest, "snooze until a time in the next 60 days"}
		}
	default:
		return &httpError{http.StatusBadRequest, "done, snooze, reopen or wake"}
	}
	return s.editItem(req.Item, func(it map[string]any) error {
		open := num(it, "resolved") == 0
		switch req.Do {
		case "done":
			if !open {
				return &httpError{http.StatusConflict, "done already"}
			}
			it["resolved"], it["resolved_by"], it["resolved_note"] = int64(now), "you", "you marked it done"
			delete(it, "snooze")
			delete(it, "auto")
			logItem(it, now, "you marked it done")
		case "reopen":
			if open || str(it, "resolved_by") != "you" {
				return &httpError{http.StatusConflict, "only an item you marked done reopens here"}
			}
			for _, k := range []string{"resolved", "resolved_by", "resolved_note"} {
				delete(it, k)
			}
			logItem(it, now, "you reopened it")
		case "snooze":
			if !open {
				return &httpError{http.StatusConflict, "it is resolved"}
			}
			it["snooze"] = map[string]any{"until": int64(req.Until), "at": int64(now) + 1}
			logItem(it, now, "you snoozed it until "+unix(req.Until).Local().Format("Mon 2 Jan 15:04"))
		case "wake":
			if obj(it, "snooze") == nil {
				return &httpError{http.StatusConflict, "not snoozed"}
			}
			delete(it, "snooze")
			logItem(it, now, "snooze cancelled")
		}
		it["updated"] = int64(now)
		return nil
	})
}

func logItem(it map[string]any, now float64, text string) {
	log, _ := it["log"].([]any)
	log = append(log, map[string]any{"ts": int64(now), "text": text})
	if len(log) > logMax {
		log = log[len(log)-logMax:]
	}
	it["log"] = log
}

// editItem rewrites one item of needs-you.json through edit, under ask.lock (athena_lib/asks.py holds it too),
// atomically and private, keeping every other item and field as it is.
func (s *Server) editItem(id string, edit func(map[string]any) error) error {
	lock, err := os.OpenFile(filepath.Join(s.store.dir, lockFile), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	path := filepath.Join(s.store.dir, asksFile)
	data, err := os.ReadFile(path)
	if err != nil {
		return &httpError{http.StatusNotFound, "no such item"}
	}
	var file map[string]any
	if err := json.Unmarshal(data, &file); err != nil || file == nil {
		return &httpError{http.StatusConflict, "needs-you.json is unreadable"}
	}
	list, _ := file["items"].([]any)
	for _, v := range list {
		if it, ok := v.(map[string]any); ok && str(it, "id") == id {
			if err := edit(it); err != nil {
				return err
			}
			out, err := json.MarshalIndent(file, "", "  ")
			if err != nil {
				return err
			}
			tmp := fmt.Sprintf("%s.tmp%d", path, os.Getpid())
			if err := os.WriteFile(tmp, append(out, '\n'), 0o600); err != nil {
				return err
			}
			if err := os.Chmod(tmp, 0o600); err != nil {
				return err
			}
			return os.Rename(tmp, path)
		}
	}
	return &httpError{http.StatusNotFound, "no such item"}
}

// timeline is an item's log and the answers given on it, oldest first.
func timeline(a Ask, actions []Action) []LogEntry {
	out := append([]LogEntry{}, a.Log...)
	for _, act := range actions {
		if act.Item != a.ID {
			continue
		}
		var what string
		switch {
		case act.Auto:
			what = "athena took the rec on its own: " + a.option(act.Option).Label
		case act.Discuss:
			what = "you asked to discuss it in HQ"
		case act.Text != "":
			what = "you replied: " + clip(act.Text, 160)
		case act.Option == undoDefault:
			what = "you undid the default"
		default:
			what = "you answered: " + a.option(act.Option).Label
		}
		if act.Rule {
			what += " (and don't ask again)"
		}
		switch act.Status {
		case "cancelled":
			what += " (undone)"
		case "queued":
			what += " (athena has it queued)"
		}
		out = append(out, LogEntry{TS: act.TS, Text: what})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].TS < out[j].TS })
	return out
}
