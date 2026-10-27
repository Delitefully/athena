package main

import (
	"bufio"
	"encoding/json"
	"html/template"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const archiveFile = "items-archive.jsonl" // items resolved over a week ago (athena_lib/asks.py _archive)

// FullView is an item's own page (plan §3): what the human asked in their words, where it stands, what is needed
// from them as a decision brief, the report or plan in full with its previews embedded, its workers and PR, and its
// timeline, with the real actions.
type FullView struct {
	Ask      Ask
	Kind     string // how the page names its kind and whose move it is
	State    string // open, snoozed until ..., resolved ..., or the default athena applied
	Call     template.HTML
	Body     template.HTML
	Previews []Preview
	Workers  []Item // each worker's row as the main page shows it
	PR       *PRView
	Timeline []LogEntry
	Actions  *Actions // the answers the page offers; nil once it is resolved
	AutoAt   float64
	Done     bool // the dash's own: Done, Snooze, and Reopen or Wake for undoing them
	Snooze   bool
	Reopen   bool
	Wake     bool
	Archived bool
}

func (s *Server) fullView(id string) (*FullView, bool) {
	snap := s.store.Load()
	_, view, _ := s.snapshot()
	now := float64(s.now().UnixNano()) / 1e9
	var a *Ask
	for i := range snap.Asks {
		if snap.Asks[i].ID == id {
			a = &snap.Asks[i]
		}
	}
	archived := false
	if a == nil {
		for _, old := range readArchive(s.store.dir) {
			if old.ID == id {
				o := old
				a, archived = &o, true
			}
		}
	}
	if a == nil {
		return nil, false
	}
	fv := &FullView{Ask: *a, Kind: kindLabel(*a), Call: inline(a.Text), Archived: archived, Timeline: timeline(*a, snap.Actions)}
	if a.Body != "" {
		fv.Body = renderMarkdown(a.Body)
	}
	if !archived {
		for _, name := range a.Previews {
			fv.Previews = append(fv.Previews, Preview{Name: strings.TrimSuffix(name, "/"), URL: "/preview/" + a.ID + "/" + name})
		}
	}
	if a.PR != "" {
		fv.PR = &PRView{URL: "https://github.com/" + strings.Replace(a.PR, "#", "/pull/", 1), Text: a.PR}
	}
	rows := map[string]Item{}
	for _, group := range [][]Item{view.NeedsYou, view.Ready, view.InProgress, view.Done} {
		for _, it := range group {
			rows[it.ID] = it
		}
	}
	for _, w := range a.Workers {
		row, ok := rows["w-"+w]
		if !ok {
			row = Item{Name: w, Label: "not live"}
		}
		if row.Title == "" {
			row.Title = row.Name
		}
		fv.Workers = append(fv.Workers, row)
	}
	switch {
	case !a.Open():
		fv.State = "resolved " + ago(s.now().Sub(unix(a.Resolved)))
		if a.ResolvedNote != "" {
			fv.State += ": " + a.ResolvedNote
		}
		fv.Reopen = a.ResolvedBy == "you" && !archived
		return fv, true
	case a.Snoozed(now):
		fv.State = "snoozed until " + at(unix(a.SnoozeUntil).Local(), s.now().Local())
		fv.Wake = true
	case a.Applied != nil:
		fv.State = "athena took the rec on its own: " + a.Applied.Label
	default:
		fv.State = "open"
	}
	fv.Done, fv.Snooze = true, !a.Snoozed(now)
	if acts := view.actionable[a.ID]; acts != nil {
		fv.Actions = acts
	}
	if a.Auto && a.Applied == nil {
		fv.AutoAt = applyAt(*a, snap.LastSeen, now)
	}
	return fv, true
}

// readArchive reads items-archive.jsonl, parsed as needs-you.json's items are.
func readArchive(dir string) []Ask {
	f, err := os.Open(filepath.Join(dir, archiveFile))
	if err != nil {
		return nil
	}
	defer f.Close()
	var list []any
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for sc.Scan() {
		var m map[string]any
		if jsonLine(sc.Bytes(), &m) {
			list = append(list, m)
		}
	}
	return parseAsks(map[string]any{"items": list})
}

func (s *Server) itemPage(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/item/")
	if !askID.MatchString(id) {
		http.NotFound(w, r)
		return
	}
	fv, ok := s.fullView(id)
	if !ok {
		http.NotFound(w, r)
		return
	}
	data := map[string]any{"Scheme": scheme(r), "Token": s.token, "Boot": s.boot, "Item": fv, "Title": fv.Ask.Name + " · athena"}
	if r.URL.Query().Get("fragment") == "1" { // the page's own refresh, on each push
		s.render(w, "itemmain", fv)
		return
	}
	s.render(w, "itempage", data)
}

// SearchHit is an item a search found, open, resolved or archived.
type SearchHit struct {
	ID, Name, Kind, State string
	Call                  template.HTML
	When                  float64
}

const searchMax = 50

// search finds items by their name, title, why, the human's words and their resolution, newest first.
func (s *Server) search(w http.ResponseWriter, r *http.Request) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	var hits []SearchHit
	if q != "" {
		words := strings.Fields(strings.ToLower(q))
		snap := s.store.Load()
		seen := map[string]bool{}
		for _, a := range append(snap.Asks, readArchive(s.store.dir)...) {
			if seen[a.ID] {
				continue
			}
			seen[a.ID] = true
			hay := strings.ToLower(strings.Join([]string{a.ID, a.Name, a.Text, a.Why, a.Words, a.ResolvedNote, a.PR, strings.Join(a.Workers, " ")}, " "))
			all := true
			for _, w := range words {
				all = all && strings.Contains(hay, w)
			}
			if !all {
				continue
			}
			state := "open"
			if !a.Open() {
				state = "resolved"
			}
			hits = append(hits, SearchHit{ID: a.ID, Name: a.Name, Kind: kindLabel(a), State: state, Call: inline(a.Text), When: a.Updated})
		}
		sort.SliceStable(hits, func(i, j int) bool { return hits[i].When > hits[j].When })
		if len(hits) > searchMax {
			hits = hits[:searchMax]
		}
	}
	s.render(w, "searchpage", map[string]any{"Scheme": scheme(r), "Token": s.token, "Boot": s.boot, "Q": q, "Hits": hits, "Title": "Search · athena"})
}

func jsonLine(b []byte, m *map[string]any) bool { return json.Unmarshal(b, m) == nil && *m != nil }
