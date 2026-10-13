package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRecheckQueuesForAthenaNamingThePR(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	code, out := post(t, srv, base, `{"item": "pr:acme/web#907", "option": "recheck"}`, nil)
	if code != http.StatusAccepted || out["seq"] != 1.0 {
		t.Fatalf("%d %v", code, out)
	}
	if got := queued(t, dir); len(got) != 1 || got[0] != (Action{1, fixtureNow, "pr:acme/web#907", "recheck", "", false, "queued"}) {
		t.Fatalf("%+v", got)
	}
	// The pill stays, re-checking, with no second Re-check until athena takes this one up.
	main, _, _ := srv.snapshot()
	pill := between(string(main), `<div class="apr rechecking" id="aw-acme-web-907">`, `</div>
</div>`)
	if pill == "" || strings.Contains(pill, `data-option="recheck"`) || !strings.Contains(pill, `data-option="dismiss"`) || !strings.Contains(pill, `, re-checking"`) {
		t.Fatalf("pill %q", pill)
	}
	if code, out := post(t, srv, base, `{"item": "pr:acme/web#907", "option": "recheck"}`, nil); code != http.StatusUnprocessableEntity {
		t.Fatalf("a second re-check: %d %v", code, out)
	}
	// Undo, as for any answer: Re-check is offered again.
	if code, out := post(t, srv, base, `{"seq": 1, "undo": true}`, nil); code != http.StatusAccepted {
		t.Fatalf("undo: %d %v", code, out)
	}
	main, _, _ = srv.snapshot()
	if !strings.Contains(string(main), `<div class="apr" id="aw-acme-web-907">`) {
		t.Fatal("still re-checking after the undo")
	}
	// A pill stacked behind another has its own Re-check.
	if code, out := post(t, srv, base, `{"item": "pr:acme/web#909", "option": "recheck"}`, nil); code != http.StatusAccepted {
		t.Fatalf("behind: %d %v", code, out)
	}
}

func TestPillActionsTheServerRefuses(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for _, c := range []struct {
		body string
		code int
	}{
		{`{"item": "pr:acme/web#4242", "option": "recheck"}`, 422}, // a PR the dash does not show
		{`{"item": "pr:acme/web#48", "option": "recheck"}`, 422},   // shown, but as a Needs you row
		{`{"item": "pr:Acme/Web#907", "option": "recheck"}`, 400},  // keys are lower case, as prKey makes them
		{`{"item": "pr:acme/web#907; rm -rf", "option": "recheck"}`, 400},
		{`{"item": "pr:https://github.com/acme/web/pull/907", "option": "recheck"}`, 400},
		{`{"item": "pr:acme/web#0", "option": "recheck"}`, 400},
		{`{"item": "pr:acme/web#907", "discuss": true}`, 422},
		{`{"item": "pr:acme/web#907", "text": "fix it"}`, 422},
		{`{"item": "pr:acme/web#907", "option": "merge"}`, 422},
		{`{"item": "w-abc-101", "option": "recheck"}`, 422},
		{`{"item": "a5", "option": "recheck"}`, 422},
		{`{"item": "pr:acme/web#907", "undo": true}`, 409}, // not dismissed
		{`{"item": "pr:acme/web#907", "option": "dismiss", "undo": true}`, 400},
	} {
		if code, out := post(t, srv, base, c.body, nil); code != c.code {
			t.Errorf("%s: %d %v, want %d", c.body, code, out, c.code)
		}
	}
	if got := queued(t, dir); len(got) != 0 {
		t.Fatalf("queued %+v", got)
	}
}

func TestDismissIsTheDashsOwnAndUndoes(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	code, out := post(t, srv, base, `{"item": "pr:acme/web#907", "option": "dismiss"}`, nil)
	if code != http.StatusAccepted || out["seq"] != 0.0 {
		t.Fatalf("%d %v", code, out)
	}
	// Never in actions.jsonl, so the doorbell does not ring and athena is not woken.
	if _, err := os.Stat(filepath.Join(dir, actionsFile)); !os.IsNotExist(err) {
		t.Fatalf("actions.jsonl written: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(dir, dismissFile))
	if err != nil {
		t.Fatal(err)
	}
	var file struct{ PRs map[string]map[string]any }
	json.Unmarshal(data, &file)
	if d := file.PRs["acme/web#907"]; d["head"] != "4f2c1ab9d0e3" || d["review"] != "awaiting review · alex" || d["ts"] != float64(fixtureNow) {
		t.Fatalf("dismissed-prs.json %s", data)
	}
	if fi, _ := os.Stat(filepath.Join(dir, dismissFile)); fi.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", fi.Mode())
	}
	main, _, _ := srv.snapshot()
	if strings.Contains(string(main), `id="aw-acme-web-907"`) || !strings.Contains(string(main), `Awaiting review <span class="count">6</span>`) {
		t.Fatal("the dismissed pill is still on the page")
	}
	if code, _ := post(t, srv, base, `{"item": "pr:acme/web#907", "option": "dismiss"}`, nil); code != http.StatusUnprocessableEntity {
		t.Fatalf("a dismissed pill offers nothing: %d", code)
	}
	// Undo names the PR, as a dismiss has no seq.
	if code, out := post(t, srv, base, `{"item": "pr:acme/web#907", "undo": true}`, nil); code != http.StatusAccepted {
		t.Fatalf("undo: %d %v", code, out)
	}
	main, _, _ = srv.snapshot()
	if !strings.Contains(string(main), `id="aw-acme-web-907"`) {
		t.Fatal("undo did not bring the pill back")
	}
	if code, _ := post(t, srv, base, `{"item": "pr:acme/web#907", "undo": true}`, nil); code != http.StatusConflict {
		t.Fatalf("a second undo: %d", code)
	}

	// Dismissed again, it shows once its PR has a new head commit.
	post(t, srv, base, `{"item": "pr:acme/web#907", "option": "dismiss"}`, nil)
	watch, _ := os.ReadFile(filepath.Join(dir, watchFile))
	os.WriteFile(filepath.Join(dir, watchFile), []byte(strings.Replace(string(watch), `"head": "4f2c1ab9d0e3",
        "merge_state": "BLOCKED",
        "mergeable": "MERGEABLE",
        "move": {
          "kind": "awaiting",
          "label": "awaiting review · alex",
          "needs": false
        },
        "number": 907`, `"head": "0000000aaaaa",
        "merge_state": "BLOCKED",
        "mergeable": "MERGEABLE",
        "move": {
          "kind": "awaiting",
          "label": "awaiting review · alex",
          "needs": false
        },
        "number": 907`, 1)), 0o644)
	srv.Refresh(true)
	main, _, _ = srv.snapshot()
	if !strings.Contains(string(main), `id="aw-acme-web-907"`) {
		t.Fatal("a new commit did not bring the pill back")
	}
}

func TestDismissingAStacksFrontPromotesTheNext(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	if code, out := post(t, srv, base, `{"item": "pr:acme/web#906", "option": "dismiss"}`, nil); code != http.StatusAccepted {
		t.Fatalf("%d %v", code, out)
	}
	_, v, _ := srv.snapshot()
	if got := pillKeys(v.Awaiting); got != "acme/api#71 acme/docs#212 acme/web#907 acme/web#908[909] acme/web#6452" {
		t.Fatalf("awaiting: %s", got)
	}
}

func TestTheRowIsAbsentWithNothingAwaiting(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for _, k := range []string{"906", "907", "6452"} {
		post(t, srv, base, `{"item": "pr:acme/web#`+k+`", "option": "dismiss"}`, nil)
	}
	post(t, srv, base, `{"item": "pr:acme/web#908", "option": "dismiss"}`, nil) // promoted when 906 went
	post(t, srv, base, `{"item": "pr:acme/api#71", "option": "dismiss"}`, nil)
	post(t, srv, base, `{"item": "pr:acme/docs#212", "option": "dismiss"}`, nil)
	main, v, _ := srv.snapshot()
	if len(v.Awaiting) != 0 || strings.Contains(string(main), "awaiting") || strings.Contains(string(main), `class="apr`) {
		t.Fatalf("the row is still there: %v", pillKeys(v.Awaiting))
	}
	// #909 waited on #908 only: back in In progress, its own row.
	if !strings.Contains(string(main), `id="pr-acme-web-909"`) {
		t.Fatal("#909 has no place")
	}
}

func between(s, start, end string) string {
	i := strings.Index(s, start)
	if i < 0 {
		return ""
	}
	j := strings.Index(s[i:], end)
	if j < 0 {
		return ""
	}
	return s[i : i+j]
}
