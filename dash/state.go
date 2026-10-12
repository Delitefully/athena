package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"
)

// The state files the page is built from, all written by athena (see docs/dashboard.md).
const (
	ledgerFile  = "ledger.jsonl"
	watchFile   = "watch.json"
	historyFile = "history.jsonl"
	asksFile    = "needs-you.json" // athena's asks, written by `athena ask` only
	actionsFile = "actions.jsonl"  // the human's answers from the page, appended by this server (actions.go)
	configFile  = "dash.json"      // local settings, outside the repository: {"linear_workspace": "..."}
	workersDir  = "workers"
)

type Report struct {
	Status   string
	PR       string
	Head     string
	Concerns []string
}

type Hook struct {
	State      string
	Summary    string
	TS         float64
	Report     *Report
	LastReport *Report
}

type PR struct {
	Number   int
	URL      string
	Title    string
	State    string // OPEN, MERGED, CLOSED
	Draft    bool
	Review   string // APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED or empty
	Checks   string // success, failure, pending, none
	Comments int
	Move     *Move  // whose move it waits on, as athena watch reads it; nil without review data
	Owner    string // a followed PR's retired worker, if it has one
}

// Move is whose move an open PR waits on (athena_lib/pr.py `move`): its row's label, and whether the row is in Needs you.
type Move struct {
	Label string
	Kind  string // awaiting, unrequested, approved, mergeable, queued, changes, failing, draft
	Needs bool
}

type WatchWorker struct {
	State  string
	Detail string
	PR     *PR
}

// ReviewRequest is a teammate's open PR that asks the human for a review.
type ReviewRequest struct {
	Repo   string // owner/name, the repo athena searched
	Number int
	Title  string
	Author string
}

type Watch struct {
	TS             float64
	Workers        map[string]WatchWorker
	PRs            map[string]*PR // every other PR athena follows, by owner/name#n: stacked, or a retired worker's
	ReviewRequests []ReviewRequest
}

type HistoryEntry struct {
	TS     float64
	Name   string
	Status string
	PR     *PR
}

// Worker is one ledger record, folded the way athena_lib/ledger.py folds it.
type Worker struct {
	Name, Linear, Title, Repo, Branch string
	State                             string // live, retired, failed
	Spawned, Updated                  float64
	GoalPending                       bool
}

type Snapshot struct {
	Workers []Worker // in order of first appearance
	Hooks   map[string]Hook
	Watch   *Watch
	History []HistoryEntry
	Asks    []Ask
	Actions []Action // folded by seq, oldest first
	Notices []string
	// LinearWorkspace comes from dash.json; ATHENA_LINEAR_WORKSPACE overrides it. Empty leaves ticket ids unlinked.
	LinearWorkspace string
}

// sig is what the poll compares: a file changed when its size or modification time did.
type sig struct {
	size  int64
	mtime int64
}

// Store reads the state dir and remembers the last good parse of every JSON file, so a file caught
// mid-write or broken by hand shows its previous content instead of blanking the page.
type Store struct {
	dir  string
	sigs map[string]sig
	good map[string]map[string]any
}

func NewStore(dir string) *Store {
	return &Store{dir: dir, good: map[string]map[string]any{}}
}

func (s *Store) watched() map[string]sig {
	out := map[string]sig{}
	add := func(rel string) {
		if fi, err := os.Stat(filepath.Join(s.dir, rel)); err == nil && fi.Mode().IsRegular() {
			out[rel] = sig{fi.Size(), fi.ModTime().UnixNano()}
		}
	}
	for _, f := range []string{ledgerFile, watchFile, historyFile, asksFile, actionsFile, configFile} {
		add(f)
	}
	entries, _ := os.ReadDir(filepath.Join(s.dir, workersDir))
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") {
			add(filepath.Join(workersDir, e.Name()))
		}
	}
	return out
}

// Changed reports whether any watched file appeared, went away or changed since the last call.
func (s *Store) Changed() bool {
	now := s.watched()
	same := s.sigs != nil && len(now) == len(s.sigs)
	if same {
		for k, v := range now {
			if s.sigs[k] != v {
				same = false
				break
			}
		}
	}
	s.sigs = now
	return !same
}

// object reads a JSON object file. A missing file is (nil, nil); an unreadable one falls back to its last good parse.
func (s *Store) object(rel string, notices *[]string) map[string]any {
	data, err := os.ReadFile(filepath.Join(s.dir, rel))
	if err != nil {
		delete(s.good, rel)
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil || m == nil {
		if last, ok := s.good[rel]; ok {
			*notices = append(*notices, fmt.Sprintf("%s is unreadable; showing its last good state", rel))
			return last
		}
		*notices = append(*notices, fmt.Sprintf("%s is unreadable", rel))
		return nil
	}
	s.good[rel] = m
	return m
}

// lines reads a JSONL file, skipping lines that do not parse (a line still being appended, or a broken one).
func (s *Store) lines(rel string, notices *[]string) []map[string]any {
	f, err := os.Open(filepath.Join(s.dir, rel))
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []map[string]any
	bad := 0
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var m map[string]any
		if json.Unmarshal(line, &m) != nil || m == nil {
			bad++
			continue
		}
		out = append(out, m)
	}
	if err := sc.Err(); err != nil {
		*notices = append(*notices, fmt.Sprintf("%s: stopped reading at a line too long to read (%v)", rel, err))
	}
	if bad > 0 {
		*notices = append(*notices, fmt.Sprintf("%s: skipped %d unreadable line%s", rel, bad, plural(bad)))
	}
	return out
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// Load reads every source. It never fails: what cannot be read is empty, with a quiet notice.
func (s *Store) Load() Snapshot {
	snap := Snapshot{Hooks: map[string]Hook{}}
	if fi, err := os.Stat(s.dir); err != nil || !fi.IsDir() {
		snap.Notices = append(snap.Notices, "no athena state yet at "+s.dir)
		return snap
	}
	snap.Workers = foldLedger(s.lines(ledgerFile, &snap.Notices))
	entries, _ := os.ReadDir(filepath.Join(s.dir, workersDir))
	seen := map[string]bool{}
	for _, e := range entries {
		name, ok := strings.CutSuffix(e.Name(), ".json")
		if !ok {
			continue
		}
		rel := filepath.Join(workersDir, e.Name())
		seen[rel] = true
		if m := s.object(rel, &snap.Notices); m != nil {
			snap.Hooks[name] = parseHook(m)
		}
	}
	for rel := range s.good { // forget workers whose file went away
		if strings.HasPrefix(rel, workersDir+string(filepath.Separator)) && !seen[rel] {
			delete(s.good, rel)
		}
	}
	if m := s.object(watchFile, &snap.Notices); m != nil {
		snap.Watch = parseWatch(m)
	}
	for _, m := range s.lines(historyFile, &snap.Notices) {
		snap.History = append(snap.History, HistoryEntry{TS: num(m, "ts"), Name: str(m, "name"),
			Status: str(m, "status"), PR: parsePR(obj(m, "pr"))})
	}
	if m := s.object(configFile, &snap.Notices); m != nil {
		if ws := str(m, "linear_workspace"); workspaceSlug.MatchString(ws) {
			snap.LinearWorkspace = ws
		} else if ws != "" {
			snap.Notices = append(snap.Notices, "dash.json: linear_workspace must be a Linear workspace slug")
		}
	}
	if m := s.object(asksFile, &snap.Notices); m != nil {
		snap.Asks = parseAsks(m)
	}
	snap.Actions = foldActions(s.lines(actionsFile, &snap.Notices))
	sort.Strings(snap.Notices)
	return snap
}

var workspaceSlug = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

var terminal = map[string]string{"retire": "retired", "failed": "failed"}

// foldLedger mirrors ledger.workers(): spawn starts a record, updates merge into it, retire and failed end it.
func foldLedger(events []map[string]any) []Worker {
	recs := map[string]map[string]any{}
	var order []string
	for _, ev := range events {
		name := str(ev, "name")
		if name == "" {
			continue
		}
		kind := str(ev, "event")
		fields := map[string]any{}
		for k, v := range ev {
			if k != "event" && k != "ts" {
				fields[k] = v
			}
		}
		rec, exists := recs[name]
		switch {
		case kind == "spawn":
			rec = fields
			rec["state"] = "live"
			rec["spawned"] = ev["ts"]
		case kind == "failed":
			rec = fields
			rec["state"] = "failed"
		case exists:
			for k, v := range fields {
				rec[k] = v
			}
			if t, ok := terminal[kind]; ok {
				rec["state"] = t
			}
		case terminal[kind] != "":
			rec = fields
			rec["state"] = terminal[kind]
		default:
			continue
		}
		if !exists {
			order = append(order, name)
		}
		rec["updated"] = ev["ts"]
		recs[name] = rec
	}
	out := make([]Worker, 0, len(order))
	for _, name := range order {
		r := recs[name]
		out = append(out, Worker{Name: name, Linear: str(r, "linear"), Title: str(r, "title"), Repo: str(r, "repo"),
			Branch: str(r, "branch"), State: str(r, "state"), Spawned: num(r, "spawned"), Updated: num(r, "updated"),
			GoalPending: truthy(r["goal_pending"])})
	}
	return out
}

func parseHook(m map[string]any) Hook {
	return Hook{State: str(m, "state"), Summary: str(m, "summary"), TS: num(m, "ts"),
		Report: parseReport(obj(m, "report")), LastReport: parseReport(obj(m, "last_report"))}
}

func parseReport(m map[string]any) *Report {
	if m == nil {
		return nil
	}
	r := &Report{Status: strings.ToUpper(str(m, "status")), PR: str(m, "pr"), Head: str(m, "head")}
	if list, ok := m["concerns"].([]any); ok {
		for _, c := range list {
			if s, ok := c.(string); ok && s != "" {
				r.Concerns = append(r.Concerns, s)
			}
		}
	}
	return r
}

func parsePR(m map[string]any) *PR {
	if m == nil || str(m, "url") == "" {
		return nil
	}
	p := &PR{Number: int(num(m, "number")), URL: str(m, "url"), Title: str(m, "title"), State: strings.ToUpper(str(m, "state")),
		Draft: truthy(m["draft"]), Review: strings.ToUpper(str(m, "review")), Checks: str(m, "checks"),
		Comments: int(num(m, "comments")), Owner: str(m, "owner")}
	if mv := obj(m, "move"); mv != nil && str(mv, "label") != "" {
		p.Move = &Move{Label: str(mv, "label"), Kind: str(mv, "kind"), Needs: truthy(mv["needs"])}
	}
	return p
}

func parseWatch(m map[string]any) *Watch {
	w := &Watch{TS: num(m, "ts"), Workers: map[string]WatchWorker{}}
	for name, v := range obj(m, "workers") {
		if wm, ok := v.(map[string]any); ok {
			w.Workers[name] = WatchWorker{State: str(wm, "state"), Detail: str(wm, "detail"), PR: parsePR(obj(wm, "pr"))}
		}
	}
	for key, v := range obj(m, "prs") {
		if pm, ok := v.(map[string]any); ok {
			if p := parsePR(pm); p != nil {
				if w.PRs == nil {
					w.PRs = map[string]*PR{}
				}
				w.PRs[key] = p
			}
		}
	}
	if list, ok := m["review_requests"].([]any); ok {
		for _, v := range list {
			if rm, ok := v.(map[string]any); ok && num(rm, "number") > 0 && str(rm, "repo") != "" {
				w.ReviewRequests = append(w.ReviewRequests, ReviewRequest{Repo: str(rm, "repo"), Number: int(num(rm, "number")),
					Title: str(rm, "title"), Author: str(rm, "author")})
			}
		}
	}
	return w
}

// Ask is one item of needs-you.json: a decision or note of athena's that belongs to no worker.
type Ask struct {
	ID, Kind, Text, Why, PR string // Kind: question or note; PR: owner/name#n, the row it folds into
	Options                 []Option
}

// Option is one answer athena offers. Do is reply (athena acts on it) or dismiss (nothing to do).
type Option struct {
	ID, Label, Icon, Do string
}

const (
	optionsMax = 2
	labelMax   = 20
	askTextMax = 600
	askWhyMax  = 200
)

var askID = regexp.MustCompile(`^[a-z0-9-]{1,32}$`)

// parseAsks reads needs-you.json, dropping what `athena ask add` would refuse rather than failing the file:
// an item without an id or text, options past the second or with a long label, and icons not in icons.txt.
func parseAsks(m map[string]any) []Ask {
	list, _ := m["items"].([]any)
	var out []Ask
	seen := map[string]bool{}
	for _, v := range list {
		im, ok := v.(map[string]any)
		if !ok {
			continue
		}
		a := Ask{ID: str(im, "id"), Kind: str(im, "kind"), Text: clip(str(im, "text"), askTextMax), Why: clip(str(im, "why"), askWhyMax),
			PR: strings.ToLower(str(im, "pr"))}
		if k := prKey(str(im, "pr")); k != "" {
			a.PR = k // a PR's URL works too
		}
		if !askID.MatchString(a.ID) || strings.TrimSpace(a.Text) == "" || seen[a.ID] {
			continue
		}
		seen[a.ID] = true
		if a.Kind != "note" {
			a.Kind = "question"
		}
		opts, _ := im["options"].([]any)
		for _, ov := range opts {
			om, ok := ov.(map[string]any)
			if !ok || len(a.Options) == optionsMax {
				continue
			}
			o := Option{ID: str(om, "id"), Label: strings.TrimSpace(str(om, "label")), Icon: str(om, "icon"), Do: str(om, "do")}
			if !askID.MatchString(o.ID) || o.ID == "dismiss" || o.Label == "" || utf8.RuneCountInString(o.Label) > labelMax {
				continue
			}
			if !knownIcon(o.Icon) {
				o.Icon = ""
			}
			if o.Do != "dismiss" {
				o.Do = "reply"
			}
			a.Options = append(a.Options, o)
		}
		out = append(out, a)
	}
	return out
}

func clip(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n-1]) + "…"
	}
	return s
}

// Action is one answer from the page, as actions.jsonl folds: a line with an item queues it, a later line with
// only its seq and a status moves it on (queued -> done or cancelled).
type Action struct {
	Seq     int
	TS      float64
	Item    string
	Option  string
	Text    string
	Discuss bool
	Status  string
}

func foldActions(lines []map[string]any) []Action {
	bySeq := map[int]*Action{}
	var order []int
	for _, m := range lines {
		seq := int(num(m, "seq"))
		if seq <= 0 {
			continue
		}
		if a := bySeq[seq]; a != nil {
			if st := str(m, "status"); st == "done" || st == "cancelled" {
				a.Status = st
			}
			continue
		}
		if str(m, "item") == "" {
			continue
		}
		bySeq[seq] = &Action{Seq: seq, TS: num(m, "ts"), Item: str(m, "item"), Option: str(m, "option"), Text: str(m, "text"),
			Discuss: truthy(m["discuss"]), Status: str(m, "status")}
		order = append(order, seq)
	}
	sort.Ints(order)
	out := make([]Action, 0, len(order))
	for _, seq := range order {
		out = append(out, *bySeq[seq])
	}
	return out
}

// Tolerant field access: a field of the wrong type reads as empty rather than failing the whole file.

func str(m map[string]any, k string) string {
	if s, ok := m[k].(string); ok {
		return s
	}
	return ""
}

func num(m map[string]any, k string) float64 {
	if f, ok := m[k].(float64); ok {
		return f
	}
	return 0
}

func obj(m map[string]any, k string) map[string]any {
	if o, ok := m[k].(map[string]any); ok {
		return o
	}
	return nil
}

func truthy(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	}
	return false
}
