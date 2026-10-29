package main

import (
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"
)

const fixtureNow = 1791500000

// ids: a worker's row by its worker's name, an item's by its id, a PR's by its repo.
func ids(items []Item) string {
	var out []string
	for _, it := range items {
		switch {
		case strings.HasPrefix(it.ID, "w-"):
			out = append(out, it.ID[2:])
		case strings.HasPrefix(it.ID, "ask-"):
			out = append(out, it.ID[4:])
		default:
			out = append(out, it.Name)
		}
	}
	return strings.Join(out, " ")
}

// asksOf makes an item per text, a decision (the human's call) when it starts "? " (cut off) and a result otherwise.
func asksOf(texts ...string) []Ask {
	var out []Ask
	for i, t := range texts {
		a := Ask{ID: "a" + strconv.Itoa(i+1), Kind: "result", Next: "you", Name: "Item " + strconv.Itoa(i+1), Text: t, Created: fixtureNow - 60}
		if q, ok := strings.CutPrefix(t, "? "); ok {
			a.Kind, a.Text = "decision", q
		}
		out = append(out, a)
	}
	return out
}

// find: the row by its name, its worker's name or its item's id.
func find(items []Item, name string) Item {
	for _, it := range items {
		if it.Name == name || it.ID == "w-"+name || it.ID == "ask-"+name {
			return it
		}
	}
	return Item{}
}

func TestBuildGroupsTheFixture(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	if got := ids(v.NeedsYou); got != "a7 a2 abc-102 abc-106 web a1 a5 a6 abc-108 abc-104 api web web" {
		t.Fatalf("needs you: %s", got)
	}
	if got := ids(v.InProgress); got != "abc-109 abc-101 abc-103 abc-107 a9 a12" {
		t.Fatalf("in progress: %s", got)
	}
	if got := ids(v.Done); got != "a10 abc-100" {
		t.Fatalf("done today: %s", got)
	}
	if v.Title != "(13) athena" {
		t.Fatalf("title %q", v.Title)
	}

	// The screenshot case: every PR awaiting a teammate's review is a pill on the Awaiting review row, a live idle
	// worker's (#907) as much as a followed one's, and the stack.json chain 906 <- 908 <- 909 is one pill with two behind.
	if got := pillKeys(v.Awaiting); got != "acme/api#71 acme/docs#212 acme/web#6452 acme/web#907 acme/web#906[908 909]" {
		t.Fatalf("awaiting: %s", got)
	}
	own := v.Awaiting[3]
	if own.Label != "awaiting review · alex" || own.Title != "Saved searches sync across devices" || own.Num != "#907" || own.URL != "https://github.com/acme/web/pull/907" {
		t.Fatalf("abc-452's pill %+v", own)
	}
	// Items: the human's calls in Needs you, the one due tomorrow first; results and an applied default in Ready for
	// you; an item on a live worker names that worker's row; athena's own and CI's moves In flight; today's resolved one
	// in Done; a snoozed one off the page.
	if r := v.NeedsYou[0]; r.ID != "ask-a7" || r.Lead() != "Customer meeting" || r.Due == 0 || r.Href != "/item/a7" {
		t.Fatalf("the meeting due tomorrow leads: %+v", r)
	}
	if got := ids(v.Ready); got != "a3 a8" {
		t.Fatalf("ready: %s", got)
	}
	if r := find(v.InProgress, "Billing export"); r.ID != "w-abc-103" || r.Title != "Billing page empty state" || r.Href != "/item/a4" {
		t.Fatalf("a4 names abc-103's row: %+v", r)
	}
	if v.Snoozed != 1 {
		t.Fatalf("snoozed %d", v.Snoozed)
	}
	a1 := find(v.NeedsYou, "Export queue start")
	if a1.Label != "your call" || a1.AutoAt != fixtureNow-2000+defaultAfter || a1.Actions.TakeRec == nil || a1.Actions.TakeRec.Label != "After ABC-101" {
		t.Fatalf("a1 %+v", a1)
	}
	if a6 := find(v.NeedsYou, "Security: staging"); a6.AutoAt != 0 || a6.Actions.TakeRec == nil {
		t.Fatalf("a call marked security has a rec, never a default of its own: %+v", a6)
	}
	// A retired worker whose PR is still open leaves Done for the row of whose move it is.
	if r := find(v.NeedsYou, "abc-104"); r.Label != "changes requested" || !r.Badge || r.PR.Text != "#64" || r.Ticket != "ABC-104" {
		t.Fatalf("abc-104 %+v %+v", r, r.PR)
	}
	var rr Item
	for _, it := range v.NeedsYou {
		if it.ID == "rr-acme-web-6170" {
			rr = it
		}
	}
	if rr.Label != "your review requested" || !rr.Badge || rr.Title != "Tidy the export queue" || rr.PR.URL != "https://github.com/acme/web/pull/6170" || rr.Detail != "by kim" {
		t.Fatalf("review request %+v %+v", rr, rr.PR)
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
	if ready.Label != "approved · yours to merge" || ready.Icon != "ready" || ready.PR.Text != "#48" || ready.PR.Checks != "success" {
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

	// Done is today's: abc-097 to abc-099 retired days ago, and are found by search, not on the page.
	if d := find(v.Done, "abc-098"); d.ID != "" {
		t.Fatalf("abc-098 retired days ago: %+v", d)
	}
	// Retired before history.jsonl existed: the PR comes from the worker file's last report.
	if d := find(v.Done, "abc-100"); d.Label != "retired" || d.PR == nil || d.PR.Text != "#39" || d.When != fixtureNow-3*3600 {
		t.Fatalf("abc-100 %+v %+v", d, d.PR)
	}
	s := NewStore("testdata/state").Load()
	v = Build(s, 1791406400+600, "acme") // the day abc-098 retired
	if d := find(v.Done, "abc-098"); d.Label != "merged" || d.Icon != "merged" || d.PR.URL != "https://github.com/acme/api/pull/77" {
		t.Fatalf("abc-098 %+v", d)
	}
}

func TestDoneHidesRetiredWorkersWithNoPRAndNoTicket(t *testing.T) {
	s := NewStore("testdata/state").Load()
	s.Workers = append(s.Workers,
		Worker{Name: "probe", State: "retired", Updated: fixtureNow - 3600},
		Worker{Name: "pr-only", State: "retired", Updated: fixtureNow - 3600},
	)
	s.Hooks["pr-only"] = Hook{LastReport: &Report{Status: "DONE", PR: "https://github.com/acme/web/pull/5"}}
	v := Build(s, fixtureNow, "acme")
	// probe has neither, pr-only only a PR; Done is today's, a10 the item resolved an hour ago.
	if got := ids(v.Done); got != "pr-only a10 abc-100" {
		t.Fatalf("done: %s", got)
	}
}

func TestBuildCases(t *testing.T) {
	live := func(name string) Worker {
		return Worker{Name: name, State: "live", Linear: strings.ToUpper(name), Updated: fixtureNow}
	}
	open := func(draft bool, review, checks string) *PR {
		return &PR{Number: 7, URL: "https://github.com/acme/web/pull/7", State: "OPEN", Draft: draft, Review: review, Checks: checks}
	}
	moved := func(label, kind string, needs bool) *PR {
		p := open(false, "REVIEW_REQUIRED", "success")
		p.Move = &Move{Label: label, Kind: kind, Needs: needs}
		return p
	}
	awaiting := func() *PR { return moved("awaiting review · alex", "awaiting", false) }
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
		{"blocked, its PR approved", live("b2"), Hook{State: "blocked"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "needs", "permission prompt"},
		{"done, a teammate reviews", live("c"), Hook{State: "done"}, &WatchWorker{PR: awaiting()}, "awaiting", "awaiting review · alex"},
		{"done, nobody asked to review", live("c2"), Hook{State: "done"}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "needs", "no reviewer requested"},
		{"done, checks failing", live("d"), Hook{State: "done"}, &WatchWorker{PR: moved("checks failing", "failing", false)}, "progress", "checks failing"},
		{"idle after a DONE report", live("l"), Hook{State: "idle", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: awaiting()}, "awaiting", "awaiting review · alex"},
		{"exited after DONE", live("m"), Hook{State: "exited", LastReport: &Report{Status: "DONE_WITH_CONCERNS"}}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "needs", "no reviewer requested"},
		{"idle with no report", live("n"), Hook{State: "idle"}, &WatchWorker{PR: awaiting()}, "awaiting", "awaiting review · alex"},
		{"no review data yet", live("n2"), Hook{State: "idle"}, &WatchWorker{PR: open(false, "", "success")}, "progress", "idle"},
		{"done, draft", live("e"), Hook{State: "done"}, &WatchWorker{PR: open(true, "", "success")}, "progress", "done, athena reviewing"},
		{"approved while working", live("f"), Hook{State: "working"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "progress", "working"},
		{"approved, worker done", live("f1"), Hook{State: "done"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "needs", "approved · yours to merge"},
		{"no review rule, done", live("f7"), Hook{State: "done"}, &WatchWorker{PR: moved("your go to merge", "mergeable", true)}, "needs", "your go to merge"},
		{"no review rule, idle after DONE", live("f9"), Hook{State: "idle", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: moved("your go to merge", "mergeable", true)}, "needs", "your go to merge"},
		{"no review rule, idle with no report", live("f10"), Hook{State: "idle"}, &WatchWorker{PR: moved("your go to merge", "mergeable", true)}, "progress", "idle"},
		{"approved, exited mid-task", live("f11"), Hook{State: "exited"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "progress", "its Claude exited"},
		{"approved, idle with no report", live("f12"), Hook{State: "idle"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "progress", "idle"},
		{"checks running", live("f13"), Hook{State: "done"}, &WatchWorker{PR: moved("checks running", "pending", false)}, "progress", "checks running"},
		{"done, athena reviewing its PR", live("r1"), Hook{State: "done"}, &WatchWorker{PR: moved("in review · athena", "reviewing", false)}, "progress", "in review · athena"},
		{"idle after DONE, athena reviewing", live("r2"), Hook{State: "idle", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: moved("in review · athena", "reviewing", false)}, "progress", "in review · athena"},
		{"idle with no report, not in review yet", live("r3"), Hook{State: "idle"}, &WatchWorker{PR: moved("in review · athena", "reviewing", false)}, "progress", "idle"},
		{"working, not in review yet", live("r4"), Hook{State: "working"}, &WatchWorker{PR: moved("in review · athena", "reviewing", false)}, "progress", "working"},
		{"review findings, worker fixing", live("r5"), Hook{State: "working"}, &WatchWorker{PR: moved("review findings · worker fixing", "changes", false)}, "progress", "review findings · worker fixing"},
		{"review findings, worker idle", live("r6"), Hook{State: "idle", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: moved("review findings · worker fixing", "changes", false)}, "progress", "review findings · worker fixing"},
		{"no review rule, still working", live("f8"), Hook{State: "working"}, &WatchWorker{PR: moved("your go to merge", "mergeable", true)}, "progress", "working"},
		{"approved, not the human's repo", live("f2"), Hook{State: "idle", LastReport: &Report{Status: "DONE_WITH_CONCERNS"}}, &WatchWorker{PR: moved("approved · your go to merge", "approved", true)}, "needs", "approved · your go to merge"},
		{"approved, in the merge queue", live("f3"), Hook{State: "idle"}, &WatchWorker{PR: moved("approved · in merge queue", "queued", false)}, "progress", "approved · in merge queue"},
		{"approved, auto-merge on", live("f4"), Hook{State: "working"}, &WatchWorker{PR: moved("approved · auto-merge on", "queued", false)}, "progress", "approved · auto-merge on"},
		{"changes requested, worker on it", live("f5"), Hook{State: "working"}, &WatchWorker{PR: moved("changes requested · worker fixing", "changes", false)}, "progress", "changes requested · worker fixing"},
		{"still working, nobody asked to review", live("f6"), Hook{State: "working"}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "progress", "working"},
		{"merged, not retired", live("g"), Hook{State: "idle"}, &WatchWorker{PR: &PR{URL: "u", State: "MERGED"}}, "done", "merged, not retired yet"},
		{"herdr fills an unknown hook state", live("h"), Hook{}, &WatchWorker{State: "blocked"}, "needs", "permission prompt"},
		{"no hook file yet", live("i"), Hook{}, nil, "progress", "starting"},
		{"exited", live("j"), Hook{State: "exited"}, nil, "progress", "its Claude exited"},
		{"the report gate gave way", live("u1"), Hook{State: "idle-unreported"}, nil, "progress", "stopped without a report"},
		{"the gate gave way, its PR open", live("u2"), Hook{State: "idle-unreported"}, &WatchWorker{PR: open(false, "", "success")}, "progress", "stopped without a report"},
		{"the gate gave way after a DONE report", live("u3"), Hook{State: "idle-unreported", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: moved("your go to merge", "mergeable", true)}, "needs", "your go to merge"},
		{"concerns", live("k"), Hook{State: "done", Report: &Report{Status: "DONE_WITH_CONCERNS"}}, nil, "progress", "done with concerns, athena reviewing"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := Snapshot{Workers: []Worker{c.w}, Hooks: map[string]Hook{c.w.Name: c.hook}}
			if c.watch != nil {
				s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{c.w.Name: *c.watch}}
			}
			v := Build(s, fixtureNow, "acme")
			if c.group == "awaiting" {
				if len(v.Awaiting) != 1 || v.Awaiting[0].Label != c.label || len(v.NeedsYou)+len(v.InProgress)+len(v.Done) != 0 {
					t.Fatalf("want a pill %q, got awaiting=%+v needs=%+v progress=%+v done=%+v", c.label, v.Awaiting, v.NeedsYou, v.InProgress, v.Done)
				}
				return
			}
			groups := map[string][]Item{"needs": v.NeedsYou, "progress": v.InProgress, "done": v.Done}
			got := groups[c.group]
			if len(got) != 1 || got[0].Label != c.label || len(v.Awaiting) != 0 {
				t.Fatalf("want %s %q, got needs=%+v progress=%+v done=%+v", c.group, c.label, v.NeedsYou, v.InProgress, v.Done)
			}
		})
	}
}

// TestInReviewIsInProgress: a DONE worker's PR that waits on athena's own review is never the human's to merge yet.
func TestInReviewIsInProgress(t *testing.T) {
	p := &PR{Number: 7, URL: "https://github.com/acme/web/pull/7", State: "OPEN", Checks: "success",
		Move: &Move{Label: "in review · athena", Kind: "reviewing", AthenaMerges: true}}
	w := Worker{Name: "r", State: "live", Updated: fixtureNow}
	s := Snapshot{Workers: []Worker{w}, Hooks: map[string]Hook{"r": {State: "done"}},
		Watch: &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{"r": {PR: p}}}}
	v := Build(s, fixtureNow, "acme")
	if len(v.InProgress) != 1 || len(v.NeedsYou) != 0 {
		t.Fatalf("want one In progress row, got needs=%+v progress=%+v", v.NeedsYou, v.InProgress)
	}
	if it := v.InProgress[0]; it.Icon != "review" || it.Badge || it.Actions != nil && (it.Actions.Go || it.Actions.OpenPR != "") {
		t.Fatalf("in review: icon %q badge %v actions %+v", it.Icon, it.Badge, it.Actions)
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
		s.Workers = append(s.Workers, Worker{Name: "r" + string(rune('a'+i)), Linear: "ABC-1", State: "retired", Updated: fixtureNow - float64(i)*3600})
	}
	s.Workers = append(s.Workers, Worker{Name: "old", Linear: "ABC-1", State: "retired", Updated: fixtureNow - 15*24*3600})
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

func TestSummaryNeverShowsTheReportJSON(t *testing.T) {
	rep := &Report{Status: "DONE"}
	cases := map[string]Hook{
		"cut before the report":  {Summary: "Opened the PR.\n\nATHENA-REPORT {\"status\":\"DONE\"}", Report: rep},
		"only the report's tail": {Summary: `ERGEABLE -> pass","decisions":["Identity path"],"head":"ac2d32bbb7"}`, Report: rep},
		"tail, last report only": {Summary: `tree verified"],"status":"DONE"}`, LastReport: rep},
	}
	want := map[string]string{"cut before the report": "Opened the PR.", "only the report's tail": "", "tail, last report only": ""}
	for name, h := range cases {
		if got := summary(h); got != want[name] {
			t.Errorf("%s: %q", name, got)
		}
	}
	if got := summary(Hook{Summary: `Asked: "which ceiling?"`}); got != `Asked: "which ceiling?"` {
		t.Errorf("a plain summary with quotes: %q", got)
	}
}

func TestDoneWorkerShowsItsFirstConcern(t *testing.T) {
	h := Hook{State: "done", Summary: `x"],"status":"DONE_WITH_CONCERNS"}`,
		Report: &Report{Status: "DONE_WITH_CONCERNS", Concerns: []string{"CI not checked on the new head"}}}
	v := Build(Snapshot{Workers: []Worker{{Name: "a", State: "live"}}, Hooks: map[string]Hook{"a": h}}, fixtureNow, "acme")
	if d := v.InProgress[0].Detail; d != "CI not checked on the new head" {
		t.Fatalf("detail %q", d)
	}
}

func TestAVerboseConcernShowsAsOneCappedLine(t *testing.T) {
	verbose := "CI not checked on the new head,\nbecause the runner queue was backed up for an hour and the job never started on this push"
	for name, h := range map[string]Hook{
		"done":    {State: "done", Report: &Report{Status: "DONE_WITH_CONCERNS", Concerns: []string{verbose}}},
		"blocked": {State: "blocked", Report: &Report{Status: "BLOCKED", Concerns: []string{verbose}}},
	} {
		v := Build(Snapshot{Workers: []Worker{{Name: "a", State: "live"}}, Hooks: map[string]Hook{"a": h}}, fixtureNow, "acme")
		items := append(v.InProgress, v.NeedsYou...)
		d := items[0].Detail
		if strings.Contains(d, "\n") || utf8.RuneCountInString(d) > reportItemMax || !strings.HasSuffix(d, "…") {
			t.Errorf("%s: detail %q", name, d)
		}
		if !strings.HasPrefix(d, "CI not checked on the new head, because") {
			t.Errorf("%s: detail %q", name, d)
		}
	}
	if got := oneLine("  short\n  one "); got != "short one" {
		t.Errorf("short: %q", got)
	}
	if got := oneLine(strings.Repeat("x", reportItemMax)); got != strings.Repeat("x", reportItemMax) {
		t.Errorf("at the cap: %q", got)
	}
}

func TestInlineNeverWritesInsideALink(t *testing.T) {
	got := string(inline("[x](https://a.test/`b`/**c**) and **d** https://e.test/`f`"))
	want := `<a href="https://a.test/` + "`b`" + `/**c**">x</a> and <strong>d</strong> <a href="https://e.test/` + "`f`" + `">https://e.test/` + "`f`" + `</a>`
	if got != want {
		t.Fatalf("\ngot  %s\nwant %s", got, want)
	}
}

func TestTicketLinksNeedAWorkspace(t *testing.T) {
	s := Snapshot{Workers: []Worker{{Name: "abc-1", Linear: "abc-1", State: "live"}}, Hooks: map[string]Hook{}}
	if it := Build(s, fixtureNow, "").InProgress[0]; it.Ticket != "ABC-1" || it.TicketURL != "" {
		t.Fatalf("no workspace must leave the id unlinked: %+v", it)
	}
	s.LinearWorkspace = "from-config"
	if it := Build(s, fixtureNow, "").InProgress[0]; it.TicketURL != "https://linear.app/from-config/issue/ABC-1" {
		t.Fatalf("dash.json workspace: %+v", it)
	}
	if it := Build(s, fixtureNow, "from-env").InProgress[0]; it.TicketURL != "https://linear.app/from-env/issue/ABC-1" {
		t.Fatalf("the environment wins over dash.json: %+v", it)
	}
}

func followed(n int, owner, label, kind string, needs bool) *PR {
	p := &PR{Number: n, URL: "https://github.com/acme/web/pull/" + strconv.Itoa(n), State: "OPEN", Title: "change " + strconv.Itoa(n), Owner: owner}
	if label != "" {
		p.Move = &Move{Label: label, Kind: kind, Needs: needs}
	}
	return p
}

func TestEveryFollowedPRHasOneRowByWhoseMoveItIs(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}, Workers: []Worker{
		{Name: "abc-1", Linear: "ABC-1", State: "live", Repo: "/src/web"},
		{Name: "abc-2", Linear: "ABC-2", State: "retired", Repo: "/src/web", Updated: fixtureNow - 3600},
		{Name: "abc-3", Linear: "ABC-3", State: "retired", Repo: "/src/web", Updated: fixtureNow - 3600},
	}}
	s.History = []HistoryEntry{{TS: fixtureNow - 3600, Name: "abc-2", PR: followed(2, "", "", "", false)},
		{TS: fixtureNow - 3600, Name: "abc-3", PR: followed(3, "", "", "", false)}}
	merged := followed(3, "abc-3", "", "", false)
	merged.State = "MERGED"
	s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{"abc-1": {State: "idle", PR: followed(1, "", "awaiting review · alex", "awaiting", false)}},
		PRs: map[string]*PR{
			"acme/web#1":  followed(1, "", "awaiting review · alex", "awaiting", false), // its live worker's row has it
			"acme/web#2":  followed(2, "abc-2", "no reviewer requested", "unrequested", true),
			"acme/web#3":  merged,
			"acme/web#10": followed(10, "", "approved · in merge queue", "queued", false),
			"acme/web#11": followed(11, "", "approved · auto-merge on", "queued", false),
			"acme/web#12": followed(12, "", "checks failing", "failing", true),
			"acme/web#13": followed(13, "", "draft", "draft", false),
			"acme/web#14": followed(14, "", "approved · yours to merge", "approved", true),
		}}
	v := Build(s, fixtureNow, "acme")
	label := func(items []Item) string {
		var out []string
		for _, it := range items {
			out = append(out, it.PR.Text+" "+it.Label)
		}
		return strings.Join(out, " | ")
	}
	if got := label(v.NeedsYou); got != "#12 checks failing | #2 no reviewer requested | #14 approved · yours to merge" {
		t.Fatalf("needs you: %s", got)
	}
	if got := label(v.InProgress); got != "#10 approved · in merge queue | #11 approved · auto-merge on | #13 draft" {
		t.Fatalf("in progress: %s", got)
	}
	if got := pillKeys(v.Awaiting); got != "acme/web#1" { // its live worker's PR, once
		t.Fatalf("awaiting: %s", got)
	}
	if got := label(v.Done); got != "#3 merged" {
		t.Fatalf("done: %s", got)
	}
	if r := v.NeedsYou[1]; r.Name != "abc-2" || r.Ticket != "ABC-2" || r.Icon != "done" || !r.Badge {
		t.Fatalf("retired worker's open PR %+v", r)
	}
	if r := v.NeedsYou[0]; r.Name != "web" || r.Title != "change 12" || r.Icon != "fail" || r.ID != "pr-acme-web-12" {
		t.Fatalf("stacked PR %+v", r)
	}
}

// An item about a PR is a row of its own, never folded under the PR's row: a decision stays in Needs you even when
// its PR's row is In flight, and its row links the PR. A PR URL in its text shows as owner/repo#n.
func TestAnItemAboutAPRIsARowOfItsOwn(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}, Workers: []Worker{{Name: "abc-1", State: "live"}}}
	s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{"abc-1": {PR: followed(1, "", "approved · in merge queue", "queued", false)}},
		PRs: map[string]*PR{"acme/web#9": followed(9, "", "approved · auto-merge on", "queued", false)}}
	s.Asks = asksOf(
		"? Is https://github.com/acme/web/pull/99 still needed? It has been idle a week.",
		"FYI: the quota resets at 14:00, see https://x.test/q?when=now",
		"? Merge **ABC-1** first?",
	)
	s.Asks[2].PR = "acme/web#1"
	v := Build(s, fixtureNow, "acme")
	if got := ids(v.NeedsYou); got != "a1 a3" {
		t.Fatalf("needs you: %s", got)
	}
	if got := ids(v.Ready); got != "a2" {
		t.Fatalf("ready: %s", got)
	}
	if got := ids(v.InProgress); got != "abc-1 web" {
		t.Fatalf("in flight: %s", got)
	}
	if got := string(v.NeedsYou[0].Call); !strings.Contains(got, `<a href="https://github.com/acme/web/pull/99">acme/web#99</a> still needed?`) {
		t.Errorf("a PR URL in an item shows as owner/repo#n: %s", got)
	}
	if got := string(v.Ready[0].Call); !strings.Contains(got, `<a href="https://x.test/q?when=now">https://x.test/q?when=now</a>`) {
		t.Errorf("any other URL stays as it is: %s", got)
	}
	if r := v.NeedsYou[1]; r.PR == nil || r.PR.URL != "https://github.com/acme/web/pull/1" || r.PR.Text != "#1" || string(r.Call) != "Merge <strong>ABC-1</strong> first?" {
		t.Fatalf("a3 links its PR on its own row: %+v %+v", r, r.PR)
	}
	for i, want := range []string{"ask", "ask"} {
		if v.NeedsYou[i].Icon != want {
			t.Errorf("%s: icon %q, want %q", v.NeedsYou[i].ID, v.NeedsYou[i].Icon, want)
		}
	}
	if v.Title != "(2) athena" {
		t.Fatalf("title %q", v.Title)
	}
}

// The bug the plan found: an ask folded under a Done row past the last one shown vanished with it. Items never fold,
// so with a full Done list, an item about the oldest retired worker's PR is still in Needs you.
func TestNoItemHidesBehindTheDoneList(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}}
	for n := 1; n <= doneMax+3; n++ {
		name := "abc-" + strconv.Itoa(n)
		s.Workers = append(s.Workers, Worker{Name: name, Linear: strings.ToUpper(name), State: "retired", Updated: fixtureNow - float64(100*n)})
		merged := followed(n, "", "", "", false)
		merged.State = "MERGED"
		s.History = append(s.History, HistoryEntry{TS: fixtureNow - float64(100*n), Name: name, PR: merged})
	}
	s.Asks = asksOf("? Announce https://github.com/acme/web/pull/" + strconv.Itoa(doneMax+3) + " in the release notes?")
	s.Asks[0].PR = "acme/web#" + strconv.Itoa(doneMax+3)
	v := Build(s, fixtureNow, "acme")
	if len(v.Done) != doneMax || ids(v.NeedsYou) != "a1" {
		t.Fatalf("needs %s, done %d", ids(v.NeedsYou), len(v.Done))
	}
}

func TestReviewRequestsAreCappedAndSkipFollowedPRs(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}, Watch: &Watch{TS: fixtureNow, PRs: map[string]*PR{"acme/web#5": followed(5, "", "draft", "draft", false)}}}
	for n := 1; n <= reviewRequestsMax+3; n++ {
		s.Watch.ReviewRequests = append(s.Watch.ReviewRequests, ReviewRequest{Repo: "acme/web", Number: n, Title: "<b>t</b>"})
	}
	v := Build(s, fixtureNow, "acme")
	if len(v.NeedsYou) != reviewRequestsMax {
		t.Fatalf("%d rows", len(v.NeedsYou))
	}
	for _, it := range v.NeedsYou {
		if it.PR.Text == "#5" {
			t.Fatal("a followed PR gets one row")
		}
	}
	if it := v.NeedsYou[0]; it.PR.URL != "https://github.com/acme/web/pull/1" || it.Title != "<b>t</b>" || it.Detail != "" {
		t.Fatalf("%+v", it)
	}
}

func TestARowNeverPrintsItsNameTwice(t *testing.T) {
	for _, c := range []struct{ name, title, want string }{
		{"Release notes draft", "Release notes draft: the 2.4 date is in the doc", "the 2.4 date is in the doc"},
		{"Staging login", "staging login — which fix?", "which fix?"},
		{"ABC-101 rollout", "ABC-101 rollout · after #12 merges", "after #12 merges"},
		{"Ship it", "Ship it", ""},
		{"Ship", "Shipping the release notes", "Shipping the release notes"},                 // a word that only starts with the name
		{"Tool upgrade", "Upgrade the tool 1.2 → 1.3 now", "Upgrade the tool 1.2 → 1.3 now"}, // the name is in it, not at its start
		{"", "A title", "A title"},
	} {
		if got := withoutName(c.title, c.name); got != c.want {
			t.Errorf("%q, %q: %q, want %q", c.name, c.title, got, c.want)
		}
	}
	// An old item, as it is in needs-you.json: its row shows the name once, then the rest of its title.
	s := Snapshot{Hooks: map[string]Hook{}, Asks: []Ask{{ID: "a1", Kind: "decision", Next: "you", Name: "Disk cleanup",
		Text: "Disk cleanup: free 20 GB on the build box", Why: "the disk is 95% full"}}}
	it := Build(s, fixtureNow, "").NeedsYou[0]
	if it.Lead() != "Disk cleanup" || it.Second() != "free 20 GB on the build box" || it.Detail != "" {
		t.Fatalf("%q %q %q", it.Lead(), it.Second(), it.Detail)
	}
}

func TestTheFilterMatchesARowByNameTitlePRAndWorker(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	all := append(append(append(append([]Item{}, v.NeedsYou...), v.Ready...), v.InProgress...), v.Done...)
	row := func(id string) string {
		for _, it := range all {
			if it.ID == id {
				return it.Search()
			}
		}
		t.Fatalf("no row %s", id)
		return ""
	}
	// An item's row: its name and its whole title (the part its row leaves out too), lower-cased.
	if s := row("ask-a1"); !strings.Contains(s, "export queue start") || !strings.Contains(s, "start **abc-111** now, or after abc-101 merges?") {
		t.Errorf("ask-a1: %q", s)
	}
	// A worker's row an item names: the item's name, the worker's name and title, its ticket, repo and PR.
	for _, want := range []string{"billing export", "abc-103", "#906", "acme/web#906"} {
		if s := row("w-abc-103"); !strings.Contains(s, want) {
			t.Errorf("w-abc-103 lacks %q: %q", want, s)
		}
	}
	// The Awaiting review pills match the same way.
	for _, p := range v.Awaiting {
		if s := p.Search(); !strings.Contains(s, strings.ToLower(p.Title)) || !strings.Contains(s, p.Num) || !strings.Contains(s, p.Key) {
			t.Errorf("pill %s: %q", p.ID, s)
		}
	}
	// On the page, every row and pill carries it.
	_, base := start(t, copyFixture(t))
	_, page := get(t, base+"/", "")
	if !strings.Contains(page, `<li id="ask-a1" class="item has-view" data-search="export queue start`) ||
		!strings.Contains(page, `class="apr" id="`) || strings.Count(page, `data-search="`) < len(all)+len(v.Awaiting) {
		t.Errorf("rows without data-search: %d of %d+%d", strings.Count(page, `data-search="`), len(all), len(v.Awaiting))
	}
}

// Each section lists its rows newest first by their time (When): within each of Needs you's tiers but the first, whose
// items keep their due-date order, and in Ready for you and In flight. Done already was.
func TestEachSectionIsNewestFirst(t *testing.T) {
	ago := func(min float64) float64 { return fixtureNow - min*60 }
	live := func(name string, ts float64) Worker { return Worker{Name: name, State: "live", Updated: ts} }
	due := func(a Ask, at float64) Ask { a.Due = at; return a }
	s := Snapshot{
		Workers: []Worker{live("blk-old", ago(90)), live("blk-new", ago(5)), live("run-old", ago(80)), live("run-new", ago(3)),
			live("pr-old", ago(70)), live("pr-new", ago(4))},
		Hooks: map[string]Hook{"blk-old": {State: "blocked"}, "blk-new": {State: "blocked"}, "run-old": {State: "working"},
			"run-new": {State: "working"}, "pr-old": {State: "done"}, "pr-new": {State: "done"}},
		Asks: []Ask{
			due(item("due-later", "decision", "you", "Due later", "?", ago(1)), fixtureNow+7200),
			due(item("due-sooner", "decision", "you", "Due sooner", "?", ago(300)), fixtureNow+3600),
			item("call-old", "decision", "you", "Old call", "?", ago(120)),
			item("call-new", "decision", "you", "New call", "?", ago(2)),
			item("call-mid", "decision", "you", "Mid call", "?", ago(30)),
			item("res-old", "result", "you", "Old result", "r", ago(100)),
			item("res-new", "result", "you", "New result", "r", ago(10)),
			item("req-old", "request", "athena", "Old request", "q", ago(85)),
			item("req-new", "request", "athena", "New request", "q", ago(1)),
		},
	}
	unrequested := func(n int) WatchWorker {
		return WatchWorker{PR: followed(n, "", "no reviewer requested", "unrequested", true)}
	}
	s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{"pr-old": unrequested(1), "pr-new": unrequested(2)}}
	v := Build(s, fixtureNow, "acme")
	if got := ids(v.NeedsYou); got != "due-sooner due-later blk-new blk-old call-new call-mid call-old pr-new pr-old" {
		t.Errorf("needs you: %s", got)
	}
	if got := ids(v.Ready); got != "res-new res-old" {
		t.Errorf("ready for you: %s", got)
	}
	if got := ids(v.InProgress); got != "req-new run-new run-old req-old" {
		t.Errorf("in flight: %s", got)
	}
}

// Awaiting review: the newest PR first (the higher number, within a repo), while a stack's PRs behind it keep chain order.
func TestAwaitingPillsAreNewestFirst(t *testing.T) {
	v := Build(snapshotOf(
		awaitingPR(1, "main", awaitingAlex, "awaiting", false),
		awaitingPR(5, "main", awaitingAlex, "awaiting", false),
		awaitingPR(3, "main", awaitingAlex, "awaiting", false),
		awaitingPR(6, "b3", awaitingAlex, "awaiting", false),
		awaitingPR(4, "b3", awaitingAlex, "awaiting", false),
	), fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#5 acme/web#3[4 6] acme/web#1" {
		t.Fatalf("awaiting: %s", got)
	}
	// Two pills of one stack.json chain whose middle PR is gone: a chain's place never outranks the newest first, and
	// pills from chains and from none sort the same every time.
	s := snapshotOf(awaitingPR(1, "main", awaitingAlex, "awaiting", false), awaitingPR(3, "main", awaitingAlex, "awaiting", false),
		awaitingPR(9, "main", awaitingAlex, "awaiting", false), awaitingPR(5, "main", awaitingAlex, "awaiting", false),
		awaitingPR(7, "main", awaitingAlex, "awaiting", false))
	s.Stacks = []Stack{{Repo: "acme/web", PRs: []int{1, 2, 3}}, {Repo: "acme/web", PRs: []int{4, 9}}, {Repo: "acme/web", PRs: []int{5, 100}}}
	for range 50 {
		if got := pillKeys(Build(s, fixtureNow, "acme").Awaiting); got != "acme/web#9 acme/web#7 acme/web#5 acme/web#3 acme/web#1" {
			t.Fatalf("chained pills: %s", got)
		}
	}
}
