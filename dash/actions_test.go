package main

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

// post sends an answer as the page does; change edits the request first, to break one of its checks.
func post(t *testing.T, srv *Server, base, body string, change func(*http.Request)) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest("POST", base+"/api/action", strings.NewReader(body))
	req.Header.Set("Origin", base)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Athena-Token", srv.token)
	if change != nil {
		change(req)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	data, _ := io.ReadAll(resp.Body)
	json.Unmarshal(data, &out)
	return resp.StatusCode, out
}

func queued(t *testing.T, dir string) []Action {
	t.Helper()
	return NewStore(dir).Load().Actions
}

func TestAnAnswerIsQueuedAndHidesItsAskAtOnce(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	_, page := get(t, base+"/", "")
	if !strings.Contains(page, `id="ask-a5"`) || !strings.Contains(page, `<meta name="athena-token" content="`+srv.token+`">`) {
		t.Fatal("the page carries the ask and its token")
	}
	code, out := post(t, srv, base, `{"item": "a5", "option": "yes"}`, nil)
	if code != http.StatusAccepted || out["seq"] != 1.0 {
		t.Fatalf("%d %v", code, out)
	}
	// Refresh(true) ran before the reply: the page every tab gets next no longer has the ask.
	main, _, _ := srv.snapshot()
	if strings.Contains(string(main), `id="ask-a5"`) {
		t.Fatal("the answered ask is still on the page")
	}
	got := queued(t, dir)
	if len(got) != 1 || got[0] != (Action{1, fixtureNow, "a5", "yes", "", false, "queued"}) {
		t.Fatalf("%+v", got)
	}
	data, _ := os.ReadFile(filepath.Join(dir, actionsFile))
	if !regexp.MustCompile(`^\{"item":"a5","option":"yes","seq":1,"status":"queued","ts":1791500000\}\n$`).Match(data) {
		t.Fatalf("one JSON line per answer: %q", data)
	}
}

func TestEveryKindOfAnswer(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for _, body := range []string{
		`{"item": "a1", "discuss": true}`,
		`{"item": "a2", "text": "  Thursday, and tell #design  "}`,
		`{"item": "a3", "option": "dismiss"}`, // a note with no options gets ×
		`{"item": "w-abc-101", "discuss": true}`,
		`{"item": "w-abc-101", "text": "rebase first"}`,
	} {
		if code, out := post(t, srv, base, body, nil); code != http.StatusAccepted {
			t.Fatalf("%s: %d %v", body, code, out)
		}
	}
	got := queued(t, dir)
	if len(got) != 5 || !got[0].Discuss || got[1].Text != "Thursday, and tell #design" || got[2].Option != "dismiss" || got[4].Item != "w-abc-101" {
		t.Fatalf("%+v", got)
	}
	// A worker's row stays: it is the worker's state, not an ask.
	main, _, _ := srv.snapshot()
	if !strings.Contains(string(main), `id="w-abc-101"`) || strings.Contains(string(main), `id="ask-a1"`) {
		t.Fatal("asks hide, worker rows stay")
	}
}

func TestPostsTheServerRefuses(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	long := `{"item": "a2", "text": "` + strings.Repeat("x", textMax+1) + `"}`
	cases := []struct {
		name, body string
		change     func(*http.Request)
		code       int
	}{
		{"no Origin", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Del("Origin") }, 403},
		{"another Origin", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Set("Origin", "http://evil.test") }, 403},
		{"another port", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Set("Origin", "http://127.0.0.1:1") }, 403},
		{"a form post", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Set("Content-Type", "text/plain") }, 415},
		{"no content type", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Del("Content-Type") }, 415},
		{"no token", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Del("X-Athena-Token") }, 403},
		{"a wrong token", `{"item": "a5", "option": "yes"}`, func(r *http.Request) { r.Header.Set("X-Athena-Token", "0123") }, 403},
		{"a GET", ``, func(r *http.Request) { r.Method = "GET" }, 405},
		{"a preflight", ``, func(r *http.Request) { r.Method = "OPTIONS" }, 405},
		{"a reply past 2000 characters", long, nil, 400},
		{"a body past the cap", `{"item": "a2", "text": "` + strings.Repeat("x", bodyMax) + `"}`, nil, 400},
		{"an empty reply", `{"item": "a2", "text": "   "}`, nil, 400},
		{"two answers", `{"item": "a5", "option": "yes", "discuss": true}`, nil, 400},
		{"no answer", `{"item": "a5"}`, nil, 400},
		{"an unknown field", `{"item": "a5", "option": "yes", "url": "https://x.test"}`, nil, 400},
		{"an unknown item", `{"item": "a99", "option": "yes"}`, nil, 422},
		{"an option it lacks", `{"item": "a5", "option": "maybe"}`, nil, 422},
		{"× on a question", `{"item": "a5", "option": "dismiss"}`, nil, 422},
		{"Reply on a note", `{"item": "a4", "text": "hi"}`, nil, 422},
		{"a retired worker", `{"item": "w-abc-098", "discuss": true}`, nil, 422},
		{"an option on a worker", `{"item": "w-abc-101", "option": "yes"}`, nil, 422},
		{"undo with an item", `{"seq": 1, "undo": true, "item": "a5"}`, nil, 400},
	}
	for _, c := range cases {
		if code, out := post(t, srv, base, c.body, c.change); code != c.code {
			t.Errorf("%s: %d %v, want %d", c.name, code, out, c.code)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, actionsFile)); !os.IsNotExist(err) {
		t.Fatal("nothing refused is written")
	}
	// The page is served with no CORS header, so no other origin may read a reply either.
	resp, _ := get(t, base+"/", "")
	if resp.Header.Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("no CORS")
	}
}

func TestUndoWithinTheSettleWindow(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	post(t, srv, base, `{"item": "a5", "option": "yes"}`, nil)
	srv.now = func() time.Time { return time.Unix(fixtureNow+settleAfter-1, 0) }
	if code, out := post(t, srv, base, `{"seq": 1, "undo": true}`, nil); code != http.StatusAccepted {
		t.Fatalf("%d %v", code, out)
	}
	if got := queued(t, dir); got[0].Status != "cancelled" {
		t.Fatalf("%+v", got)
	}
	main, _, _ := srv.snapshot()
	if !strings.Contains(string(main), `id="ask-a5"`) {
		t.Fatal("an undone answer brings its ask back at once")
	}
	if code, _ := post(t, srv, base, `{"seq": 1, "undo": true}`, nil); code != http.StatusConflict {
		t.Fatal("an answer is undone once")
	}
	// Past the window, athena may already be acting on it: undo is refused.
	post(t, srv, base, `{"item": "a5", "option": "no"}`, nil)
	srv.now = func() time.Time { return time.Unix(fixtureNow+2*settleAfter, 0) }
	if code, _ := post(t, srv, base, `{"seq": 2, "undo": true}`, nil); code != http.StatusConflict {
		t.Fatal("too late to undo")
	}
	if code, _ := post(t, srv, base, `{"seq": 7, "undo": true}`, nil); code != http.StatusConflict {
		t.Fatal("an unknown seq")
	}
}

func TestRapidAnswersAreAllQueuedInOrder(t *testing.T) {
	dir := copyFixture(t)
	s := Snapshot{Hooks: map[string]Hook{}}
	var items []string
	for i := 1; i <= 12; i++ {
		items = append(items, `{"id": "q`+string(rune('a'+i))+`", "text": "Question `+string(rune('a'+i))+`?"}`)
	}
	write(t, filepath.Join(dir, asksFile), `{"items": [`+strings.Join(items, ",")+`]}`)
	_ = s
	srv, base := start(t, dir)
	var wg sync.WaitGroup
	codes := make(chan int, 12)
	for i := 1; i <= 12; i++ {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			code, _ := post(t, srv, base, `{"item": "`+id+`", "discuss": true}`, nil)
			codes <- code
		}("q" + string(rune('a'+i)))
	}
	wg.Wait()
	close(codes)
	for c := range codes {
		if c != http.StatusAccepted {
			t.Fatalf("a rapid answer was refused: %d", c)
		}
	}
	got := queued(t, dir)
	seen := map[string]bool{}
	for i, a := range got {
		if a.Seq != i+1 || a.Status != "queued" || seen[a.Item] {
			t.Fatalf("seq %d: %+v", i+1, got)
		}
		seen[a.Item] = true
	}
	if len(got) != 12 {
		t.Fatalf("%d of 12 queued", len(got))
	}
	main, _, _ := srv.snapshot()
	if strings.Contains(string(main), `id="ask-q`) {
		t.Fatal("every answered ask is hidden")
	}
}

func TestACompactedQueueKeepsCountingFromItsLastSeq(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	// `athena ask done` compacted everything away but the last line.
	write(t, filepath.Join(dir, actionsFile), `{"seq": 41, "ts": 1, "item": "a9", "option": "yes", "status": "queued"}`+"\n"+`{"seq": 41, "ts": 2, "status": "done"}`+"\n")
	if _, out := post(t, srv, base, `{"item": "a5", "option": "yes"}`, nil); out["seq"] != 42.0 {
		t.Fatalf("%v", out)
	}
}

func TestAStuckAnswerBringsItsAskBack(t *testing.T) {
	s := Snapshot{Hooks: map[string]Hook{}, Asks: []Ask{{ID: "a1", Kind: "question", Text: "Ship?", Why: "the window closes at 5"}},
		Actions: []Action{{Seq: 1, TS: fixtureNow - stuckAfter + 1, Item: "a1", Discuss: true, Status: "queued"}}}
	if v := Build(s, fixtureNow, ""); len(v.NeedsYou) != 0 || v.actionable["a1"] != nil {
		t.Fatalf("hidden while athena has it queued: %+v", v.NeedsYou)
	}
	s.Actions[0].TS = fixtureNow - stuckAfter
	v := Build(s, fixtureNow, "")
	if len(v.NeedsYou) != 1 || v.NeedsYou[0].Detail != stuckWhy || v.actionable["a1"] == nil {
		t.Fatalf("back after %d s, saying why: %+v", stuckAfter, v.NeedsYou)
	}
	for _, st := range []string{"done", "cancelled"} {
		s.Actions[0].Status = st
		if v := Build(s, fixtureNow, ""); len(v.NeedsYou) != 1 || v.NeedsYou[0].Detail != "the window closes at 5" {
			t.Fatalf("%s: %+v", st, v.NeedsYou)
		}
	}
}

func TestWhatEachRowOffers(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	a1 := v.actionable["a1"]
	if a1 == nil || len(a1.Options) != 4 || !a1.Discuss || !a1.Reply || a1.Dismiss || !a1.Hides || a1.RowOptions() != nil {
		t.Fatalf("a question with a body: its four options (in its panel only), Discuss and Reply: %+v", a1)
	}
	if a5 := v.actionable["a5"]; a5 == nil || len(a5.RowOptions()) != 2 {
		t.Fatalf("two options show on the row: %+v", a5)
	}
	if n := v.actionable["a3"]; n == nil || len(n.Options) != 0 || n.Discuss || n.Reply || !n.Dismiss {
		t.Fatalf("a note with no options: × only: %+v", n)
	}
	if w := v.actionable["w-abc-102"]; w == nil || !w.Discuss || !w.Reply || w.Dismiss || w.Hides || len(w.Options) != 0 {
		t.Fatalf("a live worker: Discuss and Reply, no dismiss: %+v", w)
	}
	if v.actionable["w-abc-098"] != nil || v.actionable["pr-acme-web-906"] != nil || v.actionable["rr-acme-web-6170"] != nil {
		t.Fatal("no actions on a retired worker's Done row, an In progress PR row or a review request")
	}
	// The ask's why shows in the row's detail line, always visible.
	for _, it := range v.NeedsYou {
		if it.ID == "ask-a1" && it.Detail != "ABC-111 touches the same export queue as ABC-101." {
			t.Fatalf("%+v", it)
		}
	}
}

func TestPillsTakeThePlaceOfTheLabelLine(t *testing.T) {
	_, base := start(t, copyFixture(t))
	_, page := get(t, base+"/", "")
	// An ask: the pills share a cell with its label line, so no space is reserved for them.
	if !regexp.MustCompile(`<div class="swap has-acts"><p class="meta">athena asks</p><div class="acts options" data-item="a5" data-hides>`).MatchString(page) {
		t.Error("an ask's pills are not in its label line's cell")
	}
	// A worker's row: they take the place of its whole state line.
	if !regexp.MustCompile(`(?s)<li id="w-abc-101" .*?<div class="swap has-acts"><p class="meta">.*?</p><div class="acts" data-item="w-abc-101">`).MatchString(page) {
		t.Error("a worker's pills are not in its state line's cell")
	}
	// A folded ask gets a label line of its own to swap with.
	if !strings.Contains(page, `<div class="fold" id="ask-a3"><p>Approve and merge ABC-108</p><div class="swap has-acts"><p class="fold-label">note</p><div class="acts" data-item="a3" data-hides>`) {
		t.Error("a folded ask's pills have no line to swap with")
	}
	// A row with no pills has no pill-height line.
	if !regexp.MustCompile(`(?s)<li id="w-abc-098" .*?<div class="swap"><p class="meta">`).MatchString(page) {
		t.Error("a retired row's line is not left as it is")
	}
	// Icon-only pills are named by aria-label (the tooltip's text), never a native title.
	for _, want := range []string{`data-discuss aria-label="Discuss in Athena"`, `data-reply aria-label="Reply…"`, `data-option="dismiss" aria-label="Dismiss"`} {
		if !strings.Contains(page, want) {
			t.Errorf("missing %s", want)
		}
	}
	if regexp.MustCompile(`class="pill[^"]*"[^>]* title=`).MatchString(page) {
		t.Error("a pill has a native title as well as its tooltip")
	}
}

func TestAReplyIsCountedInCharactersAsTheReplyBoxCountsThem(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	// 2000 two-byte characters: what the reply box's maxlength lets through, 4000 bytes.
	if code, out := post(t, srv, base, `{"item": "a2", "text": "`+strings.Repeat("é", textMax)+`"}`, nil); code != http.StatusAccepted {
		t.Fatalf("%d %v", code, out)
	}
	if got := queued(t, dir); len(got) != 1 || len([]rune(got[0].Text)) != textMax {
		t.Fatalf("%+v", got)
	}
}

func TestTwoTabsAnsweringOneAskAtOnceQueueOneAnswer(t *testing.T) {
	for round := 0; round < 5; round++ {
		dir := copyFixture(t)
		srv, base := start(t, dir)
		var wg sync.WaitGroup
		codes := make(chan int, 2)
		for _, opt := range []string{"yes", "no"} {
			wg.Add(1)
			go func(opt string) {
				defer wg.Done()
				code, _ := post(t, srv, base, `{"item": "a5", "option": "`+opt+`"}`, nil)
				codes <- code
			}(opt)
		}
		wg.Wait()
		close(codes)
		got := map[int]int{}
		for c := range codes {
			got[c]++
		}
		// Whichever lost the race got 409, or 422 if the page had already dropped the ask.
		if got[http.StatusAccepted] != 1 || got[http.StatusConflict]+got[http.StatusUnprocessableEntity] != 1 {
			t.Fatalf("round %d: %v", round, got)
		}
		if q := queued(t, dir); len(q) != 1 {
			t.Fatalf("round %d: %+v", round, q)
		}
	}
}

func TestTheLockRefusesASecondAnswerButNotAWorkersOrAStuckOne(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	post(t, srv, base, `{"item": "a5", "option": "yes"}`, nil)
	// Another tab still shows the ask (its page check passes): the lock refuses it.
	write(t, filepath.Join(dir, actionsFile), `{"seq": 1, "ts": 1791500000, "item": "a5", "option": "yes", "status": "queued"}`+"\n")
	srv.view.actionable["a5"] = &Actions{Item: "a5", Options: []Option{{ID: "no", Label: "No"}}, Hides: true}
	if code, out := post(t, srv, base, `{"item": "a5", "option": "no"}`, nil); code != http.StatusConflict {
		t.Fatalf("%d %v", code, out)
	}
	// Past stuckAfter the ask shows again, and takes a new answer.
	srv.now = func() time.Time { return time.Unix(fixtureNow+stuckAfter, 0) }
	srv.Refresh(true)
	if code, out := post(t, srv, base, `{"item": "a5", "option": "no"}`, nil); code != http.StatusAccepted {
		t.Fatalf("stuck: %d %v", code, out)
	}
	for _, body := range []string{`{"item": "w-abc-101", "discuss": true}`, `{"item": "w-abc-101", "text": "and rebase"}`} {
		if code, out := post(t, srv, base, body, nil); code != http.StatusAccepted {
			t.Fatalf("a worker's row takes any number: %d %v", code, out)
		}
	}
}

func TestEventsTellThePageWhichStartServesIt(t *testing.T) {
	srv, base := start(t, copyFixture(t))
	_, page := get(t, base+"/", "")
	if !strings.Contains(page, `<meta name="athena-boot" content="`+srv.boot+`">`) {
		t.Fatal("the page lacks its start id")
	}
	resp, err := http.Get(base + "/events")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := make([]byte, 256)
	n, _ := io.ReadAtLeast(resp.Body, buf, 40)
	if !strings.Contains(string(buf[:n]), "event: boot\ndata: "+srv.boot+"\n\n") {
		t.Fatalf("%q", buf[:n])
	}
}
