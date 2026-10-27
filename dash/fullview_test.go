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

// An item's full view (plan §3): its own link, your words, where it stands, the decision brief with every option and
// the rec marked, the previews embedded, its workers and links, the timeline, and the real actions.
func TestTheFullViewShowsTheWholeItem(t *testing.T) {
	_, base := start(t, copyFixture(t))
	resp, page := get(t, base+"/item/a1", "")
	if resp.StatusCode != 200 {
		t.Fatalf("%d", resp.StatusCode)
	}
	for _, want := range []string{
		"<title>Export queue start · athena</title>",
		`<main class="full" data-item="a1">`,
		"<h1>Export queue start</h1>",
		`<p class="facts"><span>your call</span><span class="state">open</span>`,
		`<h2>You asked</h2><blockquote class="words">Can we start the export rework now, or wait until the sign-in fix lands?</blockquote>`,
		`<h2>Where it stands</h2><p class="call">Start <strong>ABC-111</strong> now, or after ABC-101 merges? (rec: after)</p><p>ABC-111 touches the same export queue as ABC-101.</p>`,
		`<h2>What's needed from you</h2>`, "<th>Option</th>", "<td>Split it</td>",
		`<p class="auto">athena takes the rec at `,
		`<div class="acts bar" data-item="a1" data-hides><button type="button" class="pill" data-option="now">`,
		`data-option="later">`, `After ABC-101 <span class="tag">rec</span></button>`, `data-option="split"`, `data-option="hold"`,
		"Discuss in Athena</button>", "Reply…</button>", `<label class="rule"><input type="checkbox" name="rule"> Don't ask me this again</label>`,
		`<iframe src="/preview/a1/export-screen.html" sandbox="allow-scripts" loading="lazy" title="export-screen.html"></iframe>`,
		`<ol class="timeline"><li><time data-ts=`, "filed</li>",
		`<div class="item-acts" data-item="a1"><button type="button" class="pill" data-do="done">`, `data-do="snooze" data-for="7200"`,
	} {
		if !strings.Contains(page, want) {
			t.Errorf("a1's full view lacks %s", want)
		}
	}
	if strings.Contains(page, `data-do="reopen"`) {
		t.Error("an open item does not reopen")
	}
	// A refresh of its own, for each push: the <main> alone.
	if _, frag := get(t, base+"/item/a1?fragment=1", ""); strings.Contains(frag, "<html") || !strings.Contains(frag, "<h1>Export queue start</h1>") {
		t.Fatalf("fragment %.200s", frag)
	}
	// A result's report; an item on a worker shows the worker's row; one resolved here can be reopened, and offers no
	// answers any more.
	_, a3 := get(t, base+"/item/a3", "")
	if !strings.Contains(a3, "<h2>Report</h2>") || strings.Contains(a3, `class="acts bar"`) {
		t.Errorf("a3 %s", a3)
	}
	_, a4 := get(t, base+"/item/a4", "")
	if !strings.Contains(a4, `<span class="name">Billing page empty state</span> <span class="label">done, athena reviewing</span>`) ||
		!strings.Contains(a4, `<a href="https://github.com/acme/web/pull/906">acme/web#906</a>`) {
		t.Errorf("a4's workers and PR %s", between(a4, "<h2>Workers", "</section>"))
	}
	_, a10 := get(t, base+"/item/a10", "")
	if !strings.Contains(a10, `<span class="state">resolved 1 h ago: you marked it done</span>`) || !strings.Contains(a10, `data-do="reopen"`) ||
		strings.Contains(a10, `data-do="done"`) || strings.Contains(a10, `class="acts bar"`) {
		t.Errorf("a10 %s", between(a10, "<article", "</article>"))
	}
	_, a6 := get(t, base+"/item/a6", "")
	if !strings.Contains(a6, `<span class="stakes">security: always your call</span>`) || strings.Contains(a6, `class="auto"`) {
		t.Errorf("a6 %s", between(a6, "<article", "</article>"))
	}
	for _, path := range []string{"/item/a99", "/item/A1", "/item/../needs-you.json", "/item/"} {
		if resp, _ := get(t, base+path, ""); resp.StatusCode != 404 {
			t.Errorf("%s: %d", path, resp.StatusCode)
		}
	}
}

// An item archived after a week still has its full view, and a search finds it.
func TestAnArchivedItemIsFoundAndShown(t *testing.T) {
	dir := copyFixture(t)
	write(t, filepath.Join(dir, archiveFile), `{"id": "a0", "kind": "result", "name": "Old disk cleanup", "text": "Freed 210 GB", "resolved": 1791000000, "resolved_note": "you marked it done", "created": 1790990000, "updated": 1791000000}
not json
`)
	_, base := start(t, dir)
	_, page := get(t, base+"/item/a0", "")
	if !strings.Contains(page, "<h1>Old disk cleanup</h1>") || strings.Contains(page, `data-do="reopen"`) {
		t.Fatalf("%s", between(page, "<article", "</article>"))
	}
	_, found := get(t, base+"/items?q=disk+cleanup", "")
	if !strings.Contains(found, `<a href="/item/a0">Old disk cleanup</a>`) || !strings.Contains(found, "<span>resolved</span>") {
		t.Fatalf("search %s", between(found, "<main", "</main>"))
	}
	_, found = get(t, base+"/items?q=export", "")
	if !strings.Contains(found, `href="/item/a1">Export queue start</a>`) || !strings.Contains(found, `href="/item/a4">Billing export</a>`) || strings.Contains(found, "Old disk") {
		t.Fatalf("search %s", between(found, "<main", "</main>"))
	}
	if _, none := get(t, base+"/items?q=%3Cscript%3E", ""); !strings.Contains(none, "No item matches.") || strings.Contains(none, "<script>") {
		t.Fatal("a query is escaped")
	}
}

// The full view's buttons answer as the row's do: the same item, checked against the same actions.
func TestAnAnswerFromTheFullViewIsCheckedAsTheRowsIs(t *testing.T) {
	for _, c := range []struct {
		body string
		want int
	}{
		{`{"item": "a1", "option": "split"}`, http.StatusAccepted}, // the third option, in the full view only
		{`{"item": "a1", "option": "hold", "rule": true}`, http.StatusAccepted},
		{`{"item": "a1", "text": "after, and always", "rule": true}`, http.StatusAccepted},
		{`{"item": "a1", "discuss": true, "rule": true}`, http.StatusUnprocessableEntity}, // a rule goes with an answer
		{`{"item": "a1", "option": "fifth"}`, http.StatusUnprocessableEntity},
		{`{"item": "a3", "text": "merge it"}`, http.StatusUnprocessableEntity}, // a result: nothing to answer
		{`{"item": "w-abc-101", "text": "rebase", "rule": true}`, http.StatusUnprocessableEntity},
		{`{"item": "a8", "option": "undo-default"}`, http.StatusAccepted},
		{`{"item": "a5", "option": "undo-default"}`, http.StatusUnprocessableEntity}, // no default applied there
		{`{"item": "a6", "option": "on-it"}`, http.StatusAccepted},
	} {
		dir := copyFixture(t)
		srv, base := start(t, dir)
		code, out := post(t, srv, base, c.body, nil)
		if code != c.want {
			t.Errorf("%s: %d %v, want %d", c.body, code, out, c.want)
		}
		if code == http.StatusAccepted && strings.Contains(c.body, `"rule"`) {
			if got := queued(t, dir); len(got) != 1 || !got[0].Rule {
				t.Errorf("%s: the rule did not ride on the answer: %+v", c.body, got)
			}
		}
	}
	// A fifth option in the file is dropped, so it is refused like any the page does not offer.
	dir := copyFixture(t)
	write(t, filepath.Join(dir, asksFile), `{"items": [{"id": "a1", "name": "Pick one", "text": "Which?", "body": "Rec A.", "options": [
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
	for _, path := range []string{"/", "/item/a1"} {
		_, page := get(t, base+path, "")
		if !strings.Contains(page, `<div class="tip" aria-hidden="true" hidden></div>`) || strings.Index(page, `class="tip"`) < strings.Index(page, "</main>") {
			t.Fatalf("%s has no tooltip outside <main>", path)
		}
	}
	_, page := get(t, base+"/item/a1", "")
	if !strings.Contains(page, `rel="noopener noreferrer" data-tip="Opens in a tab of its own, sandboxed`) {
		t.Fatal("a preview link says nothing of its tab")
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

// An older item's title is never cut inside a link: the row would link a cut URL.
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
	for _, path := range []string{"/", "/item/a1", "/items?q=x"} {
		resp, _ := get(t, base+path, "")
		if csp := resp.Header.Get("Content-Security-Policy"); !strings.Contains(csp, "script-src "+base+"/static/;") {
			t.Fatalf("%s: CSP %q", path, csp)
		}
		if resp.Header.Get("X-Frame-Options") != "" {
			t.Fatalf("%s: a frame option would block the full view's own previews", path)
		}
	}
}

// A body's table lays out by whole words: a cell's narrowest is its longest word, never one character a line, headers
// wrap like any cell, and one too wide for the panel scrolls sideways inside it. Every .md rule that reaches a table is
// read, so a later one cannot bring the squeeze back.
func TestABodysTableBreaksOnlyBetweenWords(t *testing.T) {
	css, err := staticFS.ReadFile("static/style.css")
	if err != nil {
		t.Fatal(err)
	}
	cells := regexp.MustCompile(`\b(th|td|tr|thead|tbody|table)\b`)
	var normal, block bool
	for _, m := range regexp.MustCompile(`([^{}]+)\{([^{}]*)\}`).FindAllStringSubmatch(string(css), -1) {
		sel, body := m[1], m[2]
		if i := strings.LastIndex(sel, "*/"); i >= 0 {
			sel = sel[i+2:] // a comment before the rule
		}
		sel = strings.TrimSpace(sel)
		if !strings.Contains(sel, ".md") || !cells.MatchString(sel) {
			continue
		}
		if strings.Contains(body, "nowrap") || strings.Contains(body, "overflow-wrap: anywhere") || strings.Contains(body, "word-break") {
			t.Errorf("%s lets a table cell break inside a word or not at all: %s", sel, body)
		}
		normal = normal || sel == ".md th, .md td" && strings.Contains(body, "overflow-wrap: normal")
		block = block || sel == ".md table" && strings.Contains(body, "display: block") && strings.Contains(body, "overflow-x: auto")
	}
	if !normal {
		t.Error(".md's overflow-wrap: anywhere reaches table cells and squeezes a column to a character a line")
	}
	if !block {
		t.Error("a table too wide for the panel no longer scrolls inside it")
	}
}
