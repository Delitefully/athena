package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// copyFixture copies testdata/state into a temp dir the test may change.
func copyFixture(t *testing.T) string {
	t.Helper()
	dst := t.TempDir()
	err := filepath.WalkDir("testdata/state", func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel("testdata/state", p)
		if d.IsDir() {
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dst, rel), data, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	return dst
}

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLoadFixture(t *testing.T) {
	s := NewStore("testdata/state").Load()
	if len(s.Workers) != 13 || len(s.Hooks) != 12 || s.Watch == nil || len(s.Watch.Workers) != 8 || len(s.Watch.PRs) != 10 {
		t.Fatalf("workers %d hooks %d watch %+v", len(s.Workers), len(s.Hooks), s.Watch)
	}
	if p := s.Watch.PRs["acme/web#906"]; p == nil || p.Move == nil || p.Move.Label != "awaiting review · alex" || p.Move.Needs || p.Title == "" {
		t.Fatalf("stacked PR %+v", p)
	}
	if p := s.Watch.PRs["acme/web#908"]; p.Head != "9b1e07c4a2d8" || p.Branch != "you/abc-147-step-3" || p.Base != "you/abc-147-step-2" {
		t.Fatalf("a PR's head and branches %+v", p)
	}
	if len(s.Stacks) != 1 || s.Stacks[0].Repo != "acme/web" || fmt.Sprint(s.Stacks[0].PRs) != "[906 908 909]" {
		t.Fatalf("stacks %+v", s.Stacks)
	}
	if p := s.Watch.PRs["acme/api#64"]; p.Owner != "abc-104" || !p.Move.Needs || p.Move.Kind != "changes" {
		t.Fatalf("retired worker's PR %+v %+v", p, p.Move)
	}
	if rr := s.Watch.ReviewRequests; len(rr) != 1 || rr[0] != (ReviewRequest{Repo: "acme/web", Number: 6170, Title: "Tidy the export queue", Author: "kim"}) {
		t.Fatalf("review requests %+v", rr)
	}
	if len(s.History) != 3 || s.History[0].PR.State != "MERGED" {
		t.Fatalf("history %+v", s.History)
	}
	if len(s.Asks) != 5 || len(s.Notices) != 0 {
		t.Fatalf("asks %+v notices %q", s.Asks, s.Notices)
	}
	h := s.Hooks["abc-106"]
	if h.State != "blocked" || h.Report.Status != "NEEDS_CONTEXT" || h.Report.Concerns[0] != "Which ceiling: 5 tries, or 24 hours?" {
		t.Fatalf("hook %+v", h)
	}
}

func TestFoldLedgerMirrorsPython(t *testing.T) {
	ev := func(kind, name string, ts float64, kv ...any) map[string]any {
		m := map[string]any{"event": kind, "name": name, "ts": ts}
		for i := 0; i < len(kv); i += 2 {
			m[kv[i].(string)] = kv[i+1]
		}
		return m
	}
	ws := foldLedger([]map[string]any{
		ev("spawn", "a", 1, "title", "first", "linear", "ABC-1", "goal_pending", "the goal"),
		ev("update", "a", 2, "goal_pending", nil),
		ev("update", "ghost", 3), // an update for a worker never spawned is ignored
		ev("spawn", "b", 4),
		ev("retire", "b", 5),
		ev("spawn", "b", 6, "title", "again"), // a reused name starts a fresh record
		ev("failed", "c", 7),
		ev("retire", "d", 8), // retired with no spawn on record
		{"event": "spawn", "ts": 9.0},
	})
	got := map[string]Worker{}
	for _, w := range ws {
		got[w.Name] = w
	}
	if len(ws) != 4 || got["ghost"].Name != "" {
		t.Fatalf("workers %+v", ws)
	}
	if a := got["a"]; a.State != "live" || a.GoalPending || a.Spawned != 1 || a.Updated != 2 || a.Title != "first" {
		t.Fatalf("a %+v", a)
	}
	if b := got["b"]; b.State != "live" || b.Title != "again" || b.Spawned != 6 {
		t.Fatalf("b %+v", b)
	}
	if got["c"].State != "failed" || got["d"].State != "retired" || got["d"].Updated != 8 {
		t.Fatalf("c %+v d %+v", got["c"], got["d"])
	}
}

func TestHalfWrittenWorkerFileKeepsLastGoodState(t *testing.T) {
	dir := copyFixture(t)
	st := NewStore(dir)
	st.Load()
	path := filepath.Join(dir, "workers", "abc-101.json")
	data, _ := os.ReadFile(path)
	write(t, path, string(data[:len(data)/2]))
	s := st.Load()
	if s.Hooks["abc-101"].State != "working" {
		t.Fatalf("lost the last good state: %+v", s.Hooks["abc-101"])
	}
	if len(s.Notices) != 1 || !strings.Contains(s.Notices[0], "abc-101.json is unreadable; showing its last good state") {
		t.Fatalf("notices %q", s.Notices)
	}
	write(t, path, string(data))
	if s := st.Load(); len(s.Notices) != 0 {
		t.Fatalf("notice outlived the repair: %q", s.Notices)
	}
}

func TestBrokenFileWithNoGoodStateIsSkippedQuietly(t *testing.T) {
	dir := copyFixture(t)
	write(t, filepath.Join(dir, "workers", "abc-200.json"), `{"state": "work`)
	write(t, filepath.Join(dir, "watch.json"), `[]`)
	s := NewStore(dir).Load()
	if _, ok := s.Hooks["abc-200"]; ok || s.Watch != nil {
		t.Fatalf("hook %+v watch %+v", s.Hooks["abc-200"], s.Watch)
	}
	want := []string{"watch.json is unreadable", "workers/abc-200.json is unreadable"}
	if strings.Join(s.Notices, "|") != strings.Join(want, "|") {
		t.Fatalf("notices %q", s.Notices)
	}
	// Without watch.json, no PR's move is known, so each worker shows its own state.
	if v := Build(s, 1791500000, "acme"); len(v.InProgress) != 6 || len(v.NeedsYou) != 6 {
		t.Fatalf("a broken file blanked the page: %s / %s", ids(v.NeedsYou), ids(v.InProgress))
	}
}

func TestLedgerLineBeingAppendedIsSkipped(t *testing.T) {
	dir := copyFixture(t)
	f, _ := os.OpenFile(filepath.Join(dir, "ledger.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"event": "retire", "name": "abc-1`)
	f.Close()
	s := NewStore(dir).Load()
	if len(s.Workers) != 13 || s.Workers[4].State != "live" {
		t.Fatalf("workers %+v", s.Workers)
	}
	if len(s.Notices) != 1 || s.Notices[0] != "ledger.jsonl: skipped 1 unreadable line" {
		t.Fatalf("notices %q", s.Notices)
	}
}

func TestMissingStateDirAndFiles(t *testing.T) {
	s := NewStore(filepath.Join(t.TempDir(), "nope")).Load()
	if len(s.Workers) != 0 || len(s.Notices) != 1 || !strings.HasPrefix(s.Notices[0], "no athena state yet at ") {
		t.Fatalf("%+v", s)
	}
	s = NewStore(t.TempDir()).Load()
	if len(s.Workers) != 0 || len(s.Notices) != 0 || s.Watch != nil {
		t.Fatalf("%+v", s)
	}
	v := Build(s, 1, "acme")
	if len(v.NeedsYou)+len(v.InProgress)+len(v.Done) != 0 || v.Title != "athena" {
		t.Fatalf("%+v", v)
	}
}

func TestChangedSeesWritesNewFilesAndDeletes(t *testing.T) {
	dir := copyFixture(t)
	st := NewStore(dir)
	if !st.Changed() {
		t.Fatal("first poll must load")
	}
	if st.Changed() {
		t.Fatal("nothing changed")
	}
	write(t, filepath.Join(dir, "workers", "abc-101.json"), `{"state": "idle", "name": "abc-101"}`)
	if !st.Changed() {
		t.Fatal("missed a write")
	}
	write(t, filepath.Join(dir, "workers", "abc-300.json"), `{}`)
	if !st.Changed() {
		t.Fatal("missed a new worker file")
	}
	os.Remove(filepath.Join(dir, "needs-you.json"))
	if !st.Changed() {
		t.Fatal("missed a delete")
	}
	write(t, filepath.Join(dir, "notes.md"), "not ours")
	if st.Changed() {
		t.Fatal("a file the page does not read is not a change")
	}
}

func TestParseAsksDropsWhatAthenaAskWouldRefuse(t *testing.T) {
	var m map[string]any
	json.Unmarshal([]byte(`{"items": [
		{"id": "a1", "text": "Ship it?", "pr": "https://github.com/Acme/Web/pull/7", "options": [
			{"id": "yes", "label": "Yes", "icon": "check"}, {"id": "no", "label": "No", "icon": "rm -rf", "do": "dismiss"},
			{"id": "maybe", "label": "Maybe"}]},
		{"id": "a2", "kind": "note", "text": "FYI", "options": [{"id": "x", "label": "A label past sixteen"}, {"id": "dismiss", "label": "Ok"}]},
		{"id": "a1", "text": "a second a1"}, {"id": "Bad Id", "text": "t"}, {"id": "a3", "text": "  "}, "junk"]}`), &m)
	got := parseAsks(m)
	if len(got) != 2 {
		t.Fatalf("%+v", got)
	}
	a := got[0]
	if a.Kind != "question" || a.PR != "acme/web#7" || len(a.Options) != 2 || a.Options[0] != (Option{"yes", "Yes", "check", "reply"}) ||
		a.Options[1] != (Option{"no", "No", "", "dismiss"}) {
		t.Fatalf("an unknown icon shows none, a third option is dropped: %+v", a)
	}
	if b := got[1]; b.Kind != "note" || len(b.Options) != 0 {
		t.Fatalf("a long label and the reserved id are dropped: %+v", b)
	}
}

// Every question has Discuss in Athena of its own, so an option labelled Discuss (an ask from before athena ask refused
// one) is dropped rather than shown as a second Discuss pill, and frees its slot.
func TestParseAsksDropsAnOptionThatDuplicatesDiscuss(t *testing.T) {
	var m map[string]any
	json.Unmarshal([]byte(`{"items": [{"id": "a59", "text": "Take the recs?", "options": [
		{"id": "discuss", "label": "Discuss"}, {"id": "take", "label": "Take recs"},
		{"id": "discuss-2", "label": " discuss in ATHENA "}, {"id": "later", "label": "Later"}]}]}`), &m)
	got := parseAsks(m)
	if len(got) != 1 || len(got[0].Options) != 2 || got[0].Options[0].ID != "take" || got[0].Options[1].ID != "later" {
		t.Fatalf("a Discuss option is shown: %+v", got)
	}
}

func TestAnAskWithABodyCarriesFourOptionsAndItsPreviews(t *testing.T) {
	var m map[string]any
	json.Unmarshal([]byte(`{"items": [
		{"id": "a1", "text": "Which?", "body": "**Rec: B.**\n\n| A | B |\n|---|---|\n| 1 | 2 |\n", "options": [
			{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}, {"id": "e", "label": "E"}],
		 "previews": ["mock.html", "site/", "../etc/passwd", "/abs", ".hidden", "a/b", "x.html", "y.html", 7]},
		{"id": "a2", "text": "Which?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}]}]}`), &m)
	got := parseAsks(m)
	if a := got[0]; len(a.Options) != 4 || a.Options[3].ID != "d" || a.Body != "**Rec: B.**\n\n| A | B |\n|---|---|\n| 1 | 2 |" {
		t.Fatalf("a body allows four options: %+v", a)
	}
	if p := strings.Join(got[0].Previews, " "); p != "mock.html site/ x.html y.html" {
		t.Fatalf("only plain names, at most %d: %q", previewsMax, p)
	}
	if len(got[1].Options) != 2 {
		t.Fatalf("no body, two options: %+v", got[1])
	}
}

// The title rule is athena_lib/asks.py's: both read dash/testdata/titles.json.
func TestATitleIsOneSentenceAsAthenaAskHoldsIt(t *testing.T) {
	data, err := os.ReadFile("testdata/titles.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases struct{ One, Many []string }
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, s := range cases.One {
		if title, long := (Ask{Text: s}).Title(); long || title != s {
			t.Errorf("one sentence, as it is: %q -> %q", s, title)
		}
	}
	for _, s := range cases.Many {
		if _, long := (Ask{Text: s}).Title(); !long {
			t.Errorf("more than one sentence: %q", s)
		}
	}
	// An ask from before the rule still shows: its first sentence, its whole text in its panel.
	for text, want := range map[string]string{
		"Start **ABC-111** now, or after ABC-101 merges? The 5-hour quota is at 81%.": "Start **ABC-111** now, or after ABC-101 merges?",
		"Rename it to \"export v2.\" Then ship it?":                                   "Rename it to \"export v2.\"",
		strings.Repeat("word ", 40) + "end?":                                          strings.Repeat("word ", 23) + "word…",
	} {
		if title, long := (Ask{Text: text}).Title(); !long || title != want {
			t.Errorf("%q -> %q, want %q", text, title, want)
		}
	}
}

func TestFoldActionsBySeq(t *testing.T) {
	got := foldActions([]map[string]any{
		{"seq": 1.0, "ts": 10.0, "item": "a1", "option": "yes", "status": "queued"},
		{"seq": 2.0, "ts": 11.0, "item": "a2", "text": "hi", "status": "queued"},
		{"seq": 1.0, "ts": 12.0, "status": "cancelled"},
		{"seq": 3.0, "ts": 13.0, "item": "w-abc-1", "discuss": true, "status": "queued"},
		{"seq": 3.0, "ts": 14.0, "status": "done"},
		{"seq": 9.0, "status": "done"}, // a status for a seq never queued (compacted away) is skipped
		{"seq": 2.0, "item": "a9", "status": "queued"},
	})
	want := []Action{{1, 10, "a1", "yes", "", false, "cancelled"}, {2, 11, "a2", "", "hi", false, "queued"}, {3, 13, "w-abc-1", "", "", true, "done"}}
	if len(got) != 3 || got[0] != want[0] || got[1] != want[1] || got[2] != want[2] {
		t.Fatalf("%+v", got)
	}
}

func TestDashConfigIsReadLiveAndChecked(t *testing.T) {
	dir := copyFixture(t)
	st := NewStore(dir)
	if s := st.Load(); s.LinearWorkspace != "" {
		t.Fatalf("no dash.json: %q", s.LinearWorkspace)
	}
	write(t, filepath.Join(dir, "dash.json"), `{"linear_workspace": "acme"}`)
	if !st.Changed() || st.Load().LinearWorkspace != "acme" {
		t.Fatal("dash.json not picked up")
	}
	write(t, filepath.Join(dir, "dash.json"), `{"linear_workspace": "evil.test/x?"}`)
	if s := st.Load(); s.LinearWorkspace != "" || len(s.Notices) != 1 {
		t.Fatalf("a workspace that is not a slug must be refused: %q %q", s.LinearWorkspace, s.Notices)
	}
}
