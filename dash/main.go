// athena dash: a read-only page of athena's work on 127.0.0.1, built from the state files and updated live.
// `athena dash on|off|show|open` runs it; see docs/dashboard.md.
package main

import (
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

const defaultPort = 2843

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	home, _ := os.UserHomeDir()
	port, _ := strconv.Atoi(envOr("ATHENA_DASH_PORT", strconv.Itoa(defaultPort)))
	stateDir := flag.String("state-dir", envOr("ATHENA_STATE_DIR", filepath.Join(home, ".local/state/athena")), "athena's state dir")
	flag.IntVar(&port, "port", port, "port on 127.0.0.1")
	hqPid := flag.Int("hq-pid", 0, "exit when this process (HQ's pane shell) is gone; 0 runs until stopped")
	linear := flag.String("linear-workspace", envOr("ATHENA_LINEAR_WORKSPACE", "sunsecurity"), "Linear workspace for ticket links")
	poll := flag.Duration("poll", time.Second, "how often to look for changed state files")
	flag.Parse()
	log.SetFlags(log.LstdFlags)

	srv := NewServer(*stateDir, port, *linear)
	srv.Refresh(true)
	ln, err := Listen(port)
	if err != nil {
		if errors.Is(err, syscall.EADDRINUSE) {
			fmt.Fprintf(os.Stderr, "port %d is in use; set ATHENA_DASH_PORT to another port\n", port)
		} else {
			fmt.Fprintf(os.Stderr, "cannot listen on 127.0.0.1:%d: %v\n", port, err)
		}
		os.Exit(3)
	}
	httpSrv := &http.Server{Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}
	go srv.Poll(*poll)
	stop := make(chan string, 1)
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
		stop <- (<-sig).String()
	}()
	if *hqPid > 0 {
		go watchPid(*hqPid, 2*time.Second, stop)
	}
	go func() {
		reason := <-stop
		log.Printf("stopping: %s", reason)
		srv.Stop()
		httpSrv.Close()
	}()
	log.Printf("athena dash on http://127.0.0.1:%d/ (pid %d, state %s, hq pid %d)", port, os.Getpid(), *stateDir, *hqPid)
	if err := httpSrv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

// watchPid sends on stop once pid no longer exists. EPERM means it exists under another user.
func watchPid(pid int, every time.Duration, stop chan<- string) {
	for range time.Tick(every) {
		if err := syscall.Kill(pid, 0); errors.Is(err, syscall.ESRCH) {
			stop <- fmt.Sprintf("HQ (pid %d) is gone", pid)
			return
		}
	}
}
