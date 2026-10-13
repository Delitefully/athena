package main

import (
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// shortMax: the most of a PR's title a pill on the Awaiting review row shows; the tooltip has the rest.
const shortMax = 48

// prKeyText is an owner/name#n as prKey gives it; pr: and that is a pill's action item.
var prKeyText = regexp.MustCompile(`^[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}#[1-9][0-9]{0,9}$`)

const pillItem = "pr:"

// stackAwaiting builds the Awaiting review row. Every PR that waits on a teammate's review is a pill there, unless the
// human dismissed it as it is now. A PR stacked on one of them (its base is that PR's head branch, or it comes later
// in a stack.json chain) goes behind it, in chain order, when it would otherwise be an In progress row placed by its
// PR's state: it then leaves In progress, so each PR shows in one place. A PR that needs the human, or whose live
// worker's own state shows, keeps its row. queued: when each item's oldest queued answer was given.
func stackAwaiting(v *View, stacks []Stack, dismissed map[string]Dismissal, queued map[string]float64, now float64) {
	type node struct {
		it       *Item
		key      string
		awaiting bool
	}
	nodes := map[string]*node{}
	hidden := func(it *Item) bool {
		d, ok := dismissed[prKey(it.pr.URL)]
		return ok && d.Head == it.pr.Head && d.Review == it.Label
	}
	for i := range v.awaiting {
		it := &v.awaiting[i]
		if hidden(it) {
			continue // dismissed: off the row until its head or review state changes
		}
		nodes[prKey(it.pr.URL)] = &node{it, prKey(it.pr.URL), true}
	}
	if len(nodes) == 0 {
		return
	}
	for i := range v.InProgress {
		it := &v.InProgress[i]
		if it.moved && it.pr != nil && prKey(it.pr.URL) != "" && nodes[prKey(it.pr.URL)] == nil {
			nodes[prKey(it.pr.URL)] = &node{it, prKey(it.pr.URL), false}
		}
	}

	// Who each PR is stacked on: stack.json's chain first, else the open PR whose head branch is its base.
	parent := map[string]string{}
	rank := map[string]int{} // place in its chain
	for _, c := range stacks {
		for i, n := range c.PRs {
			k := c.Repo + "#" + strconv.Itoa(n)
			rank[k] = i
			if i > 0 {
				parent[k] = c.Repo + "#" + strconv.Itoa(c.PRs[i-1])
			}
		}
	}
	heads := map[string]string{} // owner/name|branch -> key
	for k, n := range nodes {
		if b := branchOf(n.it); b != "" {
			heads[repoOf(k)+"|"+b] = k
		}
	}
	for k, n := range nodes {
		if _, ok := parent[k]; !ok && n.it.pr.Base != "" {
			if p, ok := heads[repoOf(k)+"|"+n.it.pr.Base]; ok && p != k {
				parent[k] = p
			}
		}
	}

	// front: the pill a PR shows on, or "" for none. A PR goes behind the pill its parent shows on; else an awaiting
	// PR is a pill of its own.
	front := map[string]string{}
	var frontOf func(k string, seen map[string]bool) string
	frontOf = func(k string, seen map[string]bool) string {
		if f, ok := front[k]; ok {
			return f
		}
		f := ""
		if p := parent[k]; p != "" && nodes[p] != nil && !seen[p] {
			seen[k] = true
			f = frontOf(p, seen)
		}
		if f == "" && nodes[k].awaiting {
			f = k
		}
		front[k] = f
		return f
	}
	keys := make([]string, 0, len(nodes))
	for k := range nodes {
		keys = append(keys, k)
	}
	sort.Strings(keys) // so a cycle in bad data breaks the same way every time
	for _, k := range keys {
		frontOf(k, map[string]bool{})
	}

	children := map[string][]string{}
	for k := range nodes {
		if p := parent[k]; p != "" && front[k] != "" && front[k] != k {
			children[p] = append(children[p], k)
		}
	}
	for _, kids := range children {
		sort.Slice(kids, func(i, j int) bool { return pillLess(kids[i], kids[j], rank) })
	}

	v.pills = map[string]*Pill{}
	pill := func(n *node, waitsOn string) Pill {
		p := newPill(n.it, n.key)
		if waitsOn != "" {
			p.WaitsOn = "#" + strconv.Itoa(prNumberOf(waitsOn))
		}
		if ts, ok := queued[p.Actions.Item]; ok && now-ts < stuckAfter {
			p.Rechecking, p.Actions.Recheck = true, false
		}
		return p
	}
	stacked := map[*Item]bool{}
	var fronts []string
	for k, f := range front {
		if f == k {
			fronts = append(fronts, k)
		}
	}
	sort.Slice(fronts, func(i, j int) bool { return pillLess(fronts[i], fronts[j], rank) })
	for _, k := range fronts {
		p := pill(nodes[k], "")
		var walk func(k string)
		walk = func(k string) {
			for _, c := range children[k] {
				stacked[nodes[c].it] = true
				if !hidden(nodes[c].it) {
					p.Behind = append(p.Behind, pill(nodes[c], k))
				}
				walk(c)
			}
		}
		walk(k)
		v.Awaiting = append(v.Awaiting, p)
	}
	for i := range v.Awaiting {
		f := &v.Awaiting[i]
		v.pills[f.Actions.Item] = f
		for j := range f.Behind {
			v.pills[f.Behind[j].Actions.Item] = &f.Behind[j]
		}
	}
	if len(stacked) > 0 {
		kept := v.InProgress[:0:0]
		for i := range v.InProgress {
			if !stacked[&v.InProgress[i]] {
				kept = append(kept, v.InProgress[i])
			}
		}
		v.InProgress = kept
	}
}

func newPill(it *Item, key string) Pill {
	title := strings.TrimSpace(it.pr.Title)
	if title == "" {
		title = it.Title
	}
	if title == "" {
		title = repoName(key)
	}
	n := strconv.Itoa(prNumberOf(key))
	return Pill{ID: "aw-" + slug(repoOf(key)) + "-" + n, Key: key, URL: it.pr.URL, Title: title, Short: shortTitle(title),
		Num: "#" + n, Label: it.Label, head: it.pr.Head,
		Actions: &Actions{Item: pillItem + key, Recheck: true, Dismiss: true}}
}

// shortTitle cuts a title past shortMax runes at a word boundary near the end, with an ellipsis.
func shortTitle(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= shortMax {
		return s
	}
	cut := string(r[:shortMax-1])
	if i := strings.LastIndex(cut, " "); i >= 0 && len([]rune(cut[:i])) >= shortMax-12 {
		cut = cut[:i]
	}
	return strings.TrimRight(cut, " .,:;-–—·") + "…"
}

func branchOf(it *Item) string {
	if it.pr.Branch != "" {
		return it.pr.Branch
	}
	return it.branch
}

func repoOf(key string) string {
	repo, _, _ := strings.Cut(key, "#")
	return repo
}

func prNumberOf(key string) int {
	_, n, _ := strings.Cut(key, "#")
	i, _ := strconv.Atoi(n)
	return i
}

// pillLess orders pills by repo, then place in a stack.json chain, then number.
func pillLess(a, b string, rank map[string]int) bool {
	if ra, rb := repoOf(a), repoOf(b); ra != rb {
		return ra < rb
	}
	ia, oka := rank[a]
	ib, okb := rank[b]
	if oka && okb && ia != ib {
		return ia < ib
	}
	return prNumberOf(a) < prNumberOf(b)
}
