package main

import (
	"fmt"
	"html/template"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	doneWindow = 14 * 24 * 3600 // seconds a retired worker stays under Done
	doneMax    = 12
	staleAfter = 120 // seconds after which watch.json's PR state is shown with its age

	reviewRequestsMax = 10 // teammates' PRs asking for the human's review, at most
)

// Item is one row on the page.
type Item struct {
	ID        string // stable across renders, so the page animates only rows that are new
	Ticket    string
	TicketURL string
	Name      string
	Title     string
	Repo      string
	Icon      string // an icon id from the page's sprite
	Badge     bool   // the human acts on it: its label is set in ink
	Label     string
	Detail    string
	When      float64
	PR        *PRView
	Note      template.HTML   // a needs-you.md line, in athena's voice
	Folds     []template.HTML // needs-you.md lines that link this row's PR, shown under it
}

type PRView struct {
	URL, Text, Checks, Review string
	StaleSince                float64 // when set, the PR state is from watch.json as of this time
}

type View struct {
	Title                      string
	NeedsYou, InProgress, Done []Item
	Notices                    []string
}

// Build sorts the snapshot into the page's three groups. now is in Unix seconds.
func Build(s Snapshot, now float64, linearWorkspace string) View {
	var v View
	v.Notices = s.Notices
	if linearWorkspace == "" {
		linearWorkspace = s.LinearWorkspace
	}
	history := map[string]HistoryEntry{}
	for _, h := range s.History {
		if h.TS >= history[h.Name].TS {
			history[h.Name] = h
		}
	}
	var watchTS float64
	watched := map[string]WatchWorker{}
	if s.Watch != nil {
		watchTS, watched = s.Watch.TS, s.Watch.Workers
	}

	var followedPRs map[string]*PR
	var reviewRequests []ReviewRequest
	if s.Watch != nil {
		followedPRs, reviewRequests = s.Watch.PRs, s.Watch.ReviewRequests
	}
	followedStale := 0.0
	if now-watchTS > staleAfter {
		followedStale = watchTS
	}
	owned := map[string]*PR{} // a retired worker's PR, as athena watch follows it
	for _, p := range followedPRs {
		if p.Owner != "" {
			owned[p.Owner] = p
		}
	}
	shown := map[string]bool{} // PRs that have a row: each gets one

	var retired []Item
	for _, w := range s.Workers {
		hook := s.Hooks[w.Name]
		it := Item{ID: "w-" + w.Name, Name: w.Name, Title: w.Title, Repo: filepath.Base(w.Repo)}
		if w.Repo == "" {
			it.Repo = ""
		}
		if w.Linear != "" {
			it.Ticket = strings.ToUpper(w.Linear)
			if linearWorkspace != "" {
				it.TicketURL = "https://linear.app/" + linearWorkspace + "/issue/" + it.Ticket
			}
		}
		switch w.State {
		case "live":
			ww, inWatch := watched[w.Name]
			pr := ww.PR
			stale := 0.0
			if pr != nil && now-watchTS > staleAfter {
				stale = watchTS
			}
			if pr == nil {
				pr = prFromReport(hook.Report, hook.LastReport)
			}
			it.PR = prView(pr, stale)
			it.When = maxf(hook.TS, w.Updated)
			if pr != nil {
				shown[prKey(pr.URL)] = true
			}
			state := hook.State
			if (state == "" || state == "unknown" || state == "starting") && inWatch {
				switch ww.State {
				case "blocked", "working", "idle", "done":
					state = ww.State // herdr's reading, as `athena status` merges it
				}
			}
			placeLive(&v, it, w, hook, state, pr)
		case "retired":
			if now-w.Updated > doneWindow {
				continue
			}
			it.When = w.Updated
			pr := history[w.Name].PR
			if pr == nil {
				pr = prFromReport(hook.Report, hook.LastReport)
			}
			if f := owned[w.Name]; f != nil && (pr == nil || prKey(pr.URL) == prKey(f.URL)) {
				shown[prKey(f.URL)] = true
				if f.State == "OPEN" && f.Move != nil {
					it.PR = prView(f, followedStale)
					placeMove(&v, it, f.Move)
					continue // still open: its row says whose move it is
				}
				pr = f
			}
			if pr == nil && w.Linear == "" {
				continue // a probe or experiment: it stays in the ledger, off the page
			}
			it.PR = prView(pr, 0)
			it.Icon, it.Label = "retired", "retired"
			if pr != nil {
				switch pr.State {
				case "MERGED":
					it.Icon, it.Label = "merged", "merged"
				case "CLOSED":
					it.Label = "closed unmerged"
				}
			}
			retired = append(retired, it)
		}
	}
	keys := make([]string, 0, len(followedPRs))
	for k := range followedPRs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		p := followedPRs[k]
		if p.State != "OPEN" || p.Move == nil || shown[prKey(p.URL)] || prKey(p.URL) == "" {
			continue
		}
		shown[prKey(p.URL)] = true
		it := Item{ID: "pr-" + slug(k), Name: repoName(prKey(p.URL)), Title: p.Title, PR: prView(p, followedStale)}
		placeMove(&v, it, p.Move)
	}
	n := 0
	for _, r := range reviewRequests {
		if !repoSlug.MatchString(r.Repo) || r.Number <= 0 || n >= reviewRequestsMax {
			continue
		}
		url := "https://github.com/" + r.Repo + "/pull/" + strconv.Itoa(r.Number)
		if shown[prKey(url)] {
			continue
		}
		shown[prKey(url)] = true
		n++
		it := Item{ID: "rr-" + slug(r.Repo+"-"+strconv.Itoa(r.Number)), Name: repoName(prKey(url)), Title: r.Title,
			Icon: "done", Badge: true, Label: "your review requested", PR: &PRView{URL: url, Text: "#" + strconv.Itoa(r.Number)}}
		if r.Author != "" {
			it.Detail = "by " + r.Author
		}
		v.NeedsYou = append(v.NeedsYou, it)
	}
	v.Done = append(v.Done, retired...)
	foldNotes(&v, s.Notes) // once every row is in, retired ones too
	sort.SliceStable(v.NeedsYou, func(i, j int) bool { return needRank(v.NeedsYou[i]) < needRank(v.NeedsYou[j]) })
	sort.SliceStable(v.Done, func(i, j int) bool { return v.Done[i].When > v.Done[j].When })
	if len(v.Done) > doneMax {
		v.Done = v.Done[:doneMax]
	}
	v.Title = "athena"
	if n := len(v.NeedsYou); n > 0 {
		v.Title = fmt.Sprintf("(%d) athena", n)
	}
	return v
}

func needRank(it Item) int {
	switch {
	case it.Note != "":
		return 2
	case it.Icon == "blocked" || it.Icon == "fail":
		return 0
	}
	return 1
}

// foldNotes: a needs-you.md line that links a PR with a row shows under that row; any other is a row of its own,
// "athena asks" with the question-mark glyph when it asks something (a question mark in its text), and "note"
// with the thread's dash otherwise.
func foldNotes(v *View, notes []string) {
	rows := map[string]*Item{}
	for _, group := range [][]Item{v.NeedsYou, v.InProgress, v.Done} {
		for i := range group {
			if group[i].PR != nil && prKey(group[i].PR.URL) != "" {
				rows[prKey(group[i].PR.URL)] = &group[i]
			}
		}
	}
	var own []Item
	for i, n := range notes {
		var row *Item
		for _, m := range prURL.FindAllString(n, -1) {
			if r := rows[prKey(m)]; r != nil {
				row = r
				break
			}
		}
		if row != nil {
			row.Folds = append(row.Folds, inlineNote(n, prKey(row.PR.URL)))
			continue
		}
		icon, label := "note", "note"
		if asks(n) {
			icon, label = "ask", "athena asks"
		}
		own = append(own, Item{ID: "n-" + strconv.Itoa(i) + "-" + slug(n), Note: inline(n), Icon: icon, Label: label})
	}
	v.NeedsYou = append(v.NeedsYou, own...)
}

var (
	prURL    = regexp.MustCompile(`https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+`)
	prKeyURL = regexp.MustCompile(`^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)`)
	repoSlug = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$`)
)

// prKey is a PR's `owner/name#n`, lower case, from its GitHub URL; empty for any other URL.
func prKey(url string) string {
	m := prKeyURL.FindStringSubmatch(url)
	if m == nil {
		return ""
	}
	return strings.ToLower(m[1]) + "#" + m[2]
}

func repoName(key string) string {
	repo, _, _ := strings.Cut(key, "#")
	_, name, _ := strings.Cut(repo, "/")
	return name
}

// moveOverWorking: the moves a working worker's row shows; on any other, the row says `working`.
var moveOverWorking = map[string]bool{"approved": true, "queued": true, "changes": true}

// moveMerges: the moves that make a PR the human's to merge. A live worker's PR is that only once the worker
// reported DONE; until then its row shows the worker's own state (athena_lib/watch.py `_reported` agrees).
var moveMerges = map[string]bool{"approved": true, "mergeable": true}

// placeMove puts a PR's row where whose move it is says: Needs you when it is the human's, else In progress.
func placeMove(v *View, it Item, m *Move) {
	it.Label, it.Badge = m.Label, m.Needs
	switch m.Kind {
	case "approved", "mergeable":
		it.Icon = "ready"
	case "queued":
		it.Icon = "ok"
	case "failing":
		it.Icon = "fail"
	case "pending":
		it.Icon = "working"
	case "unrequested":
		it.Icon = "done"
	case "changes":
		it.Icon = "working"
		if m.Needs {
			it.Icon = "done"
		}
	default:
		it.Icon = "pr"
	}
	if it.PR != nil {
		it.PR.Review = "" // the label says it
	}
	if m.Needs {
		v.NeedsYou = append(v.NeedsYou, it)
	} else {
		v.InProgress = append(v.InProgress, it)
	}
}

func placeLive(v *View, it Item, w Worker, hook Hook, state string, pr *PR) {
	report := hook.Report
	summary := summary(hook)
	switch {
	case w.GoalPending:
		it.Icon, it.Badge, it.Label = "blocked", true, "waiting at a startup prompt"
		it.Detail = "Answer it in its space; athena then sends the goal."
		v.NeedsYou = append(v.NeedsYou, it)
	case state == "blocked":
		it.Icon, it.Badge = "blocked", true
		it.Label, it.Detail = "permission prompt", summary
		if report != nil && (report.Status == "BLOCKED" || report.Status == "NEEDS_CONTEXT") {
			it.Label = map[string]string{"BLOCKED": "blocked", "NEEDS_CONTEXT": "needs context"}[report.Status]
			if len(report.Concerns) > 0 {
				it.Detail = oneLine(report.Concerns[0])
			}
		}
		v.NeedsYou = append(v.NeedsYou, it)
	case pr != nil && pr.State == "MERGED":
		it.Icon, it.Label = "merged", "merged, not retired yet"
		v.Done = append(v.Done, it)
	case pr != nil && pr.State == "OPEN" && !pr.Draft && pr.Move != nil && (state != "working" || moveOverWorking[pr.Move.Kind]) &&
		(!moveMerges[pr.Move.Kind] || reported(state, hook)):
		it.Detail = summary
		placeMove(v, it, pr.Move)
	default:
		it.Icon, it.Label, it.Detail = stateIcon(state, pr), stateLabel(state, report), summary
		if it.Detail == "" && report != nil && len(report.Concerns) > 0 {
			it.Detail = oneLine(report.Concerns[0])
		}
		if pr != nil && pr.Checks == "failure" {
			it.Icon = "fail"
		}
		v.InProgress = append(v.InProgress, it)
	}
}

// reported: the worker finished its goal. A Claude that went idle or exited after a DONE report still counts.
func reported(state string, h Hook) bool {
	if state == "done" {
		return true
	}
	r := h.Report
	if r == nil {
		r = h.LastReport
	}
	return (state == "idle" || state == "exited") && r != nil && (r.Status == "DONE" || r.Status == "DONE_WITH_CONCERNS")
}

func stateIcon(state string, pr *PR) string {
	switch state {
	case "done":
		return "review"
	case "working":
		return "working"
	case "exited":
		return "exited"
	}
	if pr != nil {
		return "pr"
	}
	return "idle"
}

func stateLabel(state string, r *Report) string {
	switch state {
	case "done":
		if r != nil && r.Status == "DONE_WITH_CONCERNS" {
			return "done with concerns, athena reviewing"
		}
		return "done, athena reviewing"
	case "working":
		return "working"
	case "idle":
		return "idle"
	case "exited":
		return "its Claude exited"
	case "", "unknown", "starting":
		return "starting"
	}
	return state
}

var prNumber = regexp.MustCompile(`/pull/(\d+)`)

// prFromReport builds a bare PR from the url in the newest report, when watch.json has none.
func prFromReport(reports ...*Report) *PR {
	for _, r := range reports {
		if r != nil && strings.HasPrefix(r.PR, "https://") {
			n := 0
			if m := prNumber.FindStringSubmatch(r.PR); m != nil {
				n, _ = strconv.Atoi(m[1])
			}
			return &PR{URL: r.PR, Number: n}
		}
	}
	return nil
}

func prView(pr *PR, stale float64) *PRView {
	if pr == nil {
		return nil
	}
	pv := &PRView{URL: pr.URL, Text: "PR", Checks: pr.Checks, StaleSince: stale}
	if pr.Number > 0 {
		pv.Text = "#" + strconv.Itoa(pr.Number)
	}
	if pr.Draft {
		pv.Text += " draft"
	}
	switch pr.Review {
	case "APPROVED":
		pv.Review = "approved"
	case "CHANGES_REQUESTED":
		pv.Review = "changes requested"
	}
	if pr.Checks == "none" {
		pv.Checks = ""
	}
	return pv
}

// reportTail matches the end of an ATHENA-REPORT line whose start was cut off by the hook's 300-character limit.
var reportTail = regexp.MustCompile(`"\s*[,:]\s*["\[{]|"\]?\s*}\s*$`)

// summary is the worker's last line of prose: never its ATHENA-REPORT, not even a cut-off tail of one.
func summary(h Hook) string {
	s := h.Summary
	if i := strings.LastIndex(s, "ATHENA-REPORT"); i >= 0 {
		s = s[:i]
	} else if (h.Report != nil || h.LastReport != nil) && reportTail.MatchString(s) {
		return ""
	}
	return lastLine(s)
}

// reportItemMax: the most a report's decision or concern shows, as worker.md asks of each entry.
const reportItemMax = 80

// oneLine: a report entry as one line. Whitespace runs fold to a space; past reportItemMax runes it is cut, the last an ellipsis.
func oneLine(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	if r := []rune(s); len(r) > reportItemMax {
		return strings.TrimRight(string(r[:reportItemMax-1]), " ") + "…"
	}
	return s
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if t := strings.TrimSpace(lines[i]); t != "" && !strings.HasPrefix(t, "ATHENA-REPORT") {
			return t
		}
	}
	return ""
}

func maxf(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

var nonSlug = regexp.MustCompile(`[^a-z0-9]+`)

func slug(s string) string {
	s = strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(s), "-"), "-")
	if len(s) > 32 {
		s = s[:32]
	}
	return s
}

// links finds a markdown link to an http(s) url, or a bare url.
var (
	links  = regexp.MustCompile(`\[([^\]]+)\]\((https?://[^)\s]+)\)|https?://[^\s<)]+`)
	strong = regexp.MustCompile(`\*\*([^*]+)\*\*`)
	code   = regexp.MustCompile("`([^`]+)`")
)

// inline renders the little markdown a needs-you line uses: links, bare URLs, **bold** and `code`.
// Text is escaped first, so nothing in the file can inject markup, and bold and code apply only outside links.
func inline(s string) template.HTML { return inlineNote(s, "") }

// inlineNote is inline for a note shown under the row of PR `own` (an owner/name#n key, or empty): a bare URL of
// that PR is dropped, since the row links it, and any other bare PR URL shows as its short `owner/name#n`.
func inlineNote(s, own string) template.HTML {
	var b strings.Builder
	last := 0
	for _, m := range links.FindAllStringSubmatchIndex(s, -1) {
		b.WriteString(prose(s[last:m[0]]))
		last = m[1]
		text, url := s[m[0]:m[1]], s[m[0]:m[1]]
		if m[2] >= 0 {
			text, url = s[m[2]:m[3]], s[m[4]:m[5]]
		} else if key := prKey(url); key != "" {
			if key == own {
				if strings.HasSuffix(b.String(), " ") && strings.HasPrefix(s[last:], " ") {
					last++ // one space where the URL was
				}
				continue
			}
			sm := prKeyURL.FindStringSubmatch(url)
			text, url = sm[1]+"#"+sm[2], sm[0]
		}
		fmt.Fprintf(&b, `<a href="%s">%s</a>`, template.HTMLEscapeString(url), template.HTMLEscapeString(text))
	}
	b.WriteString(prose(s[last:]))
	return template.HTML(strings.TrimRight(b.String(), " :;,-–—"))
}

// asks: a note that asks something, a question mark anywhere in its text (a URL's query does not count).
func asks(s string) bool {
	for _, m := range links.FindAllStringSubmatchIndex(s, -1) {
		if m[2] >= 0 && strings.Contains(s[m[2]:m[3]], "?") {
			return true
		}
	}
	return strings.Contains(links.ReplaceAllString(s, " "), "?")
}

func prose(s string) string {
	out := template.HTMLEscapeString(s)
	out = strong.ReplaceAllString(out, `<strong>$1</strong>`)
	return code.ReplaceAllString(out, `<span class="code">$1</span>`)
}
