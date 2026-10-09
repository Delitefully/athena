package main

import (
	"strings"
	"testing"
)

const fixtureNow = 1791500000

func ids(items []Item) string {
	var out []string
	for _, it := range items {
		if it.Note != "" {
			out = append(out, "note")
		} else {
			out = append(out, it.Name)
		}
	}
	return strings.Join(out, " ")
}

func find(items []Item, name string) Item {
	for _, it := range items {
		if it.Name == name {
			return it
		}
	}
	return Item{}
}

func TestBuildGroupsTheFixture(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	if got := ids(v.NeedsYou); got != "abc-102 abc-106 abc-108 note note" {
		t.Fatalf("needs you: %s", got)
	}
	if got := ids(v.InProgress); got != "abc-101 abc-103 abc-107 abc-109" {
		t.Fatalf("in progress: %s", got)
	}
	if got := ids(v.Done); got != "abc-100 abc-099 abc-098 abc-097" {
		t.Fatalf("done: %s", got)
	}
	if v.Title != "(5) athena" {
		t.Fatalf("title %q", v.Title)
	}

	perm := find(v.NeedsYou, "abc-102")
	if perm.Label != "permission prompt" || !perm.Badge || perm.Icon != "blocked" || perm.Detail != "Claude needs your permission to use Bash" {
		t.Fatalf("abc-102 %+v", perm)
	}
	ctx := find(v.NeedsYou, "abc-106")
	if ctx.Label != "needs context" || ctx.Detail != "Which ceiling: 5 tries, or 24 hours?" {
		t.Fatalf("abc-106 %+v", ctx)
	}
	ready := find(v.NeedsYou, "abc-108")
	if ready.Label != "approved, ready to merge" || ready.PR.Text != "#48" || ready.PR.Review != "approved" || ready.PR.Checks != "success" {
		t.Fatalf("abc-108 %+v %+v", ready, ready.PR)
	}
	if ready.TicketURL != "https://linear.app/acme/issue/ABC-108" || ready.Repo != "web" {
		t.Fatalf("abc-108 links %+v", ready)
	}

	if w := find(v.InProgress, "abc-101"); w.Label != "working" || w.PR.Text != "#41 draft" || w.PR.Checks != "pending" || w.PR.StaleSince != 0 {
		t.Fatalf("abc-101 %+v %+v", w, w.PR)
	}
	if w := find(v.InProgress, "abc-103"); w.Label != "done, athena reviewing" || w.Icon != "review" || w.Badge {
		t.Fatalf("abc-103 %+v", w)
	}
	if w := find(v.InProgress, "abc-109"); w.Icon != "fail" || w.PR.Checks != "failure" {
		t.Fatalf("abc-109 %+v", w)
	}
	if w := find(v.InProgress, "abc-107"); w.PR != nil || w.Label != "idle" || w.Detail != "Waiting for the design tokens to land on main." {
		t.Fatalf("abc-107 %+v", w)
	}

	if d := find(v.Done, "abc-098"); d.Label != "merged" || d.Icon != "merged" || d.PR.URL != "https://github.com/acme/api/pull/77" {
		t.Fatalf("abc-098 %+v", d)
	}
	// Retired before history.jsonl existed: the PR comes from the worker file's last report.
	if d := find(v.Done, "abc-100"); d.Label != "retired" || d.PR == nil || d.PR.Text != "#39" || d.When != fixtureNow-3*3600 {
		t.Fatalf("abc-100 %+v %+v", d, d.PR)
	}
	if d := find(v.Done, "abc-099"); d.PR != nil {
		t.Fatalf("abc-099 has no PR: %+v", d.PR)
	}
}

func TestBuildCases(t *testing.T) {
	live := func(name string) Worker { return Worker{Name: name, State: "live", Linear: strings.ToUpper(name), Updated: fixtureNow} }
	open := func(draft bool, review, checks string) *PR {
		return &PR{Number: 7, URL: "https://github.com/acme/web/pull/7", State: "OPEN", Draft: draft, Review: review, Checks: checks}
	}
	cases := []struct {
		name  string
		w     Worker
		hook  Hook
		watch *WatchWorker
		group string
		label string
	}{
		{"pending goal", Worker{Name: "a", State: "live", GoalPending: true}, Hook{}, nil, "needs", "waiting at a startup prompt"},
		{"blocked report", live("b"), Hook{State: "blocked", Report: &Report{Status: "BLOCKED", Concerns: []string{"x"}}}, nil, "needs", "blocked"},
		{"done, out of draft", live("c"), Hook{State: "done"}, &WatchWorker{PR: open(false, "REVIEW_REQUIRED", "success")}, "needs", "ready for your review"},
		{"done, out of draft, failing", live("d"), Hook{State: "done"}, &WatchWorker{PR: open(false, "", "failure")}, "progress", "done, athena reviewing"},
		{"done, draft", live("e"), Hook{State: "done"}, &WatchWorker{PR: open(true, "", "success")}, "progress", "done, athena reviewing"},
		{"approved while working", live("f"), Hook{State: "working"}, &WatchWorker{PR: open(false, "APPROVED", "pending")}, "needs", "approved, ready to merge"},
		{"merged, not retired", live("g"), Hook{State: "idle"}, &WatchWorker{PR: &PR{URL: "u", State: "MERGED"}}, "done", "merged, not retired yet"},
		{"herdr fills an unknown hook state", live("h"), Hook{}, &WatchWorker{State: "blocked"}, "needs", "permission prompt"},
		{"no hook file yet", live("i"), Hook{}, nil, "progress", "starting"},
		{"exited", live("j"), Hook{State: "exited"}, nil, "progress", "its Claude exited"},
		{"concerns", live("k"), Hook{State: "done", Report: &Report{Status: "DONE_WITH_CONCERNS"}}, nil, "progress", "done with concerns, athena reviewing"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := Snapshot{Workers: []Worker{c.w}, Hooks: map[string]Hook{c.w.Name: c.hook}}
			if c.watch != nil {
				s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{c.w.Name: *c.watch}}
			}
			v := Build(s, fixtureNow, "acme")
			groups := map[string][]Item{"needs": v.NeedsYou, "progress": v.InProgress, "done": v.Done}
			got := groups[c.group]
			if len(got) != 1 || got[0].Label != c.label {
				t.Fatalf("want %s %q, got needs=%+v progress=%+v done=%+v", c.group, c.label, v.NeedsYou, v.InProgress, v.Done)
			}
		})
	}
}

func TestStalePRStateShowsItsAge(t *testing.T) {
	s := NewStore("testdata/state").Load()
	v := Build(s, fixtureNow+600, "acme")
	if pr := find(v.InProgress, "abc-101").PR; pr.StaleSince != s.Watch.TS {
		t.Fatalf("stale since %v, watch ts %v", pr.StaleSince, s.Watch.TS)
	}
}

func TestDoneWindowAndCap(t *testing.T) {
	var s Snapshot
	s.Hooks = map[string]Hook{}
	for i := 0; i < 20; i++ {
		s.Workers = append(s.Workers, Worker{Name: "r" + string(rune('a'+i)), State: "retired", Updated: fixtureNow - float64(i)*3600})
	}
	s.Workers = append(s.Workers, Worker{Name: "old", State: "retired", Updated: fixtureNow - 15*24*3600})
	s.Workers = append(s.Workers, Worker{Name: "gone", State: "failed", Updated: fixtureNow})
	v := Build(s, fixtureNow, "acme")
	if len(v.Done) != doneMax || v.Done[0].Name != "ra" || find(v.Done, "old").Name != "" {
		t.Fatalf("done %s", ids(v.Done))
	}
}

func TestPRFromReportURL(t *testing.T) {
	pr := prFromReport(nil, &Report{PR: "https://github.com/acme/web/pull/123"})
	if pr == nil || pr.Number != 123 {
		t.Fatalf("%+v", pr)
	}
	if prFromReport(&Report{PR: ""}, &Report{PR: "not a url"}) != nil {
		t.Fatal("only an https url makes a PR link")
	}
}

func TestInlineEscapesAndLinks(t *testing.T) {
	got := string(inline(`<script>x</script> **merge** [PR](https://github.com/acme/web/pull/1) see https://x.test/a?b=1&c=2 [bad](javascript:alert(1)) ` + "`main`"))
	for _, want := range []string{
		"&lt;script&gt;x&lt;/script&gt;",
		"<strong>merge</strong>",
		`<a href="https://github.com/acme/web/pull/1">PR</a>`,
		`<a href="https://x.test/a?b=1&amp;c=2">https://x.test/a?b=1&amp;c=2</a>`,
		"[bad](javascript:alert(1))",
		`<span class="code">main</span>`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %s", want, got)
		}
	}
}
