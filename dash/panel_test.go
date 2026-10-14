package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestAnAskHasAPanelOnlyWhenItHasMoreToShow(t *testing.T) {
	v := Build(NewStore("testdata/state").Load(), fixtureNow, "acme")
	var a1, a2, a5 Item
	for _, it := range v.NeedsYou {
		switch it.ID {
		case "ask-a1":
			a1 = it
		case "ask-a2":
			a2 = it
		case "ask-a5":
			a5 = it
		}
	}
	// A body: its title is the row, its body, previews and answers the panel; the answers are the row's own.
	if p := a1.Panel; p == nil || p.Ask != "a1" || p.Actions != a1.Actions || !strings.Contains(string(p.Body), "<table>") || p.Text != "" ||
		len(p.Previews) != 1 || p.Previews[0] != (Preview{"export-screen.html", "/preview/a1/export-screen.html"}) || p.Why != a1.Detail {
		t.Fatalf("a1's panel %+v", a1.Panel)
	}
	if string(a1.Note) != "Start <strong>ABC-111</strong> now, or after ABC-101 merges? (rec: after)" {
		t.Fatalf("a1's title %q", a1.Note)
	}
	// An ask from before titles were one sentence: its first sentence is the row, its whole text opens the panel.
	if string(a2.Note) != "The release notes for 2.4 need a date: Thursday or Monday?" || a2.Panel == nil ||
		!strings.Contains(string(a2.Panel.Text), `Draft: <a href="https://github.com/acme/web/pull/50">acme/web#50</a>`) || a2.Panel.Body != "" {
		t.Fatalf("a2 %q %+v", a2.Note, a2.Panel)
	}
	if a5.Panel != nil {
		t.Fatalf("a title and a why alone have no panel: %+v", a5.Panel)
	}
	// A folded ask gets one too, under its fold.
	if f := find(v.NeedsYou, "abc-108").Folds; len(f) != 1 || f[0].Panel == nil || !strings.Contains(string(f[0].Panel.Body), "<li>Merge style: squash") {
		t.Fatalf("abc-108's fold %+v", f)
	}
}

func TestThePanelRendersUnderItsRowAndFold(t *testing.T) {
	_, base := start(t, copyFixture(t))
	_, page := get(t, base+"/", "")
	// The row is as before, with its why line as the summary of a closed <details>.
	row := between(page, `<li id="ask-a1" class="item note">`, `</details>`) // the body has <li>s of its own
	for _, want := range []string{
		`<div class="acts" data-item="a1" data-hides><button type="button" class="pill" data-discuss`, // past two options: Discuss and Reply alone
		`<details class="ask-detail" data-ask="a1"><summary><span class="detail">ABC-111 touches the same export queue as ABC-101.</span><span class="more">Details<svg class="pi chev" aria-hidden="true"><use href="#i-chevron-down"/></svg></span></summary>`,
		"<th>Option</th>", "<td>Split it</td>", "<strong>Recommendation: after ABC-101 merges.</strong>",
		`<a class="pill" href="/preview/a1/export-screen.html" target="_blank" rel="noopener noreferrer" data-tip=`,
		`<div class="acts bar" data-item="a1" data-hides><button type="button" class="pill" data-option="now">`,
		`data-option="split"`, `data-option="hold"`, "Discuss in Athena</button>", "Reply…</button>",
	} {
		if !strings.Contains(row, want) {
			t.Errorf("a1 lacks %s", want)
		}
	}
	if strings.Contains(row, "<details open") || strings.Contains(row, `<p class="detail" title=`) {
		t.Error("a panel starts closed, its why line its summary")
	}
	if n := strings.Count(row, `data-option="split"`); n != 1 {
		t.Errorf("the third option is the panel's alone, shown %d times", n)
	}
	// A summary holds text only: a control in it would toggle the panel.
	for _, s := range regexp.MustCompile(`(?s)<summary>.*?</summary>`).FindAllString(page, -1) {
		if strings.Contains(s, "<button") || strings.Contains(s, "<a ") {
			t.Errorf("a control in a summary: %s", s)
		}
	}
	fold := between(page, `<div class="fold" id="ask-a3">`, `</details>`)
	if !strings.Contains(fold, `<details class="ask-detail" data-ask="a3">`) || !strings.Contains(fold, `<div class="acts bar" data-item="a3" data-hides><button type="button" class="pill" data-option="dismiss">`) {
		t.Errorf("a3's fold has no panel: %s", fold)
	}
	if strings.Contains(between(page, `<li id="ask-a5" class="item note">`, `</li>`), "<details") {
		t.Error("an ask with nothing more has no panel")
	}
}

// The panel's buttons answer as the row's do: the same item, checked against the same actions.
func TestAnAnswerFromThePanelIsCheckedAsTheRowsIs(t *testing.T) {
	for _, c := range []struct {
		body string
		want int
	}{
		{`{"item": "a1", "option": "split"}`, http.StatusAccepted}, // the third option, on the panel only
		{`{"item": "a1", "option": "hold"}`, http.StatusAccepted},
		{`{"item": "a1", "option": "fifth"}`, http.StatusUnprocessableEntity},
		{`{"item": "a3", "option": "dismiss"}`, http.StatusAccepted},
		{`{"item": "a3", "text": "merge it"}`, http.StatusUnprocessableEntity}, // a note: no Reply, on the row or the panel
	} {
		dir := copyFixture(t)
		srv, base := start(t, dir)
		if code, out := post(t, srv, base, c.body, nil); code != c.want {
			t.Errorf("%s: %d %v, want %d", c.body, code, out, c.want)
		}
	}
	// A fifth option in the file is dropped, so it is refused like any the page does not offer.
	dir := copyFixture(t)
	write(t, filepath.Join(dir, asksFile), `{"items": [{"id": "a1", "text": "Which?", "body": "Rec A.", "options": [
		{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}, {"id": "e", "label": "E"}]}]}`)
	srv, base := start(t, dir)
	if code, _ := post(t, srv, base, `{"item": "a1", "option": "e"}`, nil); code != http.StatusUnprocessableEntity {
		t.Errorf("a fifth option: %d", code)
	}
	if code, _ := post(t, srv, base, `{"item": "a1", "option": "d"}`, nil); code != http.StatusAccepted {
		t.Errorf("a fourth option: %d", code)
	}
}

func TestAPreviewIsServedSandboxed(t *testing.T) {
	srv, base := start(t, copyFixture(t))
	resp, body := get(t, base+"/preview/a1/export-screen.html", "")
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html") || !strings.Contains(body, "Export billing data") {
		t.Fatalf("%d %s", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	// An opaque origin: its script can read nothing of the dash, so not the token, and its POSTs carry Origin null.
	if csp := resp.Header.Get("Content-Security-Policy"); csp != "sandbox allow-scripts" || strings.Contains(csp, "allow-same-origin") {
		t.Fatalf("CSP %q", csp)
	}
	if resp.Header.Get("Cache-Control") != "no-store" || resp.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("headers %v", resp.Header)
	}
	if strings.Contains(body, srv.token) {
		t.Fatal("a preview carries the token")
	}
	// The dash's own page keeps its CSP: the preview's does not leak onto it.
	if resp, _ := get(t, base+"/", ""); !strings.HasPrefix(resp.Header.Get("Content-Security-Policy"), "default-src 'self'") {
		t.Fatalf("the page's CSP %q", resp.Header.Get("Content-Security-Policy"))
	}
}

// What a sandboxed preview's script could send, even knowing the token: its Origin is "null", and the server refuses it.
func TestAPreviewCannotAnswerAnAsk(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	for _, origin := range []string{"null", ""} {
		code, out := post(t, srv, base, `{"item": "a1", "option": "now"}`, func(r *http.Request) {
			if origin == "" {
				r.Header.Del("Origin")
			} else {
				r.Header.Set("Origin", origin)
			}
		})
		if code != http.StatusForbidden {
			t.Errorf("Origin %q: %d %v", origin, code, out)
		}
	}
	if len(queued(t, dir)) != 0 {
		t.Fatal("an answer was queued")
	}
}

func TestAPreviewPathCannotLeaveItsFolder(t *testing.T) {
	dir := copyFixture(t)
	root := filepath.Join(dir, previewsDir)
	write(t, filepath.Join(dir, "secret.txt"), "SECRET")
	os.MkdirAll(filepath.Join(root, "a1", "site", "css"), 0o700)
	write(t, filepath.Join(root, "a1", "site", "index.html"), "<p>site</p>")
	write(t, filepath.Join(root, "a1", "site", "css", "a.css"), "p{}")
	write(t, filepath.Join(root, "a1", ".hidden"), "SECRET")
	must(t, os.Symlink(filepath.Join(dir, "secret.txt"), filepath.Join(root, "a1", "leak.html")))
	must(t, os.Symlink(dir, filepath.Join(root, "a1", "state")))
	must(t, os.Symlink(dir, filepath.Join(root, "a9")))
	_, base := start(t, dir)
	for path, want := range map[string]int{
		"/preview/a1/site/":                       200,
		"/preview/a1/site/css/a.css":              200,
		"/preview/a1/site":                        301, // to site/, so its relative links resolve inside it
		"/preview/a1/leak.html":                   404, // a symlink to a file outside
		"/preview/a1/state/secret.txt":            404, // through a symlinked folder
		"/preview/a9/secret.txt":                  404, // an ask's folder that is a symlink
		"/preview/a1/.hidden":                     404,
		"/preview/a1/%2e%2e/%2e%2e/secret.txt":    404,
		"/preview/a1/..%2f..%2fsecret.txt":        404,
		"/preview/a1/site%5c..%5c..%5csecret.txt": 404,
		"/preview/A1/export-screen.html":          404, // not an ask id
		"/preview/a1/export-screen.html/":         404,
		"/preview/a1/missing.html":                404,
		"/preview/a2/":                            404, // no previews
	} {
		req, _ := http.NewRequest("GET", base+path, nil)
		resp, err := (&http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		data := make([]byte, 4096)
		n, _ := resp.Body.Read(data)
		resp.Body.Close()
		if resp.StatusCode != want && !(want == 404 && resp.StatusCode >= 300 && resp.StatusCode < 500 && resp.StatusCode != 200) {
			t.Errorf("%s: %d, want %d", path, resp.StatusCode, want)
		}
		if strings.Contains(string(data[:n]), "SECRET") || strings.Contains(string(data[:n]), `"next"`) {
			t.Errorf("%s served what is outside its folder", path)
		}
		if resp.Header.Get("Content-Security-Policy") != previewCSP && resp.StatusCode != 301 && strings.HasPrefix(path, "/preview/") && resp.StatusCode == 200 {
			t.Errorf("%s: CSP %q", path, resp.Header.Get("Content-Security-Policy"))
		}
	}
	// A POST is refused too: the route only reads.
	resp, err := http.Post(base+"/preview/a1/export-screen.html", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("POST: %d", resp.StatusCode)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// Tooltips are one element of the body, placed by app.js, so no clipped .swap, .acts or .panel can cut one off: none is
// a pill's own ::after any more, and the page has the one.
func TestTooltipsAreTheBodysOwnNeverAPillsPseudoElement(t *testing.T) {
	css, err := staticFS.ReadFile("static/style.css")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(css), "attr(aria-label)") || !strings.Contains(string(css), ".tip {") {
		t.Fatal("a tooltip is drawn inside its pill")
	}
	js, _ := staticFS.ReadFile("static/app.js")
	if !strings.Contains(string(js), "function showTip") {
		t.Fatal("app.js does not place the tooltip")
	}
	_, base := start(t, copyFixture(t))
	_, page := get(t, base+"/", "")
	if !strings.Contains(page, `<div class="tip" aria-hidden="true" hidden></div>`) || strings.Index(page, `class="tip"`) < strings.Index(page, "</main>") {
		t.Fatal("the page has no tooltip outside <main>")
	}
	if !strings.Contains(page, `rel="noopener noreferrer" data-tip="Opens in a tab of its own, sandboxed`) {
		t.Fatal("a preview link says nothing of its tab")
	}
}

// A note whose 3 or 4 options are its panel's alone has no pill on its row, so hovering it never swaps in an empty line.
func TestANoteWithItsOptionsInThePanelKeepsItsLine(t *testing.T) {
	dir := copyFixture(t)
	write(t, filepath.Join(dir, asksFile), `{"items": [{"id": "a1", "kind": "note", "text": "Export plan", "body": "Rec A.",
		"options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}]}]}`)
	_, base := start(t, dir)
	_, page := get(t, base+"/", "")
	row := between(page, `<li id="ask-a1" class="item note">`, `</details>`)
	if !strings.Contains(row, `<div class="swap"><p class="meta">note</p>`) || strings.Contains(row, `<div class="acts" `) || strings.Contains(row, `<div class="acts options" `) {
		t.Fatalf("an empty row of pills: %s", row)
	}
	if !strings.Contains(row, `<div class="acts bar" data-item="a1" data-hides><button type="button" class="pill" data-option="a">`) {
		t.Fatalf("the panel lacks the options: %s", row)
	}
}

// A body keeps its first line's indent, as `athena ask add` does: an indented code block stays one.
func TestABodyKeepsItsFirstIndent(t *testing.T) {
	var m map[string]any
	json.Unmarshal([]byte(`{"items": [{"id": "a1", "text": "Rebase?", "body": "\n\n    git rebase main\n\nThen push.\n\n"}]}`), &m)
	a := parseAsks(m)[0]
	if a.Body != "    git rebase main\n\nThen push." || !strings.Contains(string(renderMarkdown(a.Body)), "<pre><code>git rebase main") {
		t.Fatalf("%q", a.Body)
	}
}

// An older ask's title is never cut inside a link: the row would link a cut URL.
func TestALongTitleIsNeverCutInsideALink(t *testing.T) {
	text := strings.Repeat("word ", 20) + "see [the PR](https://github.com/acme/web/pull/1234) and decide now"
	title, long := (Ask{Text: text}).Title()
	if !long || title != strings.TrimSpace(strings.Repeat("word ", 20))+" see…" {
		t.Fatalf("%q", title)
	}
}

// The page runs scripts from /static/ only: a preview's own .js is never one of the page's.
func TestThePageRunsScriptsFromStaticOnly(t *testing.T) {
	_, base := start(t, copyFixture(t))
	resp, _ := get(t, base+"/", "")
	if csp := resp.Header.Get("Content-Security-Policy"); !strings.Contains(csp, "script-src "+base+"/static/;") {
		t.Fatalf("CSP %q", csp)
	}
}

// A body's table lays out by whole words: a cell's narrowest is its longest word, never one character a line, headers
// wrap like any cell, and one too wide for the panel scrolls sideways inside it.
func TestABodysTableBreaksOnlyBetweenWords(t *testing.T) {
	css, err := staticFS.ReadFile("static/style.css")
	if err != nil {
		t.Fatal(err)
	}
	rule := func(sel string) string {
		m := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(sel) + ` \{([^}]*)\}`).FindStringSubmatch(string(css))
		if m == nil {
			t.Fatalf("no %s rule", sel)
		}
		return m[1]
	}
	if !strings.Contains(rule(".md th, .md td"), "overflow-wrap: normal") {
		t.Error(".md's overflow-wrap: anywhere reaches table cells and squeezes a column to a character a line")
	}
	if strings.Contains(rule(".md th"), "nowrap") {
		t.Error("a header that cannot wrap takes the width its column's words need")
	}
	if t2 := rule(".md table"); !strings.Contains(t2, "display: block") || !strings.Contains(t2, "overflow-x: auto") {
		t.Error("a table too wide for the panel no longer scrolls inside it")
	}
}
