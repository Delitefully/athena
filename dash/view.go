package main

import (
	"fmt"
	"html/template"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	doneWindow = 14 * 24 * 3600 // seconds a retired worker stays under Done
	doneMax    = 12
	staleAfter = 120 // seconds after which watch.json's PR state is shown with its age

	reviewRequestsMax = 10 // teammates' PRs asking for the human's review, at most

	stuckAfter = 600 // seconds a queued answer may wait for athena before its ask shows again
	stuckWhy   = "athena hasn't picked this up yet"
)

// Item is one row on the page: an item's, a worker's or a PR's. Its name comes first (plan §3): an item's 2 to 4
// words, a worker's title; ticket ids, repos and PR numbers are in its meta line and its full view.
type Item struct {
	ID        string // stable across renders, so the page animates only rows that are new
	Ticket    string
	TicketURL string
	Name      string        // what the row leads with
	Href      string        // the item's full view, /item/<id>, when the row is an item's
	Call      template.HTML // an item's call or where it stands, in athena's voice
	Title     string        // a worker's or PR's title, when Name is its worker's name
	Repo      string
	Icon      string // an icon id from the page's sprite
	Badge     bool   // the human acts on it: its label is set in ink
	Label     string
	Detail    string
	When      float64 // its age is from this
	Due       float64 // an item's due date
	PR        *PRView
	Actions   *Actions // the pills the row offers, or nil
	Sent      string   // a PR row's button answer athena has queued: "merge go sent"
	AutoAt    float64  // a default that applies on its own then, unless the human objects

	item   string   // the item it is, or that a worker's row carries
	finds  []string // what else the filter matches it on: that item's whole title, PR and workers
	pr     *PR      // its PR, for the Awaiting review row
	branch string   // that PR's head branch
	moved  bool     // placed by its PR's whose-move state, not by its worker's own state
	rank   int      // Needs you's order: due soon first
}

// Preview is a link to a preview the dash serves sandboxed: embedded in an item's full view, or opened in a tab of its own.
type Preview struct {
	Name, URL string
}

// Actions are what a row or fold lets the human answer from the page, each a POST to /api/action naming Item.
type Actions struct {
	Item    string
	Options []Option // athena's: every one in the item's full view
	TakeRec *Option  // the recommended option: "Take rec" on the row
	Done    bool     // the dash's own: the human took the last step, or read it
	Snooze  bool     // the dash's own: hide it for a while
	Undo    string   // a default athena applied on its own: "Undo <label>" queues undo-default for athena
	Rule    bool     // "Don't ask me this again" may go with an answer (a decision's)
	Open    string   // its full view
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

// RowOptions are the options the row's pills show: a worker's or PR's row has none; an item's row offers only the
// one-click calls (Take rec, Done, Snooze, Open), and its options are in its full view.
func (a *Actions) RowOptions() []Option {
	if a.Open != "" || len(a.Options) > optionsMax {
		return nil
	}
	return a.Options
}

// OnRow: whether the row has any pill to show.
func (a *Actions) OnRow() bool {
	return len(a.RowOptions()) > 0 || a.TakeRec != nil || a.Done || a.Snooze || a.Undo != "" || a.Open != "" ||
		a.Open == "" && (a.Discuss || a.Reply) || a.Dismiss || a.Recheck || a.RequestReview || a.Go || a.OpenPR != ""
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
	case option == undoDefault:
		return a.Undo != ""
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
	Title         string
	Health        HealthView
	Awaiting      []Pill
	AwaitingCount int // every PR on the row, those stacked behind a pill too
	NeedsYou      []Item
	Ready         []Item // results and applied defaults the human has not read: Done takes them off
	InProgress    []Item // In flight: athena's, a worker's or someone else's move
	Done          []Item // today's
	Snoozed       int
	Notices       []string

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
			if pr == nil && w.Linear == "" || now-w.Updated > doneToday {
				continue // a probe or experiment stays in the ledger, off the page; Done is today's
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
	placeItems(&v, s, now) // once every row is in: an item on a worker names its row
	markSent(&v, s.Actions, now)
	for i := range v.NeedsYou {
		v.NeedsYou[i].rank = needRank(v.NeedsYou[i], now)
	}
	sort.SliceStable(v.NeedsYou, func(i, j int) bool {
		a, b := v.NeedsYou[i], v.NeedsYou[j]
		if a.rank != b.rank {
			return a.rank < b.rank
		}
		switch a.rank {
		case 0:
			return a.Due < b.Due
		case 2:
			return a.When < b.When // the oldest call first: it has waited longest
		}
		return false
	})
	sort.SliceStable(v.Done, func(i, j int) bool { return v.Done[i].When > v.Done[j].When })
	if len(v.Done) > doneMax {
		v.Done = v.Done[:doneMax]
	}
	v.Health = healthView(s.Health, now)
	v.Title = "athena"
	if n := len(v.NeedsYou); n > 0 {
		v.Title = fmt.Sprintf("(%d) athena", n)
	}
	v.actionable = map[string]*Actions{}
	for key, p := range v.pills {
		v.actionable[key] = p.Actions
	}
	for _, group := range [][]Item{v.NeedsYou, v.Ready, v.InProgress, v.Done} {
		for _, it := range group {
			if it.Actions != nil {
				v.actionable[it.Actions.Item] = it.Actions
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

// needRank: Needs you's order. An item due within a day (or overdue) first, by due date: the meeting sorts to the top
// the day before. Then a blocked worker or failing checks, then the human's own calls (items), then PR rows.
func needRank(it Item, now float64) int {
	switch {
	case it.Due > 0 && it.Due-now < doneToday:
		return 0
	case it.Icon == "blocked" || it.Icon == "fail":
		return 1
	case strings.HasPrefix(it.ID, "ask-"):
		return 2
	}
	return 3
}

// kindLabel is how a row names an item's kind and whose move it is.
func kindLabel(a Ask) string {
	switch a.Kind {
	case "decision":
		if a.Next == "you" {
			return "your call"
		}
	case "result":
		return "result"
	case "promise":
		return "athena promised"
	case "incident":
		return "incident"
	}
	if a.Next == "you" {
		return "your move"
	}
	return a.Next + "'s move"
}

// placeItems puts every open item where whose move it is says (plan §3): Needs you when it is the human's call,
// Ready for you while a result or a default athena applied on its own is unread, In flight while it is anyone
// else's move, Done when it resolved today. A snoozed item waits off the page; one the human answered here is hidden
// while athena has the answer queued, and shows again, saying so, once it has waited stuckAfter. An item on a live
// worker (request, promise or incident) names that worker's row instead of a row of its own.
func placeItems(v *View, s Snapshot, now float64) {
	queued := waiting(s.Actions)
	auto := map[string]Action{}
	for _, a := range s.Actions {
		if a.Auto && a.Status == "queued" { // done or undone: the item's applied (or gone) says the rest
			auto[a.Item] = a
		}
	}
	rows := map[string]*Item{}
	for _, group := range [][]Item{v.NeedsYou, v.InProgress, v.Done} {
		for i := range group {
			rows[group[i].ID] = &group[i]
		}
	}
	merged := mergedPRs(s)
	for _, a := range s.Asks {
		title, _ := a.Title()
		// No why on the row: it is the full view's. The title leaves out the name it starts with, so the row says it once.
		it := Item{ID: "ask-" + a.ID, Name: a.Name, Href: "/item/" + a.ID, Call: inline(withoutName(title, a.Name)), Icon: "note",
			Label: kindLabel(a), When: a.Created, Due: a.Due, item: a.ID, finds: append([]string{a.Text, a.PR}, a.Workers...)}
		if a.Name == "" {
			it.Name = "Untitled item"
		}
		if a.PR != "" {
			it.PR = &PRView{URL: "https://github.com/" + strings.Replace(a.PR, "#", "/pull/", 1), Text: "#" + strconv.Itoa(prNumberOf(a.PR))}
		}
		acts := &Actions{Item: a.ID, Options: a.Options, Open: it.Href}
		it.Actions = acts
		if !a.Open() {
			if now-a.Resolved < doneToday {
				it.Icon, it.Label, it.When, it.Due, it.Actions = "ok", "resolved", a.Resolved, 0, nil
				if a.ResolvedNote != "" {
					it.Label = a.ResolvedNote
				}
				v.Done = append(v.Done, it)
			}
			continue
		}
		if a.Snoozed(now) {
			v.Snoozed++
			continue
		}
		if ap, ok := auto[a.ID]; ok || a.Applied != nil {
			label, verb := a.option(ap.Option).Label, "athena is taking the rec on its own: " // queued: HQ has yet to act
			if a.Applied != nil {
				label, verb = a.Applied.Label, "athena took the rec on its own: "
			}
			it.Icon, it.Badge, it.Label = "ready", true, verb+label
			acts.Done, acts.Undo = true, label
			v.Ready = append(v.Ready, it)
			continue
		}
		if ts, ok := queued[a.ID]; ok {
			if now-ts < stuckAfter {
				continue
			}
			it.Sent = stuckWhy
		}
		switch a.Kind {
		case "decision":
			acts.Discuss, acts.Reply, acts.Hides, acts.Rule = true, true, true, true
			acts.Snooze = true
			if a.Rec != "" {
				o := a.option(a.Rec)
				acts.TakeRec = &o
			}
			if a.Auto {
				it.AutoAt = applyAt(a, s.LastSeen, now)
			}
			it.Icon = "ask"
		case "result":
			acts.Done, acts.Snooze = true, true
			it.Icon, it.Badge = "done", true
		default:
			acts.Done = true
			it.Icon = map[string]string{"request": "working", "promise": "idle", "incident": "fail"}[a.Kind]
		}
		if a.Kind != "result" && a.Kind != "decision" && a.PR != "" && merged[a.PR] {
			it.Icon, it.Badge, it.Label = "merged", true, "merged: done when you have looked"
			acts.Snooze = true
			v.Ready = append(v.Ready, it)
			continue
		}
		if a.Kind != "decision" && a.Next != "you" {
			if row := workerRow(rows, a.Workers); row != nil {
				if row.Title == "" {
					row.Title = row.Name
				}
				row.Name, row.Href, row.item = it.Name, it.Href, a.ID
				row.finds = append(row.finds, it.finds...)
				if row.Ticket == "" {
					row.Ticket = strings.ToUpper(strings.TrimPrefix(row.ID, "w-"))
				}
				continue
			}
		}
		switch {
		case a.Kind == "result":
			v.Ready = append(v.Ready, it)
		case a.Next == "you":
			v.NeedsYou = append(v.NeedsYou, it)
		default:
			v.InProgress = append(v.InProgress, it)
		}
	}
}

// Lead is what the row starts with: an item's name, else a worker's or PR's title, else its name.
func (it Item) Lead() string {
	if it.Href == "" && it.Title != "" {
		return it.Title
	}
	return it.Name
}

// Second is what follows the lead: an item's call, or the title of the worker an item's name now leads.
func (it Item) Second() template.HTML {
	if it.Call != "" {
		return it.Call
	}
	if it.Href != "" && it.Title != "" && it.Title != it.Name {
		return template.HTML(template.HTMLEscapeString(it.Title))
	}
	return ""
}

// withoutName is an item's title less the name it starts with (any case) and the stop after it, so a row whose title
// repeats its name says it once: "Disk cleanup: free 20 GB on the build box" under that name is "free 20 GB on the build box".
func withoutName(title, name string) string {
	if name == "" || len(title) < len(name) || !strings.EqualFold(title[:len(name)], name) {
		return title
	}
	rest := title[len(name):]
	if r, _ := utf8.DecodeRuneInString(rest); rest != "" && (unicode.IsLetter(r) || unicode.IsDigit(r)) {
		return title // the name ends inside a word
	}
	return strings.TrimLeft(rest, " :,;·—–-")
}

// Search is what the header's filter matches a row on, lower-cased: its name, title (an item's in full), ticket,
// repo, PR (#n and owner/repo#n) and workers.
func (it Item) Search() string {
	parts := append([]string{it.Name, it.Title, it.Ticket, it.Repo}, it.finds...)
	if strings.HasPrefix(it.ID, "w-") {
		parts = append(parts, strings.TrimPrefix(it.ID, "w-"))
	}
	if it.PR != nil {
		parts = append(parts, it.PR.Text, prKey(it.PR.URL))
	}
	return strings.ToLower(strings.Join(strings.Fields(strings.Join(parts, " ")), " "))
}

// Search: an Awaiting review pill's, as a row's.
func (p Pill) Search() string {
	return strings.ToLower(strings.Join([]string{p.Title, p.Num, p.Key}, " "))
}

// workerRow: the row of the first of an item's workers that has one.
func workerRow(rows map[string]*Item, workers []string) *Item {
	for _, w := range workers {
		if row := rows["w-"+w]; row != nil {
			return row
		}
	}
	return nil
}

// mergedPRs: the PRs athena saw merged, by owner/name#n.
func mergedPRs(s Snapshot) map[string]bool {
	out := map[string]bool{}
	if s.Watch != nil {
		for _, ww := range s.Watch.Workers {
			if ww.PR != nil && ww.PR.State == "MERGED" {
				out[prKey(ww.PR.URL)] = true
			}
		}
		for _, p := range s.Watch.PRs {
			if p.State == "MERGED" {
				out[prKey(p.URL)] = true
			}
		}
	}
	for _, h := range s.History {
		if h.PR != nil && h.PR.State == "MERGED" {
			out[prKey(h.PR.URL)] = true
		}
	}
	return out
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

// moveMerges: the moves that make a PR the human's to merge, and `reviewing`, athena's own review before that. A live
// worker's PR shows one only once the worker reported DONE; until then its row shows the worker's own state
// (athena_lib/watch.py `_reported` agrees).
var moveMerges = map[string]bool{"approved": true, "mergeable": true, "reviewing": true}

// placeMove puts a PR's row where whose move it is says: Needs you when it is the human's, else In progress.
func placeMove(v *View, it Item, m *Move) {
	it.Label, it.Badge, it.moved = m.Label, m.Needs, true
	switch m.Kind {
	case "approved", "mergeable":
		it.Icon = "ready"
	case "queued":
		it.Icon = "ok"
	case "reviewing":
		it.Icon = "review"
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
