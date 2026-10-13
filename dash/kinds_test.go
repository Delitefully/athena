package main

import (
	"bytes"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// rowPills renders v and lists the pills of row id in order: each data-option, discuss, reply, or open-pr for the link.
func rowPills(t *testing.T, v View, id string) string {
	t.Helper()
	var buf bytes.Buffer
	if err := templates(func() time.Time { return unix(fixtureNow) }).ExecuteTemplate(&buf, "main", v); err != nil {
		t.Fatal(err)
	}
	row := between(buf.String(), `<li id="`+id+`"`, "</li>")
	if row == "" {
		t.Fatalf("no row %s in %s", id, buf.String())
	}
	row, _, _ = strings.Cut(row, `<div class="fold"`) // an ask folded under it has pills of its own
	var out []string
	for _, tag := range regexp.MustCompile(`<(?:button|a) [^>]*class="pill[^"]*"[^>]*>`).FindAllString(row, -1) {
		switch {
		case strings.Contains(tag, "data-discuss"):
			out = append(out, "discuss")
		case strings.Contains(tag, "data-reply"):
			out = append(out, "reply")
		case strings.HasPrefix(tag, "<a "):
			out = append(out, "open-pr")
		default:
			out = append(out, regexp.MustCompile(`data-option="([^"]+)"`).FindStringSubmatch(tag)[1])
		}
	}
	return strings.Join(out, " ")
}

func TestEachKindOffersItsButtons(t *testing.T) {
	for _, c := range []struct {
		kind         string
		athenaMerges bool
		want         string
	}{
		{"unrequested", false, "request-review discuss"},
		{"failing", false, "recheck discuss"},
		{"approved", false, "open-pr discuss"}, // a human-merge repo: the human merges on GitHub
		{"approved", true, "open-pr go discuss"},
		{"mergeable", false, "open-pr discuss"},
		{"mergeable", true, "open-pr go discuss"},
		{"changes", false, "open-pr discuss"},
		{"changes", true, "open-pr discuss"}, // Merge is for a PR that is ready, nothing else
	} {
		p := followed(20, "", "its label", c.kind, true)
		p.Head, p.Move.AthenaMerges = "4f2c1ab9d0e3", c.athenaMerges
		v := Build(snapshotOf(p), fixtureNow, "acme")
		if got := rowPills(t, v, "pr-acme-web-20"); got != c.want {
			t.Errorf("%s (athena merges %v): %q, want %q", c.kind, c.athenaMerges, got, c.want)
		}
		a := v.actionable["pr:acme/web#20"]
		if a == nil || !a.Discuss || a.Reply || a.Dismiss || a.Hides {
			t.Errorf("%s: the row's actions %+v", c.kind, a)
		}
	}
}

func TestKindButtonsAreNamedAndLinkThePR(t *testing.T) {
	p := followed(20, "", "your go to merge", "mergeable", true)
	p.Head, p.Move.AthenaMerges = "4f2c1ab9d0e3", true
	var buf bytes.Buffer
	templates(time.Now).ExecuteTemplate(&buf, "main", Build(snapshotOf(p), fixtureNow, "acme"))
	page := buf.String()
	for _, want := range []string{
		`<div class="acts options" data-item="pr:acme/web#20">`, // Discuss shows as its icon alone, as beside athena's options
		`<a class="pill" href="https://github.com/acme/web/pull/20" aria-label="Open PR #20 on GitHub">`,
		`<button type="button" class="pill" data-option="go" data-head="4f2c1ab9d0e3" aria-label="Merge: tell athena go">`,
	} {
		if !strings.Contains(page, want) {
			t.Errorf("missing %s in %s", want, page)
		}
	}
	q := followed(21, "", "no reviewer requested", "unrequested", true)
	f := followed(22, "", "checks failing", "failing", true)
	buf.Reset()
	templates(time.Now).ExecuteTemplate(&buf, "main", Build(snapshotOf(q, f), fixtureNow, "acme"))
	for _, want := range []string{`data-option="request-review" aria-label="Request review"`, `data-option="recheck" aria-label="Re-check and fix"`} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("missing %s", want)
		}
	}
}

func TestMergeOnlyWithAHeadToBindTheGoTo(t *testing.T) {
	p := followed(20, "", "your go to merge", "mergeable", true)
	p.Move.AthenaMerges = true // but watch.json has no head for it
	if got := rowPills(t, Build(snapshotOf(p), fixtureNow, "acme"), "pr-acme-web-20"); got != "open-pr discuss" {
		t.Fatalf("%q", got)
	}
}

func TestOtherRowsKeepTheirButtons(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	// A blocked live worker: Discuss and Reply, on its row, as before.
	if got := rowPills(t, v, "w-abc-102"); got != "discuss reply" {
		t.Errorf("a blocked worker: %q", got)
	}
	// A live worker whose PR is approved: the PR's buttons, its Discuss naming the PR.
	if got := rowPills(t, v, "w-abc-108"); got != "open-pr discuss" {
		t.Errorf("abc-108: %q", got)
	}
	if v.actionable["w-abc-108"] != nil || v.actionable["pr:acme/web#48"] == nil {
		t.Error("abc-108's row answers for its PR")
	}
	// A retired worker's open PR, changes requested.
	if got := rowPills(t, v, "w-abc-104"); got != "open-pr discuss" {
		t.Errorf("abc-104: %q", got)
	}
	// Your review requested: nothing, as before.
	if got := rowPills(t, v, "rr-acme-web-6170"); got != "" {
		t.Errorf("a review request: %q", got)
	}
	// In progress rows placed by their PR's state offer nothing.
	for _, it := range v.InProgress {
		if it.moved && it.Actions != nil && it.Actions.Item != "w-"+it.Name {
			t.Errorf("%s: %+v", it.ID, it.Actions)
		}
	}
}

func TestAQueuedKindAnswerShowsAsSent(t *testing.T) {
	p := followed(20, "", "your go to merge", "mergeable", true)
	p.Head, p.Move.AthenaMerges = "4f2c1ab9d0e3", true
	s := snapshotOf(p)
	s.Actions = []Action{{Seq: 1, TS: fixtureNow - 30, Item: "pr:acme/web#20", Option: "go", Status: "queued"}}
	v := Build(s, fixtureNow, "acme")
	if got := rowPills(t, v, "pr-acme-web-20"); got != "open-pr discuss" {
		t.Fatalf("a second go is offered: %q", got)
	}
	if it := find(v.NeedsYou, "web"); it.Sent != "merge go sent" {
		t.Fatalf("sent %q", it.Sent)
	}
	// Past stuckAfter it is offered again.
	if got := rowPills(t, Build(s, fixtureNow+stuckAfter, "acme"), "pr-acme-web-20"); got != "open-pr go discuss" {
		t.Fatalf("stuck: %q", got)
	}
	// A Discuss waiting does not hold the buttons back.
	s.Actions[0].Option, s.Actions[0].Discuss = "", true
	if got := rowPills(t, Build(s, fixtureNow, "acme"), "pr-acme-web-20"); got != "open-pr go discuss" {
		t.Fatalf("discuss queued: %q", got)
	}
}

func TestAKindActionReachesTheQueue(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for i, body := range []string{
		`{"item": "pr:acme/web#912", "option": "request-review"}`,
		`{"item": "pr:acme/web#913", "option": "recheck"}`,
		`{"item": "pr:acme/api#75", "option": "go", "head": "8d1c2b3a4f5e"}`,
		`{"item": "pr:acme/web#48", "discuss": true}`,
	} {
		if code, out := post(t, srv, base, body, nil); code != http.StatusAccepted || out["seq"] != float64(i+1) {
			t.Fatalf("%s: %d %v", body, code, out)
		}
	}
	got := queued(t, dir)
	if len(got) != 4 || got[0].Option != "request-review" || got[1].Option != "recheck" || got[2].Option != "go" || !got[3].Discuss {
		t.Fatalf("%+v", got)
	}
	// The go names the head the human saw, so athena merges that commit or none.
	data, _ := os.ReadFile(filepath.Join(dir, actionsFile))
	if !strings.Contains(string(data), `"head":"8d1c2b3a4f5e"`) || strings.Count(string(data), `"head"`) != 1 {
		t.Fatalf("actions.jsonl %s", data)
	}
	// One at a time: the row says it is sent, and a second is refused.
	if code, _ := post(t, srv, base, `{"item": "pr:acme/api#75", "option": "go", "head": "8d1c2b3a4f5e"}`, nil); code != http.StatusUnprocessableEntity {
		t.Fatalf("a second go: %d", code)
	}
}

func TestKindActionsTheServerRefuses(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for _, c := range []struct {
		body string
		code int
	}{
		{`{"item": "pr:acme/web#4242", "option": "request-review"}`, 422}, // a PR the dash does not show
		{`{"item": "pr:acme/web#41", "option": "recheck"}`, 422},          // shown, but In progress, by its worker's state
		{`{"item": "pr:acme/web#912", "option": "go", "head": "a1b2c3d4e5f6"}`, 422}, // not ready to merge
		{`{"item": "pr:acme/web#912", "option": "recheck"}`, 422},         // not the button its kind has
		{`{"item": "pr:acme/web#48", "option": "go", "head": "1a2b3c4d5e6f"}`, 422},  // a human-merge repo: no Merge
		{`{"item": "pr:acme/web#48", "text": "merge it"}`, 422},           // no Reply on a PR's row
		{`{"item": "pr:acme/api#75", "option": "go"}`, 409},               // a go must name the head it is for
		{`{"item": "pr:acme/api#75", "option": "go", "head": "0000000000aa"}`, 409}, // the PR moved on since the page
		{`{"item": "pr:acme/web#912", "option": "request-review", "head": "a1b2c3d4e5f6"}`, 400}, // a head is a go's only
		{`{"item": "w-abc-108", "discuss": true}`, 422},                   // its row answers for its PR now
	} {
		if code, out := post(t, srv, base, c.body, nil); code != c.code {
			t.Errorf("%s: %d %v, want %d", c.body, code, out, c.code)
		}
	}
	if got := queued(t, dir); len(got) != 0 {
		t.Fatalf("queued %+v", got)
	}
}
