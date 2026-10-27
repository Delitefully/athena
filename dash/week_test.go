package main

import (
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

// Plan §6, "Your week, replayed": the stories that run through items and the dash. (A crash or a reboot, and a full
// disk, are athena doctor's: v2-alwayson.)

func item(id, kind, next, name, text string, created float64) Ask {
	return Ask{ID: id, Kind: kind, Next: next, Name: name, Text: text, Created: created, Updated: created}
}

// A one-off question: the item appears as the human types (athena's move, In flight); the answer lands in Ready for
// you with its report; they read it and click Done; it is searchable later.
func TestWeekAOneOffQuestion(t *testing.T) {
	now := float64(fixtureNow)
	q := item("a1", "request", "athena", "System One on clouds", "Checking Vertex, Foundry and Bedrock for System One models", now-600)
	v := Build(Snapshot{Asks: []Ask{q}}, now, "")
	if ids(v.InProgress) != "a1" || len(v.NeedsYou) != 0 {
		t.Fatalf("asked: in flight %q", ids(v.InProgress))
	}
	q.Kind, q.Next, q.Text, q.Body = "result", "you", "None of Vertex, Foundry or Bedrock carry System One", "**None.** Checked each catalog."
	v = Build(Snapshot{Asks: []Ask{q}}, now, "")
	if ids(v.Ready) != "a1" || v.Ready[0].Actions == nil || !v.Ready[0].Actions.Done {
		t.Fatalf("answered: ready %q", ids(v.Ready))
	}
	dir := t.TempDir()
	write(t, filepath.Join(dir, asksFile), `{"next": 2, "items": [{"id": "a1", "kind": "result", "next": "you", "name": "System One on clouds",
		"text": "None of Vertex, Foundry or Bedrock carry System One", "body": "**None.** Checked each catalog.", "created": 1791499400}]}`)
	srv, base := start(t, dir)
	if code, out := itemPost(t, srv, base, `{"item": "a1", "do": "done"}`); code != http.StatusOK {
		t.Fatalf("Done: %d %v", code, out)
	}
	_, view, _ := srv.snapshot()
	if ids(view.Ready) != "" || ids(view.Done) != "a1" {
		t.Fatalf("read: ready %q done %q", ids(view.Ready), ids(view.Done))
	}
	if _, found := get(t, base+"/items?q=system+one", ""); !strings.Contains(found, `<a href="/item/a1">System One on clouds</a>`) {
		t.Fatal("not searchable later")
	}
}

// Research, plan, build: the restatement is a decision with a rec (Take all recs); the build is a request a worker
// carries, named on the worker's row; once its PR merges it waits in Ready for you until the human looks, unless they
// took the last step themselves (HQ resolves it then, "you merged it").
func TestWeekResearchPlanBuild(t *testing.T) {
	now := float64(fixtureNow)
	plan := item("a1", "decision", "you", "⌘K palette", "Feature flag no, shared UI no, DB only if perf needs it: take all recs?", now-3600)
	plan.Options, plan.Rec = []Option{{ID: "take-all-recs", Label: "Take all recs", Do: "reply"}}, "take-all-recs"
	v := Build(Snapshot{Asks: []Ask{plan}}, now, "")
	if r := v.NeedsYou[0]; r.Actions.TakeRec == nil || r.Actions.TakeRec.Label != "Take all recs" {
		t.Fatalf("the restatement offers Take rec: %+v", r)
	}
	s := Snapshot{Hooks: map[string]Hook{"abc-7": {State: "working"}}, Workers: []Worker{{Name: "abc-7", Linear: "ABC-7", Title: "Command palette", State: "live"}},
		Asks: []Ask{item("a2", "request", "worker", "⌘K palette", "Building it", now-1800)}}
	s.Asks[0].Workers, s.Asks[0].PR = []string{"abc-7"}, "acme/web#70"
	v = Build(s, now, "acme")
	if r := find(v.InProgress, "abc-7"); r.Lead() != "⌘K palette" || string(r.Second()) != "Command palette" || r.Href != "/item/a2" || ids(v.InProgress) != "abc-7" {
		t.Fatalf("building: %+v", r)
	}
	merged := followed(70, "", "", "", false)
	merged.State = "MERGED"
	s.Workers[0].State, s.Workers[0].Updated = "retired", now-60
	s.History = []HistoryEntry{{TS: now - 60, Name: "abc-7", PR: merged}}
	v = Build(s, now, "acme")
	if r := find(v.Ready, "a2"); r.Label != "merged: done when you have looked" || !r.Actions.Done {
		t.Fatalf("merged: %+v", v.Ready)
	}
	s.Asks[0].Resolved, s.Asks[0].ResolvedNote = now, "you merged it"
	if v = Build(s, now, "acme"); find(v.Done, "a2").Label != "you merged it" || len(v.Ready) != 0 {
		t.Fatalf("the human merged it: done %+v", v.Done)
	}
}

// A weekend away: chat stays quiet and reversible calls take their default after the stated time, 2 h once the human
// is away; a call with stakes waits for them. Monday's Ready for you says what athena took.
func TestWeekAWeekendAway(t *testing.T) {
	friday := float64(fixtureNow)
	rerun := decisionWithDefault()
	rerun.Created, rerun.Updated = friday+600, friday+600
	reply := item("a2", "decision", "you", "Customer reply", "Send the drafted reply? (rec send)", friday+600)
	reply.Options, reply.Rec, reply.Auto, reply.Stakes = []Option{{ID: "send", Label: "Send", Do: "reply"}}, "send", true, []string{"customer"}
	s := Snapshot{Asks: []Ask{rerun, reply}, LastSeen: friday}
	if got := dueDefaults(s, friday+awayAfter); len(got) != 0 {
		t.Fatalf("too soon: %+v", got)
	}
	got := dueDefaults(s, friday+600+awayAfter)
	if len(got) != 1 || got[0].ID != "a1" {
		t.Fatalf("Friday night: %+v", got)
	}
	if got := dueDefaults(s, friday+3*86400); len(got) != 1 {
		t.Fatalf("Monday, the customer reply still waits: %+v", got)
	}
	s.Actions = []Action{{Seq: 1, TS: friday + 600 + awayAfter, Item: "a1", Option: "re-run", Auto: true, Status: "done"}}
	s.Asks[0].Applied = &Applied{Option: "re-run", Label: "Re-run", TS: friday + 700 + awayAfter}
	v := Build(s, friday+3*86400, "")
	if r := find(v.Ready, "a1"); r.Label != "athena took the rec on its own: Re-run" || r.Actions.Undo != "Re-run" {
		t.Fatalf("Monday: %+v", v.Ready)
	}
	if ids(v.NeedsYou) != "a2" {
		t.Fatalf("the call with stakes is still the human's: %q", ids(v.NeedsYou))
	}
}

// A customer thread: the meeting is a promise with a due date, so it sorts to the top the day before; the draft reply
// is in its full view; when the human posts it, athena resolves it.
func TestWeekACustomerThread(t *testing.T) {
	now := float64(fixtureNow)
	meeting := item("a3", "promise", "you", "Customer meeting", "Confirm the customer's engineer for Wed 11:30", now-2*86400)
	meeting.Due, meeting.Body = now+20*3600, "**Draft reply:** Wednesday 11:30 works."
	other := item("a1", "decision", "you", "Docs domain", "Move the docs domain?", now-5*86400)
	s := Snapshot{Hooks: map[string]Hook{"abc-1": {State: "blocked"}}, Workers: []Worker{{Name: "abc-1", State: "live"}}, Asks: []Ask{other, meeting}}
	v := Build(s, now, "")
	if got := ids(v.NeedsYou); got != "a3 abc-1 a1" {
		t.Fatalf("the day before, the meeting leads: %s", got)
	}
	if v.NeedsYou[0].Due != meeting.Due {
		t.Fatal("no due date on the row")
	}
	if got := dueText(unix(meeting.Due).Local(), unix(now).Local()); got != "due tmrw" && !strings.HasPrefix(got, "due ") {
		t.Fatalf("%q", got)
	}
	s.Asks[1].Resolved, s.Asks[1].ResolvedNote = now+3600, "you posted the reply"
	v = Build(s, now+3700, "")
	if find(v.Done, "a3").Label != "you posted the reply" || find(v.NeedsYou, "a3").ID != "" {
		t.Fatalf("posted: %+v", v.Done)
	}
}
