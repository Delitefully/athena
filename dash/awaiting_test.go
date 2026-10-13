package main

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// pillKeys: each pill's key, with the keys stacked behind it in brackets.
func pillKeys(pills []Pill) string {
	var out []string
	for _, p := range pills {
		k := p.Key
		if len(p.Behind) > 0 {
			var behind []string
			for _, b := range p.Behind {
				behind = append(behind, strings.TrimPrefix(b.Num, "#"))
			}
			k += "[" + strings.Join(behind, " ") + "]"
		}
		out = append(out, k)
	}
	return strings.Join(out, " ")
}

// awaitingPR is an open acme/web PR on head branch b<n>, based on base, whose move is label (kind).
func awaitingPR(n int, base, label, kind string, needs bool) *PR {
	p := followed(n, "", label, kind, needs)
	p.Head, p.Branch, p.Base = "sha"+itoa(n), "b"+itoa(n), base
	return p
}

func itoa(n int) string {
	return strings.TrimPrefix(followed(n, "", "", "", false).URL, "https://github.com/acme/web/pull/")
}

const awaitingAlex = "awaiting review · alex"

func snapshotOf(prs ...*PR) Snapshot {
	s := Snapshot{Hooks: map[string]Hook{}, Watch: &Watch{TS: fixtureNow, PRs: map[string]*PR{}}}
	for _, p := range prs {
		s.Watch.PRs[prKey(p.URL)] = p
	}
	return s
}

func TestTheRowShowsOnlyWithPRsAwaitingReview(t *testing.T) {
	v := Build(snapshotOf(awaitingPR(1, "main", "draft", "draft", false)), fixtureNow, "acme")
	if v.Awaiting != nil {
		t.Fatalf("no PR awaits review, yet %+v", v.Awaiting)
	}
	v = Build(snapshotOf(awaitingPR(1, "main", awaitingAlex, "awaiting", false)), fixtureNow, "acme")
	if pillKeys(v.Awaiting) != "acme/web#1" || len(v.InProgress) != 0 {
		t.Fatalf("awaiting %s, in progress %+v", pillKeys(v.Awaiting), v.InProgress)
	}
	p := v.Awaiting[0]
	if p.ID != "aw-acme-web-1" || p.Num != "#1" || p.Title != "change 1" || p.Actions.Item != "pr:acme/web#1" || !p.Actions.Recheck || !p.Actions.Dismiss {
		t.Fatalf("pill %+v %+v", p, p.Actions)
	}
	if v.actionable["pr:acme/web#1"] != p.Actions {
		t.Fatal("the pill's actions are not offered")
	}
}

func TestEveryPRShowsInExactlyOnePlace(t *testing.T) {
	s := NewStore("testdata/state").Load()
	v := Build(s, fixtureNow, "acme")
	seen := map[string]int{}
	for _, group := range [][]Item{v.NeedsYou, v.InProgress, v.Done} {
		for _, it := range group {
			if it.PR != nil && prKey(it.PR.URL) != "" {
				seen[prKey(it.PR.URL)]++
			}
		}
	}
	var walk func([]Pill)
	walk = func(pills []Pill) {
		for _, p := range pills {
			seen[p.Key]++
			walk(p.Behind)
		}
	}
	walk(v.Awaiting)
	for k, n := range seen {
		if n != 1 {
			t.Errorf("%s shows %d times", k, n)
		}
	}
	for _, k := range []string{"acme/web#906", "acme/web#907", "acme/web#908", "acme/web#909", "acme/web#6452", "acme/api#71", "acme/docs#212"} {
		if seen[k] != 1 {
			t.Errorf("%s is missing", k)
		}
	}
}

func TestStacksFromBaseBranchesAndChains(t *testing.T) {
	// By base branch: 1 <- 2 <- 3, and 4 also on 1. 2 awaits review too; 3's checks run; 4 is a draft.
	s := snapshotOf(
		awaitingPR(1, "main", awaitingAlex, "awaiting", false),
		awaitingPR(2, "b1", awaitingAlex, "awaiting", false),
		awaitingPR(3, "b2", "checks running", "pending", false),
		awaitingPR(4, "b1", "draft", "draft", false),
		awaitingPR(9, "main", "draft", "draft", false), // on nothing awaiting: stays in progress
	)
	v := Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#1[2 3 4]" {
		t.Fatalf("awaiting: %s", got)
	}
	if got := ids(v.InProgress); got != "web" || v.InProgress[0].PR.Text != "#9" {
		t.Fatalf("in progress: %s", got)
	}
	b := v.Awaiting[0].Behind
	if b[0].WaitsOn != "#1" || b[1].WaitsOn != "#2" || b[2].WaitsOn != "#1" || b[1].Label != "checks running" || v.Awaiting[0].WaitsOn != "" {
		t.Fatalf("behind %+v", b)
	}
	if v.actionable["pr:acme/web#3"] == nil || !v.actionable["pr:acme/web#3"].Recheck {
		t.Fatal("a pill behind has its own actions")
	}

	// stack.json's order wins over numbers and branches: 30 <- 10 <- 20.
	s = snapshotOf(
		awaitingPR(30, "main", awaitingAlex, "awaiting", false),
		awaitingPR(10, "main", awaitingAlex, "awaiting", false),
		awaitingPR(20, "main", "checks running", "pending", false),
	)
	s.Stacks = []Stack{{Repo: "acme/web", PRs: []int{30, 10, 20}}}
	v = Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#30[10 20]" {
		t.Fatalf("chain: %s", got)
	}

	// A PR that needs the human keeps its row, and the chain behind it is not stacked through it.
	s = snapshotOf(
		awaitingPR(1, "main", awaitingAlex, "awaiting", false),
		awaitingPR(2, "b1", "checks failing", "failing", true),
		awaitingPR(3, "b2", "checks running", "pending", false),
	)
	v = Build(s, fixtureNow, "acme")
	if pillKeys(v.Awaiting) != "acme/web#1" || len(v.NeedsYou) != 1 || len(v.InProgress) != 1 {
		t.Fatalf("awaiting %s, needs %+v, progress %+v", pillKeys(v.Awaiting), v.NeedsYou, v.InProgress)
	}

	// A live worker still working on a PR stacked on one: its row is the worker's, so it stays.
	s = snapshotOf(awaitingPR(1, "main", awaitingAlex, "awaiting", false))
	s.Workers = []Worker{{Name: "abc-5", Linear: "ABC-5", State: "live", Branch: "you/abc-5"}}
	s.Hooks["abc-5"] = Hook{State: "working"}
	s.Watch.Workers = map[string]WatchWorker{"abc-5": {PR: awaitingPR(5, "b1", "checks running", "pending", false)}}
	v = Build(s, fixtureNow, "acme")
	if pillKeys(v.Awaiting) != "acme/web#1" || find(v.InProgress, "abc-5").Label != "working" {
		t.Fatalf("awaiting %s, in progress %+v", pillKeys(v.Awaiting), v.InProgress)
	}

	// The ledger's branch stands in for a PR's own: an idle worker's PR, with a followed PR based on it.
	own := awaitingPR(6, "main", awaitingAlex, "awaiting", false)
	own.Branch = ""
	s = snapshotOf(awaitingPR(7, "you/abc-6", awaitingAlex, "awaiting", false))
	s.Workers = []Worker{{Name: "abc-6", State: "live", Branch: "you/abc-6"}}
	s.Hooks["abc-6"] = Hook{State: "idle"}
	s.Watch.Workers = map[string]WatchWorker{"abc-6": {PR: own}}
	v = Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#6[7]" {
		t.Fatalf("by the ledger's branch: %s", got)
	}

	// A cycle (bad data) neither loops nor loses a PR, and breaks the same way every time.
	s = snapshotOf(awaitingPR(1, "b2", awaitingAlex, "awaiting", false), awaitingPR(2, "b1", awaitingAlex, "awaiting", false))
	v = Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#2[1]" {
		t.Fatalf("cycle: %s", got)
	}
}

func TestDismissHoldsUntilTheHeadOrReviewChanges(t *testing.T) {
	s := snapshotOf(awaitingPR(1, "main", awaitingAlex, "awaiting", false), awaitingPR(2, "main", awaitingAlex, "awaiting", false))
	s.Dismissed = map[string]Dismissal{"acme/web#1": {Head: "sha1", Review: awaitingAlex}}
	if got := pillKeys(Build(s, fixtureNow, "acme").Awaiting); got != "acme/web#2" {
		t.Fatalf("dismissed: %s", got)
	}
	s.Watch.PRs["acme/web#1"].Head = "sha1-new" // a new commit
	if got := pillKeys(Build(s, fixtureNow, "acme").Awaiting); got != "acme/web#1 acme/web#2" {
		t.Fatalf("after a push: %s", got)
	}
	s.Watch.PRs["acme/web#1"].Head = "sha1"
	s.Watch.PRs["acme/web#1"].Move.Label = "awaiting review · alex, kim" // another reviewer asked
	if got := pillKeys(Build(s, fixtureNow, "acme").Awaiting); got != "acme/web#1 acme/web#2" {
		t.Fatalf("after a review change: %s", got)
	}

	// Dismissing a stack's front: an awaiting PR behind it is a pill of its own, the rest go back to In progress.
	// A dismissed draft behind a pill is hidden with it.
	s = snapshotOf(
		awaitingPR(1, "main", awaitingAlex, "awaiting", false),
		awaitingPR(2, "b1", awaitingAlex, "awaiting", false),
		awaitingPR(3, "b2", "checks running", "pending", false),
		awaitingPR(4, "b1", "draft", "draft", false),
	)
	s.Dismissed = map[string]Dismissal{"acme/web#1": {Head: "sha1", Review: awaitingAlex}}
	v := Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#2[3]" || len(v.InProgress) != 1 || v.InProgress[0].PR.Text != "#4" {
		t.Fatalf("front dismissed: %s, in progress %+v", got, v.InProgress)
	}
	// A dismissed PR is as if off the row: one behind is hidden, and a PR stacked only through it has its own row again.
	s.Dismissed = map[string]Dismissal{"acme/web#2": {Head: "sha2", Review: awaitingAlex}}
	v = Build(s, fixtureNow, "acme")
	if got := pillKeys(v.Awaiting); got != "acme/web#1[4]" || len(v.InProgress) != 1 || v.InProgress[0].PR.Text != "#3" || v.pills["pr:acme/web#2"] != nil {
		t.Fatalf("behind dismissed: %s, in progress %+v", got, v.InProgress)
	}
}

func TestRecheckingWhileQueued(t *testing.T) {
	s := snapshotOf(awaitingPR(1, "main", awaitingAlex, "awaiting", false))
	s.Actions = []Action{{Seq: 1, TS: fixtureNow - 30, Item: "pr:acme/web#1", Option: "recheck", Status: "queued"}}
	p := Build(s, fixtureNow, "acme").Awaiting[0]
	if !p.Rechecking || p.Actions.Recheck || !p.Actions.Dismiss {
		t.Fatalf("queued: %+v %+v", p, p.Actions)
	}
	for _, a := range []Action{
		{Seq: 1, TS: fixtureNow - 30, Item: "pr:acme/web#1", Option: "recheck", Status: "done"},
		{Seq: 1, TS: fixtureNow - 30, Item: "pr:acme/web#1", Option: "recheck", Status: "cancelled"},
		{Seq: 1, TS: fixtureNow - stuckAfter, Item: "pr:acme/web#1", Option: "recheck", Status: "queued"}, // athena never took it up
	} {
		s.Actions = []Action{a}
		if p := Build(s, fixtureNow, "acme").Awaiting[0]; p.Rechecking || !p.Actions.Recheck {
			t.Fatalf("%+v: %+v", a, p)
		}
	}
}

func TestShortTitle(t *testing.T) {
	cases := map[string]string{
		"Pin the Go toolchain":        "Pin the Go toolchain",
		strings.Repeat("x", shortMax): strings.Repeat("x", shortMax),
		"Checkout keeps the promo code when a cart is restored on another device after sign-in": "Checkout keeps the promo code when a cart is…",
		strings.Repeat("y", 60):                             strings.Repeat("y", shortMax-1) + "…",
		"  Spaces   fold\tto one  ":                         "Spaces fold to one",
		"Billing export, step 3: upload, to object storage": "Billing export, step 3: upload, to object…",
	}
	for in, want := range cases {
		got := shortTitle(in)
		if got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
		if n := utf8.RuneCountInString(got); n > shortMax {
			t.Errorf("%q: %d runes", got, n)
		}
	}
}
