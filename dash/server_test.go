package main

import (
	"bufio"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

// start serves a copy of the fixture on a loopback port, as main does.
func start(t *testing.T, dir string) (*Server, string) {
	t.Helper()
	ln, err := Listen(0)
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	srv := NewServer(dir, port, "acme")
	srv.now = func() time.Time { return time.Unix(fixtureNow, 0) }
	srv.Refresh(true)
	hs := &httptest.Server{Listener: ln, Config: &http.Server{Handler: srv.Handler()}}
	hs.Start()
	t.Cleanup(func() { srv.Stop(); hs.Close() })
	return srv, "http://127.0.0.1:" + strconv.Itoa(port)
}

func get(t *testing.T, url string, host string) (*http.Response, string) {
	t.Helper()
	req, _ := http.NewRequest("GET", url, nil)
	if host != "" {
		req.Host = host
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp, string(body)
}

func TestListenBindsLoopbackOnly(t *testing.T) {
	ln, err := Listen(0)
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	if ip := ln.Addr().(*net.TCPAddr).IP; !ip.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatalf("bound %v", ip)
	}
}

func TestPageRendersTheThreeGroups(t *testing.T) {
	_, base := start(t, copyFixture(t))
	resp, body := get(t, base+"/", "")
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html") {
		t.Fatalf("%d %s", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	for _, want := range []string{
		"<title>(5) athena</title>",
		`<h2 id="h-needs">Needs you <span class="count">5</span></h2>`,
		`<h2 id="h-progress">In progress <span class="count">4</span></h2>`,
		`<h2 id="h-done">Done <span class="count">4</span></h2>`,
		`<a class="ticket" href="https://linear.app/acme/issue/ABC-108">ABC-108</a>`,
		`<a href="https://github.com/acme/web/pull/48">#48</a>`,
		`<span class="checks failure">checks failing</span>`,
		"Start <strong>ABC-111</strong> now",
		`<a href="https://github.com/acme/web/pull/50">https://github.com/acme/web/pull/50</a>`,
		`<li id="w-abc-102" class="item act">`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("page lacks %s", want)
		}
	}
	// Nothing loads from another host: every src and stylesheet is local.
	for _, m := range regexp.MustCompile(`(?:src|href)="([^"]+)"`).FindAllStringSubmatch(body, -1) {
		if strings.HasPrefix(m[1], "http") && !strings.Contains(m[0], `href="https://`) {
			t.Errorf("remote resource %s", m[0])
		}
	}
	if strings.Contains(body, "<link rel=\"stylesheet\" href=\"http") || strings.Contains(body, `src="http`) {
		t.Error("remote resource")
	}
}

func TestSchemeParameterPinsTheScheme(t *testing.T) {
	_, base := start(t, copyFixture(t))
	if _, body := get(t, base+"/?scheme=dark", ""); !strings.Contains(body, `<html lang="en" data-scheme="dark">`) {
		t.Fatal("dark not pinned")
	}
	if _, body := get(t, base+"/?scheme=%22%3E", ""); !strings.Contains(body, `<html lang="en">`) {
		t.Fatal("a bad scheme must be ignored")
	}
}

func TestOnlyLoopbackHostsAreAnswered(t *testing.T) {
	_, base := start(t, copyFixture(t))
	port := base[strings.LastIndex(base, ":")+1:]
	for host, code := range map[string]int{
		"127.0.0.1:" + port: 200, "localhost:" + port: 200,
		"attacker.test:" + port: http.StatusMisdirectedRequest, "127.0.0.1:1": http.StatusMisdirectedRequest,
	} {
		if resp, _ := get(t, base+"/healthz", host); resp.StatusCode != code {
			t.Errorf("host %s: %d, want %d", host, resp.StatusCode, code)
		}
	}
}

func TestFontsAndStaticFilesAreEmbedded(t *testing.T) {
	_, base := start(t, copyFixture(t))
	for _, p := range []string{"InstrumentSans.woff2", "InstrumentSerif-Regular.woff2", "style.css", "app.js", "mark.svg"} {
		resp, body := get(t, base+"/static/"+p, "")
		if resp.StatusCode != 200 || len(body) < 100 {
			t.Errorf("%s: %d, %d bytes", p, resp.StatusCode, len(body))
		}
	}
	_, css := get(t, base+"/static/style.css", "")
	if strings.Contains(css, "http") || strings.Contains(css, "@import") {
		t.Error("style.css must not load from another host")
	}
}

// readEvent reads one SSE event and returns its joined data lines.
func readEvent(t *testing.T, r *bufio.Reader) string {
	t.Helper()
	var data []string
	event := ""
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			t.Fatalf("stream ended: %v", err)
		}
		line = strings.TrimRight(line, "\n")
		switch {
		case line == "" && event != "":
			return strings.Join(data, "\n")
		case strings.HasPrefix(line, "event: "):
			event = line[len("event: "):]
		case strings.HasPrefix(line, "data: "):
			data = append(data, line[len("data: "):])
		}
	}
}

func TestEventsPushAChangedFileToAnOpenPage(t *testing.T) {
	dir := copyFixture(t)
	srv, base := start(t, dir)
	go srv.Poll(20 * time.Millisecond)
	resp, err := http.Get(base + "/events")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("content type %s", ct)
	}
	r := bufio.NewReader(resp.Body)
	first := readEvent(t, r)
	if !strings.Contains(first, `id="w-abc-101"`) || !strings.Contains(first, "In progress") {
		t.Fatalf("first event is not the page: %.200s", first)
	}
	write(t, filepath.Join(dir, "workers", "abc-101.json"),
		`{"name": "abc-101", "state": "blocked", "summary": "Claude needs your permission to use Write", "ts": 1791500000}`)
	got := make(chan string, 1)
	go func() { got <- readEvent(t, r) }()
	select {
	case page := <-got:
		if !strings.Contains(page, "Claude needs your permission to use Write") {
			t.Fatalf("update lacks the change: %.300s", page)
		}
		needs := page[:strings.Index(page, `id="h-progress"`)]
		if !strings.Contains(needs, `id="w-abc-101"`) {
			t.Fatal("abc-101 did not move to Needs you")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no update within 2 s")
	}
}

func TestUnchangedStateIsNotPushedAgain(t *testing.T) {
	dir := copyFixture(t)
	srv, _ := start(t, dir)
	if srv.Refresh(true) {
		t.Fatal("the same page was broadcast twice")
	}
	os.Chtimes(filepath.Join(dir, "ledger.jsonl"), time.Now(), time.Now())
	if srv.Refresh(false) {
		t.Fatal("a touched file with the same content changed the page")
	}
}

func TestTheMinuteTickDoesNotRepushAnUnchangedPage(t *testing.T) {
	srv, _ := start(t, copyFixture(t))
	srv.now = func() time.Time { return time.Unix(fixtureNow+80, 0) } // the minute texts change; watch.json is not stale yet
	if srv.Refresh(true) {
		t.Fatal("only the relative times changed, and the page's own timer updates those")
	}
	srv.now = func() time.Time { return time.Unix(fixtureNow+15*24*3600, 0) } // the Done window moved on
	if !srv.Refresh(true) {
		t.Fatal("a real change in the page was not pushed")
	}
}

func TestOfferKeepsOnlyTheNewestPage(t *testing.T) {
	c := make(chan []byte, 1)
	offer(c, []byte("old"))
	offer(c, []byte("new"))
	if got := string(<-c); got != "new" {
		t.Fatalf("got %s", got)
	}
}

func TestWatchPidStopsWhenTheProcessIsGone(t *testing.T) {
	stop := make(chan string, 1)
	go watchPid(1<<22+12345, 10*time.Millisecond, stop) // above any real pid
	select {
	case reason := <-stop:
		if !strings.Contains(reason, "is gone") {
			t.Fatal(reason)
		}
	case <-time.After(time.Second):
		t.Fatal("did not notice the missing pid")
	}
	alive := make(chan string, 1)
	go watchPid(os.Getpid(), 10*time.Millisecond, alive)
	select {
	case reason := <-alive:
		t.Fatalf("stopped for a live pid: %s", reason)
	case <-time.After(100 * time.Millisecond):
	}
}

func BenchmarkRender(b *testing.B) {
	srv := NewServer("testdata/state", 2843, "acme")
	for i := 0; i < b.N; i++ {
		srv.main = nil
		srv.Refresh(true)
	}
}
