package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTheHealthDotIsNeverGreenWithoutAFreshCheck(t *testing.T) {
	now := float64(fixtureNow)
	for _, c := range []struct {
		h     *Health
		state string
		text  string
	}{
		{nil, "unknown", "doctor hasn't reported"},
		{&Health{TS: now - healthStale - 1, OK: true}, "unknown", "doctor not running · last check " + unix(now-healthStale-1).Local().Format("15:04")},
		{&Health{TS: now - 3*24*3600, OK: true}, "unknown", "doctor not running · last check " + unix(now-3*24*3600).Local().Format("Mon 15:04")},
		{&Health{TS: now - 30, OK: true}, "ok", "all ok"},
		{&Health{TS: now - 30, OK: false}, "bad", "something is wrong"},
		{&Health{TS: now - 30, Problems: []Problem{{Check: "dash", Detail: "down since 09:12"}, {Check: "board"}}}, "bad", "dash: down since 09:12 · +1 more"},
	} {
		got := healthView(c.h, now)
		if got.State != c.state || got.Text != c.text {
			t.Errorf("%+v: %+v", c.h, got)
		}
	}
	// Stale or missing, the tooltip names the fix: the service that runs doctor every minute, or one check now.
	for _, h := range []*Health{nil, {TS: now - healthStale - 1, OK: true}} {
		if v := healthView(h, now); !strings.Contains(v.Title, "athena service install") || !strings.Contains(v.Title, "athena doctor --fix") {
			t.Errorf("%+v: the tooltip names no fix: %q", h, v.Title)
		}
	}
	// health.json as v2's doctor writes it: a problem makes it not ok, whatever its ok says.
	var m map[string]any
	json.Unmarshal([]byte(`{"ts": 1791499990, "ok": true, "problems": [{"check": "hq", "detail": "messaging socket missing", "since": 1791499000}],
		"checks": {"hq": {"ok": false, "detail": "messaging socket missing", "fixed": false}}}`), &m)
	h := parseHealth(m)
	if h.OK || len(h.Problems) != 1 || h.Problems[0].Since != 1791499000 {
		t.Fatalf("%+v", h)
	}
	if v := healthView(h, now); v.State != "bad" || v.Text != "hq: messaging socket missing" || v.Title != "hq: messaging socket missing" {
		t.Fatalf("%+v", v)
	}
	// On the page: one dot, and a change to health.json is pushed.
	dir := copyFixture(t)
	srv, base := start(t, dir)
	if _, page := get(t, base+"/", ""); !strings.Contains(page, `<p class="health ok" title="athena doctor&#39;s last check: `) {
		t.Fatal("no green dot")
	}
	write(t, filepath.Join(dir, healthFile), `{"ts": 1791499990, "ok": false, "problems": [{"check": "board", "detail": "watcher gone"}]}`)
	if !srv.Refresh(false) {
		t.Fatal("a new health.json is not a change")
	}
	if main, _, _ := srv.snapshot(); !strings.Contains(string(main), `<p class="health bad" title="board: watcher gone" role="status"><span class="dot" aria-hidden="true"></span>board: watcher gone</p>`) {
		t.Fatalf("%s", main[:400])
	}
	os.Remove(filepath.Join(dir, healthFile))
	srv.Refresh(false)
	if main, _, _ := srv.snapshot(); !strings.Contains(string(main), `<p class="health unknown" title="athena doctor has not reported`) ||
		!strings.Contains(string(main), `</span>doctor hasn&#39;t reported</p>`) {
		t.Fatal("no health.json reads as unknown, saying so")
	}
}

func decisionWithDefault() Ask {
	return Ask{ID: "a1", Kind: "decision", Next: "you", Name: "Flaky e2e", Text: "Re-run it?", Rec: "re-run", Auto: true,
		Options: []Option{{ID: "re-run", Label: "Re-run", Do: "reply"}}, Created: fixtureNow - 3600, Updated: fixtureNow - 3600}
}

// Decision 4: a default applies on its own after 12 h, or 2 h once the human has been away, and only to a
// reversible, low-stakes call: never one marked security, customer, money or one-way, whatever the file says.
func TestADefaultAppliesOnlyWhenDueAndNeverToACallWithStakes(t *testing.T) {
	now := float64(fixtureNow)
	ids := func(s Snapshot, at float64) string {
		var out []string
		for _, a := range dueDefaults(s, at) {
			out = append(out, a.ID)
		}
		return strings.Join(out, " ")
	}
	a := decisionWithDefault()
	s := Snapshot{Asks: []Ask{a}, LastSeen: now - 60}
	if got := ids(s, now); got != "" {
		t.Fatalf("an hour old, the human here: %q", got)
	}
	if got := ids(s, a.Created+defaultAfter); got != "a1" {
		t.Fatalf("12 h on: %q", got)
	}
	// Away since 2 h ago: 2 h after the later of the item's filing (an hour ago) and their leaving, so in an hour.
	s.LastSeen = now - awayAfter
	if got := ids(s, now); got != "" {
		t.Fatalf("away, the item an hour old: %q", got)
	}
	if at := applyAt(a, s.LastSeen, now); at != now+3600 {
		t.Fatalf("applyAt %v", at-now)
	}
	if got := ids(s, now+3600); got != "a1" {
		t.Fatalf("away, 2 h after filing: %q", got)
	}
	s.LastSeen = now - 3*awayAfter // gone long before it was filed
	if got := ids(s, a.Created+awayAfter); got != "a1" {
		t.Fatalf("away before it was filed: %q", got)
	}
	s.LastSeen = 0 // nothing known of the human: only the 12 h
	if got := ids(s, now+3*3600); got != "" {
		t.Fatalf("no activity known: %q", got)
	}
	// Never for a call with stakes, even marked auto by hand in the file, past both times.
	for _, stake := range []string{"security", "customer", "money", "one-way"} {
		b := a
		b.Stakes = []string{stake}
		if got := ids(Snapshot{Asks: []Ask{b}, LastSeen: now - 10*awayAfter}, now+2*defaultAfter); got != "" {
			t.Errorf("%s: applied", stake)
		}
	}
	var m map[string]any
	json.Unmarshal([]byte(`{"items": [{"id": "a1", "kind": "decision", "name": "Rotate key", "text": "Rotate it?", "rec": "yes", "auto": true,
		"stakes": ["money"], "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}`), &m)
	if p := parseAsks(m); p[0].Auto || len(p[0].Stakes) != 1 {
		t.Fatalf("parsed %+v", p[0])
	}
	// Nor once answered, snoozed, applied, resolved, or for an option it does not offer, or anything but a decision.
	for name, change := range map[string]func(*Snapshot){
		"answered": func(s *Snapshot) { s.Actions = []Action{{Seq: 1, Item: "a1", Option: "re-run", Status: "queued"}} },
		"done":     func(s *Snapshot) { s.Actions = []Action{{Seq: 1, Item: "a1", Discuss: true, Status: "done"}} },
		"snoozed":  func(s *Snapshot) { s.Asks[0].SnoozeUntil, s.Asks[0].SnoozedAt = now+2*defaultAfter, now },
		"applied":  func(s *Snapshot) { s.Asks[0].Applied = &Applied{Option: "re-run"} },
		"resolved": func(s *Snapshot) { s.Asks[0].Resolved = now },
		"no rec":   func(s *Snapshot) { s.Asks[0].Rec = "nope" },
		"a result": func(s *Snapshot) { s.Asks[0].Kind = "result" },
	} {
		s := Snapshot{Asks: []Ask{decisionWithDefault()}, LastSeen: now - 60}
		change(&s)
		if got := ids(s, now+defaultAfter); got != "" {
			t.Errorf("%s: applied", name)
		}
	}
	// A cancelled answer (undone within its seconds) leaves it due.
	s = Snapshot{Asks: []Ask{a}, Actions: []Action{{Seq: 1, Item: "a1", Option: "re-run", Status: "cancelled"}}}
	if got := ids(s, now+defaultAfter); got != "a1" {
		t.Fatalf("cancelled: %q", got)
	}
}

// The server applies a due default as an answer for athena, marked auto, once; the item moves to Ready for you, saying
// so, with Done and Undo; the human's later activity counts, an auto answer does not.
func TestTheServerAppliesADueDefaultOnceAndShowsIt(t *testing.T) {
	dir := copyFixture(t)
	srv, _ := start(t, dir)
	srv.now = func() time.Time { return time.Unix(fixtureNow-2000+defaultAfter+5, 0) }
	srv.Refresh(true)
	srv.Refresh(true)
	got := queued(t, dir)
	if len(got) != 1 || got[0].Item != "a1" || got[0].Option != "later" || !got[0].Auto || got[0].Status != "queued" {
		t.Fatalf("%+v", got)
	}
	main, view, _ := srv.snapshot()
	r := find(view.Ready, "Export queue start")
	if r.Label != "athena is taking the rec on its own: After ABC-101" || r.Actions == nil || !r.Actions.Done || r.Actions.Undo != "After ABC-101" {
		t.Fatalf("a1 %+v", r)
	}
	if !strings.Contains(string(main), `data-option="undo-default" data-tip="Undo After ABC-101: athena takes it back"`) {
		t.Fatal("no Undo on the row")
	}
	if s := srv.store.Load(); s.LastSeen != fixtureNow-2100 {
		t.Fatalf("an auto answer is not the human's activity: %v", s.LastSeen)
	}
	// The security item (a6) is a week old and marked: never applied.
	for _, a := range got {
		if a.Item == "a6" {
			t.Fatal("a6 applied")
		}
	}
	data, _ := os.ReadFile(filepath.Join(dir, actionsFile))
	if !strings.Contains(string(data), `"auto":true`) {
		t.Fatalf("%s", data)
	}
}

func itemPost(t *testing.T, srv *Server, base, body string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest("POST", base+"/api/item", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", base)
	req.Header.Set("X-Athena-Token", srv.token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func itemIn(t *testing.T, dir, id string) map[string]any {
	t.Helper()
	var file struct {
		Next  int              `json:"next"`
		Items []map[string]any `json:"items"`
	}
	data, err := os.ReadFile(filepath.Join(dir, asksFile))
	if err != nil || json.Unmarshal(data, &file) != nil || file.Next != 13 {
		t.Fatalf("needs-you.json: %v %s", err, data)
	}
	for _, it := range file.Items {
		if it["id"] == id {
			return it
		}
	}
	return nil
}

// Done and Snooze are the dash's own: they work while HQ is busy or down, write needs-you.json themselves under its
// lock, keep every other field, and never queue anything for athena.
func TestDoneAndSnoozeWorkWithoutHQ(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	if code, out := itemPost(t, srv, base, `{"item": "a3", "do": "done"}`); code != 200 {
		t.Fatalf("%d %v", code, out)
	}
	a3 := itemIn(t, dir, "a3")
	if a3["resolved"] != float64(fixtureNow) || a3["resolved_by"] != "you" || a3["body"] == nil || a3["name"] != "Palette film" {
		t.Fatalf("a3 %v", a3)
	}
	if log, _ := a3["log"].([]any); len(log) != 1 || log[0].(map[string]any)["text"] != "you marked it done" {
		t.Fatalf("a3's timeline %v", a3["log"])
	}
	if _, err := os.Stat(filepath.Join(dir, actionsFile)); !os.IsNotExist(err) {
		t.Fatal("Done queued something for athena")
	}
	if fi, _ := os.Stat(filepath.Join(dir, asksFile)); fi.Mode().Perm() != 0o600 {
		t.Fatalf("needs-you.json is %v", fi.Mode().Perm())
	}
	_, view, _ := srv.snapshot()
	if find(view.Ready, "a3").ID != "" || find(view.Done, "a3").Label != "you marked it done" {
		t.Fatal("a3 did not move to Done today")
	}
	// Undo is Reopen, for what the human marked done here.
	if code, _ := itemPost(t, srv, base, `{"item": "a3", "do": "reopen"}`); code != 200 || itemIn(t, dir, "a3")["resolved"] != nil {
		t.Fatalf("reopen: %d", code)
	}
	// Snooze: off the page until then; Wake brings it back.
	until := fixtureNow + 7200
	if code, out := itemPost(t, srv, base, `{"item": "a5", "do": "snooze", "until": `+itoa(until)+`}`); code != 200 {
		t.Fatalf("%d %v", code, out)
	}
	if sn, _ := itemIn(t, dir, "a5")["snooze"].(map[string]any); sn["until"] != float64(until) || sn["at"] != float64(fixtureNow+1) {
		t.Fatalf("a5 %v", itemIn(t, dir, "a5"))
	}
	_, view, _ = srv.snapshot()
	if find(view.NeedsYou, "a5").ID != "" || view.Snoozed != 2 {
		t.Fatalf("a5 still shows; snoozed %d", view.Snoozed)
	}
	srv.now = func() time.Time { return time.Unix(int64(until)+1, 0) }
	srv.Refresh(true)
	if _, view, _ = srv.snapshot(); find(view.NeedsYou, "a5").ID == "" {
		t.Fatal("a5 is not back when its time is up")
	}
	srv.now = func() time.Time { return time.Unix(fixtureNow, 0) }
	srv.Refresh(true)
	if code, _ := itemPost(t, srv, base, `{"item": "a5", "do": "wake"}`); code != 200 || itemIn(t, dir, "a5")["snooze"] != nil {
		t.Fatalf("wake: %d", code)
	}
	// What it refuses.
	for body, want := range map[string]int{
		`{"item": "a10", "do": "done"}`:                       http.StatusConflict, // resolved already
		`{"item": "a1", "do": "reopen"}`:                      http.StatusConflict, // open
		`{"item": "a99", "do": "done"}`:                       http.StatusNotFound,
		`{"item": "a1", "do": "snooze", "until": 1}`:          http.StatusBadRequest, // in the past
		`{"item": "a1", "do": "snooze", "until": 9999999999}`: http.StatusBadRequest, // past 60 days
		`{"item": "a1", "do": "merge"}`:                       http.StatusBadRequest,
		`{"item": "a1", "do": "done", "until": 1791503600}`:   http.StatusBadRequest,
		`{"item": "../x", "do": "done"}`:                      http.StatusBadRequest,
		`{"item": "a1", "do": "done", "extra": true}`:         http.StatusBadRequest,
		`{"item": "a1", "do": "wake"}`:                        http.StatusConflict,
	} {
		if code, _ := itemPost(t, srv, base, body); code != want {
			t.Errorf("%s: %d, want %d", body, code, want)
		}
	}
	// The same checks as an answer: this page's origin and token only.
	req, _ := http.NewRequest("POST", base+"/api/item", strings.NewReader(`{"item": "a1", "do": "done"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "null")
	req.Header.Set("X-Athena-Token", srv.token)
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("a preview's POST: %d", resp.StatusCode)
	}
	if itemIn(t, dir, "a1")["resolved"] != nil {
		t.Fatal("a1 was resolved")
	}
}

// Ages and due dates as the rows say them.
func TestAgesAndDueDates(t *testing.T) {
	for d, want := range map[time.Duration]string{30 * time.Second: "now", 10 * time.Minute: "10m", 2 * time.Hour: "2h", 7 * 24 * time.Hour: "7d"} {
		if got := short(d); got != want {
			t.Errorf("%v: %q", d, got)
		}
	}
	now := time.Date(2026, 10, 27, 10, 0, 0, 0, time.Local)
	for due, want := range map[time.Time]string{
		now.Add(-2 * time.Hour):                           "overdue 2h",
		now.Add(40 * time.Minute):                         "due in 40m",
		time.Date(2026, 10, 27, 17, 30, 0, 0, time.Local): "due 17:30",
		time.Date(2026, 10, 28, 11, 30, 0, 0, time.Local): "due tmrw",
		time.Date(2026, 10, 29, 9, 0, 0, 0, time.Local):   "due Thu",
		time.Date(2026, 11, 14, 9, 0, 0, 0, time.Local):   "due 14 Nov",
	} {
		if got := dueText(due, now); got != want {
			t.Errorf("%v: %q, want %q", due, got, want)
		}
	}
	js, _ := staticFS.ReadFile("static/app.js")
	for _, fn := range []string{"function short(", "function dueText("} {
		if !strings.Contains(string(js), fn) {
			t.Errorf("app.js lacks %s: the page's own timer keeps these fresh", fn)
		}
	}
}

// Needs you shows at most needsShown rows; the rest fold behind "n more", saying when the first default applies.
func TestNeedsYouFoldsPastSevenRows(t *testing.T) {
	dir := copyFixture(t)
	// Two calls newer than the fixture's default (a1) push it below the fold, newest first.
	path := filepath.Join(dir, "needs-you.json")
	var file map[string]any
	data, _ := os.ReadFile(path)
	json.Unmarshal(data, &file)
	for i, id := range []string{"n1", "n2"} {
		file["items"] = append(file["items"].([]any), map[string]any{"id": id, "kind": "decision", "next": "you", "name": "New call " + id,
			"text": "Go ahead?", "created": fixtureNow - 60*(i+1), "updated": fixtureNow - 60*(i+1)})
	}
	data, _ = json.Marshal(file)
	os.WriteFile(path, data, 0o600)
	_, base := start(t, dir)
	_, page := get(t, base+"/", "")
	needs := between(page, `<section class="group needs"`, `</section>`)
	shown := between(needs, "<ul>", `<details class="more"`)
	if n := strings.Count(shown, `<li id=`); n != needsShown {
		t.Fatalf("%d rows before the fold", n)
	}
	if !strings.Contains(needs, `<details class="more" data-fold="needs"><summary>8 more · athena takes the rec at `) || !strings.Contains(needs, "unless you object</summary>") {
		t.Fatalf("%s", needs)
	}
}

// A push replaces <main> (app.js render), and a fold the human opened stays open: each fold has a key that holds
// across pages, app.js reopens the folds open under that key before the page shows, and a fold that grew shows every
// row it now has. The page has no JS test setup, so this checks the page and the code that keeps the folds.
func TestAPushKeepsAnOpenFoldOpen(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	folds := func() (string, string) {
		_, page := get(t, base+"/", "")
		return between(page, `<details class="more" data-fold="needs">`, `</details>`), between(page, `<details class="group done" data-fold="done"`, `</details>`)
	}
	needs, done := folds()
	if strings.Count(needs, `<li id=`) != 6 || done == "" {
		t.Fatalf("before: needs fold %q, done fold %q", needs, done)
	}
	path := filepath.Join(dir, "needs-you.json")
	var file map[string]any
	data, _ := os.ReadFile(path)
	json.Unmarshal(data, &file)
	for _, id := range []string{"g1", "g2"} {
		file["items"] = append(file["items"].([]any), map[string]any{"id": id, "kind": "decision", "next": "you", "name": "Grown " + id,
			"text": "Go ahead?", "created": fixtureNow - 7200, "updated": fixtureNow - 7200})
	}
	data, _ = json.Marshal(file)
	os.WriteFile(path, data, 0o600)
	srv.Refresh(true)
	if needs, done = folds(); strings.Count(needs, `<li id=`) != 8 || done == "" {
		t.Fatalf("after the push, the folds keep their keys and the grown one holds all 8 rows: %q, %q", needs, done)
	}

	js, _ := staticFS.ReadFile("static/app.js")
	render := between(string(js), "function render(html) {", "\n}\n")
	save := strings.Index(render, `main.querySelectorAll("details[data-fold][open]:not([data-filtered])")`)
	swap := strings.Index(render, "main.innerHTML = html;")
	reopen := strings.Index(render, `main.querySelector(`+"`details[data-fold=\"${CSS.escape(key)}\"]`"+`)`)
	filter := strings.Index(render, "filter();")
	if save < 0 || swap < save || reopen < swap || filter < reopen {
		t.Fatalf("render keeps no open fold across a push (save %d, swap %d, reopen %d, filter %d)", save, swap, reopen, filter)
	}
	// Focus on a PR stacked behind a pill: its stack fans out (.held, as with focus inside) while focus goes back.
	refocus := between(string(js), "function refocus(at) {", "\n}\n")
	css, _ := staticFS.ReadFile("static/style.css")
	if !strings.Contains(render, "refocus(focus)") || !strings.Contains(refocus, `stack?.classList.add("held")`) ||
		!strings.Contains(string(css), ".stack:is(:hover, :focus-within, .held) > .behind {") {
		t.Fatal("a push drops focus from a PR stacked behind a pill")
	}
}

// Review round 1: stakes the dash cannot read block a default (fail closed): a string, the wrong case, an unknown
// value, a wrong type. Only an absent or empty stakes list lets auto through.
func TestStakesItCannotReadBlockTheDefault(t *testing.T) {
	for _, stakes := range []string{`"security"`, `["Security"]`, `["secrets"]`, `[7]`, `{"security": true}`, `["security", "secrets"]`, `"x"`, `true`} {
		var m map[string]any
		json.Unmarshal([]byte(`{"items": [{"id": "a1", "kind": "decision", "name": "Rotate key", "text": "Rotate it?", "rec": "yes", "auto": true,
			"stakes": `+stakes+`, "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}`), &m)
		a := parseAsks(m)[0]
		if a.Auto {
			t.Errorf("stakes %s: auto", stakes)
		}
		if got := dueDefaults(Snapshot{Asks: []Ask{a}}, fixtureNow+2*defaultAfter); len(got) != 0 {
			t.Errorf("stakes %s: the default applied", stakes)
		}
	}
	for _, stakes := range []string{`[]`, `null`} {
		var m map[string]any
		json.Unmarshal([]byte(`{"items": [{"id": "a1", "kind": "decision", "name": "Re-run e2e", "text": "Re-run?", "rec": "yes", "auto": true,
			"stakes": `+stakes+`, "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}`), &m)
		if a := parseAsks(m)[0]; !a.Auto {
			t.Errorf("stakes %s: no stakes, yet not auto", stakes)
		}
	}
}

// Review round 1: an auto answer athena has done, or the human undid, no longer holds its item in Ready for you: once
// the Undo is handled (applied and auto gone from the item), it is the human's call again, in Needs you.
func TestAHandledAutoAnswerLeavesReadyForYou(t *testing.T) {
	a := decisionWithDefault()
	a.Auto = false // athena handled the Undo: the default is off and nothing is applied
	for _, st := range []string{"done", "cancelled"} {
		s := Snapshot{Asks: []Ask{a}, Actions: []Action{{Seq: 1, TS: fixtureNow - 600, Item: "a1", Option: "re-run", Auto: true, Status: st}}}
		v := Build(s, fixtureNow, "")
		if ids(v.Ready) != "" || ids(v.NeedsYou) != "a1" {
			t.Errorf("%s: ready %q needs %q", st, ids(v.Ready), ids(v.NeedsYou))
		}
	}
	s := Snapshot{Asks: []Ask{a}, Actions: []Action{{Seq: 1, TS: fixtureNow - 600, Item: "a1", Option: "re-run", Auto: true, Status: "queued"}}}
	if v := Build(s, fixtureNow, ""); ids(v.Ready) != "a1" {
		t.Errorf("a queued auto answer shows in Ready for you: %q", ids(v.Ready))
	}
}
