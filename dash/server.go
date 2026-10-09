package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"fmt"
	"html/template"
	"io/fs"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"sync"
	"time"
)

//go:embed page.html
var pageHTML string

//go:embed static
var staticFS embed.FS

var funcs = template.FuncMap{
	"iso": func(ts float64) string { return unix(ts).UTC().Format(time.RFC3339) },
	"checks": func(c string) string {
		return map[string]string{"success": "green", "failure": "failing", "pending": "running"}[c]
	},
	// The icons drawn as solid ink badges: events someone acts on, as in watch.svg.
	"badge": func(icon string) bool {
		return icon == "blocked" || icon == "fail" || icon == "done" || icon == "ready"
	},
	"group": func(key, name string, items []Item, empty string) map[string]any {
		return map[string]any{"Key": key, "Name": name, "Items": items, "Empty": empty}
	},
}

func unix(ts float64) time.Time {
	sec, frac := math.Modf(ts)
	return time.Unix(int64(sec), int64(frac*1e9))
}

// ago matches the page's own timer in app.js, which keeps these fresh between renders.
func ago(d time.Duration) string {
	s := int(d.Seconds())
	switch {
	case s < 60:
		return "just now"
	case s < 3600:
		return strconv.Itoa(s/60) + " min ago"
	case s < 86400:
		return strconv.Itoa(s/3600) + " h ago"
	}
	return strconv.Itoa(s/86400) + " d ago"
}

func templates(now func() time.Time) *template.Template {
	lockup, err := staticFS.ReadFile("static/lockup.svg")
	if err != nil {
		panic(err)
	}
	clock := template.FuncMap{"ago": func(ts float64) string { return ago(now().Sub(unix(ts))) }}
	t := template.Must(template.New("dash").Funcs(funcs).Funcs(clock).Parse(pageHTML))
	return template.Must(t.New("lockup").Parse(string(lockup)))
}

// Server keeps the last rendered <main> and pushes each new one to every open page.
type Server struct {
	store           *Store
	tmpl            *template.Template
	port            int
	linearWorkspace string
	hqPid           int
	now             func() time.Time

	mu      sync.Mutex
	main    []byte
	view    View
	version int
	clients map[chan []byte]struct{}
	done    chan struct{}
}

func NewServer(stateDir string, port int, linearWorkspace string) *Server {
	s := &Server{store: NewStore(stateDir), port: port, linearWorkspace: linearWorkspace,
		now: time.Now, clients: map[chan []byte]struct{}{}, done: make(chan struct{})}
	s.tmpl = templates(func() time.Time { return s.now() })
	return s
}

// Refresh re-reads the state when force is set or a file changed, and broadcasts the result if the page differs.
// A panic while building or rendering keeps the last good page.
func (s *Server) Refresh(force bool) (changed bool) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("render failed, keeping the last good page: %v", r)
			changed = false
		}
	}()
	if !s.store.Changed() && !force {
		return false
	}
	now := s.now()
	view := Build(s.store.Load(), float64(now.UnixNano())/1e9, s.linearWorkspace)
	var buf bytes.Buffer
	if err := s.tmpl.ExecuteTemplate(&buf, "main", view); err != nil {
		log.Printf("render failed, keeping the last good page: %v", err)
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if bytes.Equal(withoutTimes(buf.Bytes()), withoutTimes(s.main)) {
		return false
	}
	s.main, s.view = buf.Bytes(), view
	s.version++
	for c := range s.clients {
		offer(c, s.main)
	}
	return true
}

var timeText = regexp.MustCompile(`(<time [^>]*>)[^<]*(</time>)`)

// withoutTimes drops the "4 min ago" texts, which the page's own timer keeps current, so the minute's re-render
// pushes a page only when something else changed.
func withoutTimes(page []byte) []byte {
	return timeText.ReplaceAll(page, []byte("$1$2"))
}

// offer hands a page to a client without blocking: a slow client only ever gets the newest page.
func offer(c chan []byte, page []byte) {
	select {
	case c <- page:
	default:
		select {
		case <-c:
		default:
		}
		select {
		case c <- page:
		default:
		}
	}
}

// Poll refreshes every interval while files change, and re-renders every minute so time windows move on.
func (s *Server) Poll(interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	last := time.Now()
	for {
		select {
		case <-s.done:
			return
		case <-t.C:
			force := time.Since(last) >= time.Minute
			if force {
				last = time.Now()
			}
			s.Refresh(force)
		}
	}
}

func (s *Server) Stop() {
	select {
	case <-s.done:
	default:
		close(s.done)
	}
}

func (s *Server) snapshot() ([]byte, View, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.main, s.view, s.version
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.page)
	mux.HandleFunc("/events", s.events)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		_, _, version := s.snapshot()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"pid": os.Getpid(), "version": version, "state": s.store.dir, "hq_pid": s.hqPid})
	})
	static, _ := fs.Sub(staticFS, "static")
	files := http.StripPrefix("/static/", http.FileServer(http.FS(static)))
	mux.Handle("/static/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "max-age=3600")
		files.ServeHTTP(w, r)
	}))
	return s.localOnly(mux)
}

// localOnly answers only requests addressed to this server by its loopback name, so a web page cannot read
// the dashboard by pointing its own domain at 127.0.0.1 (DNS rebinding).
func (s *Server) localOnly(next http.Handler) http.Handler {
	allowed := map[string]bool{}
	for _, h := range []string{"127.0.0.1", "localhost", "[::1]"} {
		allowed[h+":"+strconv.Itoa(s.port)] = true
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !allowed[r.Host] {
			http.Error(w, "athena dash answers on 127.0.0.1 only", http.StatusMisdirectedRequest)
			return
		}
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:")
		next.ServeHTTP(w, r)
	})
}

func (s *Server) page(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	_, view, _ := s.snapshot()
	scheme := r.URL.Query().Get("scheme")
	if scheme != "light" && scheme != "dark" {
		scheme = ""
	}
	var buf bytes.Buffer
	err := s.tmpl.ExecuteTemplate(&buf, "page", map[string]any{"Title": view.Title, "Scheme": scheme, "View": view})
	if err != nil {
		http.Error(w, "render failed", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Write(buf.Bytes())
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	c := make(chan []byte, 1)
	s.mu.Lock()
	s.clients[c] = struct{}{}
	current := s.main
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.clients, c)
		s.mu.Unlock()
	}()
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-store")
	fmt.Fprint(w, "retry: 2000\n\n")
	writeEvent(w, current)
	flusher.Flush()
	ping := time.NewTicker(25 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-s.done:
			return
		case page := <-c:
			writeEvent(w, page)
			flusher.Flush()
		case <-ping.C:
			fmt.Fprint(w, ": ping\n\n")
			flusher.Flush()
		}
	}
}

// writeEvent sends one `main` event; every line of the HTML is its own data line, as SSE requires.
func writeEvent(w http.ResponseWriter, page []byte) {
	var buf bytes.Buffer
	buf.WriteString("event: main\n")
	for _, line := range bytes.Split(page, []byte("\n")) {
		buf.WriteString("data: ")
		buf.Write(bytes.TrimRight(line, "\r"))
		buf.WriteByte('\n')
	}
	buf.WriteByte('\n')
	w.Write(buf.Bytes())
}

// Listen binds 127.0.0.1 only.
func Listen(port int) (net.Listener, error) {
	return net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
}
