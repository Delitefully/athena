package main

import (
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"syscall"
)

// previewsDir holds each ask's previews, <state>/previews/<id>/, copied there by `athena ask add --preview`.
const previewsDir = "previews"

// previewCSP sandboxes every preview without allow-same-origin: the document gets an opaque origin, so its script can
// neither read the dash's pages (and the token in them) nor pass the Origin check of POST /api/action, whose Origin it
// sends as "null". allow-scripts lets an interactive mock run.
const previewCSP = "sandbox allow-scripts"

// preview serves a file of an ask's preview, read-only and sandboxed. A path with a hidden, empty, "." or ".." segment
// is refused, and so is any symlink along it: os.DirFS and http.FS would follow one out of the folder.
func (s *Server) preview(w http.ResponseWriter, r *http.Request) {
	h := w.Header()
	h.Set("Content-Security-Policy", previewCSP)
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/preview/"), "/")
	if len(parts) < 2 || !askID.MatchString(parts[0]) {
		http.NotFound(w, r)
		return
	}
	dirWanted := parts[len(parts)-1] == "" // a folder's URL ends in /
	if dirWanted {
		parts = parts[:len(parts)-1]
	}
	for _, p := range parts[1:] {
		if p == "" || strings.HasPrefix(p, ".") || strings.ContainsAny(p, "\\\x00") {
			http.NotFound(w, r)
			return
		}
	}
	full := filepath.Join(s.store.dir, previewsDir)
	fi, err := os.Lstat(full)
	for _, p := range parts {
		if err != nil || fi.Mode()&os.ModeSymlink != 0 {
			http.NotFound(w, r)
			return
		}
		full = filepath.Join(full, p)
		fi, err = os.Lstat(full)
	}
	isDir := err == nil && fi.IsDir()
	if isDir {
		if !dirWanted {
			http.Redirect(w, r, path.Base(r.URL.Path)+"/", http.StatusMovedPermanently) // its relative links resolve inside it
			return
		}
		full = filepath.Join(full, "index.html") // a folder shows its index.html, never a listing
		fi, err = os.Lstat(full)
	}
	if err != nil || !fi.Mode().IsRegular() || dirWanted && !isDir {
		http.NotFound(w, r)
		return
	}
	f, err := os.OpenFile(full, os.O_RDONLY|syscall.O_NOFOLLOW, 0) // never through a symlink swapped in since the Lstat
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	http.ServeContent(w, r, fi.Name(), fi.ModTime(), f)
}
