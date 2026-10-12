package main

import (
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"
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
	if got := ids(v.NeedsYou); got != "abc-102 abc-106 abc-108 abc-104 web note note note" {
		t.Fatalf("needs you: %s", got)
	}
	if got := ids(v.InProgress); got != "abc-101 abc-103 abc-107 abc-109 abc-452 web" {
		t.Fatalf("in progress: %s", got)
	}
	if got := ids(v.Done); got != "abc-100 abc-099 abc-098 abc-097" {
		t.Fatalf("done: %s", got)
	}
	if v.Title != "(8) athena" {
		t.Fatalf("title %q", v.Title)
	}

	// The screenshot case: a live idle worker's PR and a stacked PR, each awaiting a teammate's review with
	// auto-merge on, and each linked from a note that folds into its row.
	own := find(v.InProgress, "abc-452")
	if own.Label != "awaiting review · alex" || own.Badge || own.PR.Text != "#6452" || own.PR.Review != "" || len(own.Folds) != 1 ||
		!strings.Contains(string(own.Folds[0]), `href="https://github.com/acme/web/pull/6452"`) {
		t.Fatalf("abc-452 %+v %+v", own, own.PR)
	}
	stack := v.InProgress[len(v.InProgress)-1]
	if stack.Label != "awaiting review · alex" || stack.Title != "Billing export, step 2: CSV writer" || stack.PR.Text != "#6148" ||
		stack.Ticket != "" || len(stack.Folds) != 1 || !strings.Contains(string(stack.Folds[0]), "next to land") {
		t.Fatalf("stack row %+v %+v", stack, stack.PR)
	}
	notes := v.NeedsYou[len(v.NeedsYou)-3:]
	for i, want := range []string{"note", "note", "athena asks"} {
		if notes[i].Label != want {
			t.Errorf("note %d %q: label %q, want %q", i, notes[i].Note, notes[i].Label, want)
		}
	}
	if !strings.Contains(string(notes[2].Note), "Close ABC-4550") {
		t.Fatalf("the question %q", notes[2].Note)
	}
	// A retired worker whose PR is still open leaves Done for the row of whose move it is.
	if r := find(v.NeedsYou, "abc-104"); r.Label != "changes requested" || !r.Badge || r.PR.Text != "#64" || r.Ticket != "ABC-104" {
		t.Fatalf("abc-104 %+v %+v", r, r.PR)
	}
	rr := v.NeedsYou[4]
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

func TestDoneHidesRetiredWorkersWithNoPRAndNoTicket(t *testing.T) {
	s := NewStore("testdata/state").Load()
	s.Workers = append(s.Workers,
		Worker{Name: "probe", State: "retired", Updated: fixtureNow - 3600},
		Worker{Name: "pr-only", State: "retired", Updated: fixtureNow - 3600},
	)
	s.Hooks["pr-only"] = Hook{LastReport: &Report{Status: "DONE", PR: "https://github.com/acme/web/pull/5"}}
	v := Build(s, fixtureNow, "acme")
	// probe has neither; abc-099 has only a ticket, pr-only only a PR, abc-098 both.
	if got := ids(v.Done); got != "pr-only abc-100 abc-099 abc-098 abc-097" {
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
		{"done, a teammate reviews", live("c"), Hook{State: "done"}, &WatchWorker{PR: awaiting()}, "progress", "awaiting review · alex"},
		{"done, nobody asked to review", live("c2"), Hook{State: "done"}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "needs", "no reviewer requested"},
		{"done, checks failing", live("d"), Hook{State: "done"}, &WatchWorker{PR: moved("checks failing", "failing", false)}, "progress", "checks failing"},
		{"idle after a DONE report", live("l"), Hook{State: "idle", LastReport: &Report{Status: "DONE"}}, &WatchWorker{PR: awaiting()}, "progress", "awaiting review · alex"},
		{"exited after DONE", live("m"), Hook{State: "exited", LastReport: &Report{Status: "DONE_WITH_CONCERNS"}}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "needs", "no reviewer requested"},
		{"idle with no report", live("n"), Hook{State: "idle"}, &WatchWorker{PR: awaiting()}, "progress", "awaiting review · alex"},
		{"no review data yet", live("n2"), Hook{State: "idle"}, &WatchWorker{PR: open(false, "", "success")}, "progress", "idle"},
		{"done, draft", live("e"), Hook{State: "done"}, &WatchWorker{PR: open(true, "", "success")}, "progress", "done, athena reviewing"},
		{"approved while working", live("f"), Hook{State: "working"}, &WatchWorker{PR: moved("approved · yours to merge", "approved", true)}, "needs", "approved · yours to merge"},
		{"approved, not the human's repo", live("f2"), Hook{State: "idle"}, &WatchWorker{PR: moved("approved · your go to merge", "approved", true)}, "needs", "approved · your go to merge"},
		{"approved, in the merge queue", live("f3"), Hook{State: "idle"}, &WatchWorker{PR: moved("approved · in merge queue", "queued", false)}, "progress", "approved · in merge queue"},
		{"approved, auto-merge on", live("f4"), Hook{State: "working"}, &WatchWorker{PR: moved("approved · auto-merge on", "queued", false)}, "progress", "approved · auto-merge on"},
		{"changes requested, worker on it", live("f5"), Hook{State: "working"}, &WatchWorker{PR: moved("changes requested · worker fixing", "changes", false)}, "progress", "changes requested · worker fixing"},
		{"still working, nobody asked to review", live("f6"), Hook{State: "working"}, &WatchWorker{PR: moved("no reviewer requested", "unrequested", true)}, "progress", "working"},
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
	if got := label(v.InProgress); got != "#1 awaiting review · alex | #10 approved · in merge queue | #11 approved · auto-merge on | #13 draft" {
		t.Fatalf("in progress: %s", got)
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

func TestNotesFoldIntoTheRowOfThePRTheyLink(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}, Workers: []Worker{{Name: "abc-1", State: "live"}}}
	s.Watch = &Watch{TS: fixtureNow, Workers: map[string]WatchWorker{"abc-1": {PR: followed(1, "", "awaiting review · alex", "awaiting", false)}},
		PRs: map[string]*PR{"acme/web#9": followed(9, "", "awaiting review · sam", "awaiting", false)}}
	s.Notes = []string{
		"Merge **ABC-1**? https://github.com/Acme/Web/pull/1",
		"Then [the next one](https://github.com/acme/web/pull/9).",
		"Is https://github.com/acme/web/pull/99 still needed?",
		"FYI: the quota resets at 14:00",
		"Which first: ABC-5 or ABC-6 ?  ",
	}
	v := Build(s, fixtureNow, "acme")
	if got := ids(v.NeedsYou); got != "note note note" {
		t.Fatalf("needs you: %s", got)
	}
	for i, want := range []string{"athena asks", "note", "athena asks"} {
		if v.NeedsYou[i].Label != want {
			t.Errorf("%q: %q, want %q", v.NeedsYou[i].Note, v.NeedsYou[i].Label, want)
		}
	}
	if f := v.InProgress[0].Folds; len(f) != 1 || !strings.Contains(string(f[0]), "<strong>ABC-1</strong>") {
		t.Fatalf("abc-1 folds %q", f)
	}
	if f := v.InProgress[1].Folds; len(f) != 1 || !strings.Contains(string(f[0]), ">the next one</a>") {
		t.Fatalf("#9 folds %q", f)
	}
	if v.Title != "(3) athena" {
		t.Fatalf("title %q", v.Title)
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
