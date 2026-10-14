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

	stuckAfter = 600 // seconds a queued answer may wait for athena before its ask shows again
	stuckWhy   = "athena hasn't picked this up yet"
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
	Note      template.HTML // an ask's text, in athena's voice
	Panel     *Panel        // an ask's detail, expanded under it
	Folds     []Fold        // asks about this row's PR, shown under it
	Actions   *Actions      // the pills the row offers, or nil
	Sent      string        // a PR row's button answer athena has queued: "merge go sent"

	pr     *PR    // its PR, for the Awaiting review row
	branch string // that PR's head branch
	moved  bool   // placed by its PR's whose-move state, not by its worker's own state
}

// Fold is an ask shown under the row of the PR it is about.
type Fold struct {
	ID      string
	Text    template.HTML
	Label   string // "athena asks" or "note": the line its pills take the place of
	Why     string
	Actions *Actions
	Panel   *Panel
}

// Panel is an ask's detail, a native <details> under its row or fold: its why line is the summary, which opens on the
// ask's whole text (when its title cuts it), its Markdown body, its previews, and its answers as full-size buttons.
// Its Actions are the row's own, so the server checks an answer from either the same way.
type Panel struct {
	Ask      string        // the ask's id: the page keeps a panel open across pushes by it
	Why      string        // the summary's line
	Text     template.HTML // the ask's whole text, when its title is cut (an ask from before titles were short)
	Body     template.HTML // rendered by renderMarkdown
	Previews []Preview
	Actions  *Actions
}

// Preview is a link to a preview the dash serves sandboxed, opened in a tab of its own.
type Preview struct {
	Name, URL string
}

// Actions are what a row or fold lets the human answer from the page, each a POST to /api/action naming Item.
type Actions struct {
	Item    string
	Options []Option // athena's, at most two
	Discuss bool     // "Discuss in Athena"
	Reply   bool     // "Reply…": a line of free text
	Dismiss bool     // a note athena gave no options, or a PR on the Awaiting review row: "×"
	Recheck bool     // a PR on the Awaiting review row, or a Needs you row's with failing checks: "Re-check and fix", one queued at a time
	Hides   bool     // an answer hides the ask until athena takes it up

	// A Needs you row placed by its PR's whose-move state answers for that PR (Item pr:owner/name#n), with the buttons
	// that fit why it needs the human (prActions). None of them writes to GitHub: each queues an answer for athena.
	ForPR         bool   // Discuss shows as its icon alone beside the PR's buttons
	RequestReview bool   // no reviewer requested: "Request review", which athena does with the repo's team
	Go            bool   // ready, in a repo athena merges: "Merge", the human's go for the PR at Head only
	Head          string // the head commit a go is for, as the page shows the PR
	OpenPR        string // the PR's URL: "Open PR", a plain link, where the human merges or answers the review
	Num           string // the PR's #n, for the link's name
}

// sentText: what a PR row says while its button's answer waits for athena; each such answer is one at a time.
var sentText = map[string]string{"recheck": "re-check sent", "request-review": "review request sent", "go": "merge go sent"}

// RowOptions are the options the row's pills show: past two they are the panel's alone, with Discuss and Reply on the row.
func (a *Actions) RowOptions() []Option {
	if len(a.Options) > optionsMax {
		return nil
	}
	return a.Options
}

// OnRow: whether the row has any pill to show. A note whose 3 or 4 options are its panel's alone has none, and its
// label line then stays as it is on hover.
func (a *Actions) OnRow() bool {
	return len(a.RowOptions()) > 0 || a.Discuss || a.Reply || a.Dismiss || a.Recheck || a.RequestReview || a.Go || a.OpenPR != ""
}

// allows: whether a POST's answer is one these actions offer.
func (a *Actions) allows(option, text string, discuss bool) bool {
	switch {
	case discuss:
		return a.Discuss
	case text != "":
		return a.Reply
	case option == "dismiss":
		return a.Dismiss
	case option == "recheck":
		return a.Recheck
	case option == "request-review":
		return a.RequestReview
	case option == "go":
		return a.Go
	}
	for _, o := range a.Options {
		if o.ID == option {
			return true
		}
	}
	return false
}

type PRView struct {
	URL, Text, Checks, Review string
	StaleSince                float64 // when set, the PR state is from watch.json as of this time
}

// Pill is a PR on the Awaiting review row: one that waits on a teammate's review, with the PRs stacked on it behind.
type Pill struct {
	ID, Key, URL string // Key: owner/name#n
	Title        string // in full, for the tooltip
	Short        string // the title as the pill shows it
	Num          string // "#906"
	Label        string // whose move it waits on: "awaiting review · alex", or a dependent's own state
	WaitsOn      string // a dependent's: the #n of the PR it is stacked on
	Rechecking   bool   // a re-check is queued for athena
	Actions      *Actions
	Behind       []Pill // the PRs stacked on it, in chain order

	head string
}

type View struct {
	Title                      string
	Awaiting                   []Pill
	AwaitingCount              int // every PR on the row, those stacked behind a pill too
	NeedsYou, InProgress, Done []Item
	Notices                    []string

	actionable map[string]*Actions // every row and fold the page offers actions on, by Item
	pills      map[string]*Pill    // every pill on the Awaiting review row, front or behind, by its action item
	awaiting   []Item              // PRs waiting on a teammate's review, before stacking
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
			it.Actions = &Actions{Item: it.ID, Discuss: true, Reply: true} // no dismiss: the row is the worker's state
			it.pr, it.branch = pr, w.Branch
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
					it.pr, it.branch = f, w.Branch
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
		it := Item{ID: "pr-" + slug(k), Name: repoName(prKey(p.URL)), Title: p.Title, PR: prView(p, followedStale), pr: p}
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
	stackAwaiting(&v, s.Stacks, s.Dismissed, waiting(s.Actions), now)
	v.AwaitingCount = len(v.pills)
	foldAsks(&v, s.Asks, waiting(s.Actions), now) // once every row is in, retired ones too
	markSent(&v, s.Actions, now)
	sort.SliceStable(v.NeedsYou, func(i, j int) bool { return needRank(v.NeedsYou[i]) < needRank(v.NeedsYou[j]) })
	sort.SliceStable(v.Done, func(i, j int) bool { return v.Done[i].When > v.Done[j].When })
	if len(v.Done) > doneMax {
		v.Done = v.Done[:doneMax]
	}
	v.Title = "athena"
	if n := len(v.NeedsYou); n > 0 {
		v.Title = fmt.Sprintf("(%d) athena", n)
	}
	v.actionable = map[string]*Actions{}
	for key, p := range v.pills {
		v.actionable[key] = p.Actions
	}
	for _, group := range [][]Item{v.NeedsYou, v.InProgress, v.Done} {
		for _, it := range group {
			if it.Actions != nil {
				v.actionable[it.Actions.Item] = it.Actions
			}
			for _, f := range it.Folds {
				if f.Actions != nil {
					v.actionable[f.Actions.Item] = f.Actions
				}
			}
		}
	}
	return v
}

// waiting: when each item's oldest still queued answer was given.
func waiting(actions []Action) map[string]float64 {
	out := map[string]float64{}
	for _, a := range actions {
		if a.Status != "queued" {
			continue
		}
		if ts, ok := out[a.Item]; !ok || a.TS < ts {
			out[a.Item] = a.TS
		}
	}
	return out
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

// foldAsks: an ask about a PR with a row (its `pr`, else a PR URL in its text) shows under that row; any other is a
// row of its own, "athena asks" with the question-mark glyph for a question, and "note" with the thread's dash otherwise. An ask the human answered from the page is
// hidden while athena has the answer queued, and shows again, saying so, once it has waited stuckAfter.
func foldAsks(v *View, asks []Ask, queued map[string]float64, now float64) {
	rows := map[string]*Item{}
	for _, group := range [][]Item{v.NeedsYou, v.InProgress, v.Done} {
		for i := range group {
			if group[i].PR != nil && prKey(group[i].PR.URL) != "" {
				rows[prKey(group[i].PR.URL)] = &group[i]
			}
		}
	}
	var own []Item
	for _, a := range asks {
		why := a.Why
		if ts, ok := queued[a.ID]; ok {
			if now-ts < stuckAfter {
				continue
			}
			why = stuckWhy
		}
		acts := &Actions{Item: a.ID, Options: a.Options, Hides: true}
		if a.Kind == "question" {
			acts.Discuss, acts.Reply = true, true
		} else if len(a.Options) == 0 {
			acts.Dismiss = true
		}
		row := rows[a.PR]
		for _, m := range prURL.FindAllString(a.Text, -1) {
			if row != nil {
				break
			}
			row = rows[prKey(m)]
		}
		icon, label := "note", "note"
		if a.Kind == "question" {
			icon, label = "ask", "athena asks"
		}
		title, long := a.Title()
		ownPR := ""
		if row != nil {
			ownPR = prKey(row.PR.URL)
		}
		panel := askPanel(a, why, long, ownPR, acts)
		if row != nil {
			row.Folds = append(row.Folds, Fold{ID: "ask-" + a.ID, Text: inlineNote(title, ownPR), Label: label, Why: why, Actions: acts, Panel: panel})
			continue
		}
		own = append(own, Item{ID: "ask-" + a.ID, Note: inline(title), Icon: icon, Label: label, Detail: why, Actions: acts, Panel: panel})
	}
	v.NeedsYou = append(v.NeedsYou, own...)
}

// askPanel is an ask's detail, or nil for an ask with only its title and why: nothing more to show.
func askPanel(a Ask, why string, long bool, ownPR string, acts *Actions) *Panel {
	if a.Body == "" && !long && len(a.Previews) == 0 {
		return nil
	}
	p := &Panel{Ask: a.ID, Why: why, Actions: acts}
	if long {
		p.Text = inlineNote(a.Text, ownPR)
	}
	if a.Body != "" {
		p.Body = renderMarkdown(a.Body)
	}
	for _, name := range a.Previews {
		p.Previews = append(p.Previews, Preview{Name: strings.TrimSuffix(name, "/"), URL: "/preview/" + a.ID + "/" + name})
	}
	return p
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
	it.Label, it.Badge, it.moved = m.Label, m.Needs, true
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
		if m.Kind == "failing" || m.Kind == "pending" {
			it.PR.Checks = "" // "checks failing" or "checks running": the label says that too
		}
	}
	if m.Kind == "awaiting" && it.pr != nil && prKey(it.pr.URL) != "" {
		v.awaiting = append(v.awaiting, it) // the Awaiting review row's, once stackAwaiting has sorted out the stacks
		return
	}
	if m.Needs {
		if it.pr != nil && prKey(it.pr.URL) != "" {
			it.Actions = prActions(it, m)
		}
		v.NeedsYou = append(v.NeedsYou, it)
	} else {
		v.InProgress = append(v.InProgress, it)
	}
}

// prActions: a Needs you PR row's buttons, by why it needs the human. Discuss on every one; Request review when no
// reviewer is requested; Re-check when its checks fail; Open PR when it is the human's to merge or answer, with Merge
// beside it only in a repo athena merges (never one in human_merge_repos) and only for a head the page can name.
func prActions(it Item, m *Move) *Actions {
	a := &Actions{Item: pillItem + prKey(it.pr.URL), Discuss: true, ForPR: true}
	switch m.Kind {
	case "unrequested":
		a.RequestReview = true
	case "failing":
		a.Recheck = true
	case "approved", "mergeable":
		a.OpenPR, a.Num = it.pr.URL, "#"+strconv.Itoa(prNumberOf(prKey(it.pr.URL)))
		a.Go, a.Head = m.AthenaMerges && it.pr.Head != "", it.pr.Head
	case "changes":
		a.OpenPR, a.Num = it.pr.URL, "#"+strconv.Itoa(prNumberOf(prKey(it.pr.URL)))
	default:
		a.ForPR, a.Reply = false, true
	}
	return a
}

// markSent: a PR row whose button answer athena has queued says so, and offers that button again only once the
// answer is done, undone or stuck (stuckAfter).
func markSent(v *View, actions []Action, now float64) {
	sent := map[string]string{}
	for _, a := range actions {
		if a.Status == "queued" && now-a.TS < stuckAfter && sentText[a.Option] != "" {
			sent[a.Item] = sentText[a.Option]
		}
	}
	for i := range v.NeedsYou {
		it := &v.NeedsYou[i]
		if a := it.Actions; a != nil && a.ForPR && sent[a.Item] != "" {
			it.Sent = sent[a.Item]
			a.RequestReview, a.Recheck, a.Go = false, false, false
		}
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

// links finds a markdown link to an http(s) url, or a bare url, which never ends in punctuation: the period after
// "see https://x.test/a." ends the sentence, not the URL. athena_lib/asks.py _LINK is the same.
var (
	links  = regexp.MustCompile(`\[([^\]]+)\]\((https?://[^)\s]+)\)|https?://[^\s<)]*[^\s<).,;:!?'"]`)
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

func prose(s string) string {
	out := template.HTMLEscapeString(s)
	out = strong.ReplaceAllString(out, `<strong>$1</strong>`)
	return code.ReplaceAllString(out, `<span class="code">$1</span>`)
}
