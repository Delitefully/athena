package main

import (
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
	if len(s.Workers) != 11 || len(s.Hooks) != 11 || s.Watch == nil || len(s.Watch.Workers) != 7 {
		t.Fatalf("workers %d hooks %d watch %+v", len(s.Workers), len(s.Hooks), s.Watch)
	}
	if len(s.History) != 2 || s.History[0].PR.State != "MERGED" {
		t.Fatalf("history %+v", s.History)
	}
	if len(s.Notes) != 2 || len(s.Notices) != 0 {
		t.Fatalf("notes %q notices %q", s.Notes, s.Notices)
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
	// Without watch.json, abc-108's approval is unknown, so it waits under In progress.
	if v := Build(s, 1791500000, "acme"); len(v.InProgress) != 5 || len(v.NeedsYou) != 4 {
		t.Fatalf("a broken file blanked the page: %s / %s", ids(v.NeedsYou), ids(v.InProgress))
	}
}

func TestLedgerLineBeingAppendedIsSkipped(t *testing.T) {
	dir := copyFixture(t)
	f, _ := os.OpenFile(filepath.Join(dir, "ledger.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"event": "retire", "name": "abc-1`)
	f.Close()
	s := NewStore(dir).Load()
	if len(s.Workers) != 11 || s.Workers[4].State != "live" {
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
	os.Remove(filepath.Join(dir, "needs-you.md"))
	if !st.Changed() {
		t.Fatal("missed a delete")
	}
	write(t, filepath.Join(dir, "notes.md"), "not ours")
	if st.Changed() {
		t.Fatal("a file the page does not read is not a change")
	}
}

func TestParseNotes(t *testing.T) {
	got := parseNotes("# Needs you\n\n<!-- kept by athena\nover two lines -->\n- one\n* two\n  - [ ] three\nfour\n\n<!-- c -->\n")
	if strings.Join(got, "|") != "one|two|three|four" {
		t.Fatalf("%q", got)
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
