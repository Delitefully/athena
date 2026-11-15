"""The page's HTML, as the old athena dash's page.html drew it, template for template: the same markup, classes and ids, so its
style.css and app.js serve unchanged. Escaping follows Go's html/template, which the old page went through."""
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from athena.caps.dash.view import NEEDS_SHOWN

STATIC = Path(__file__).with_name("static")
LOCKUP = (STATIC / "lockup.svg").read_text()


def e(value) -> str:
    """As html/template escapes text and attribute values."""
    s = str(value if value is not None else "")
    return (s.replace("&", "&amp;").replace("'", "&#39;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;")
            .replace("+", "&#43;").replace("\x00", "�"))


_SAFE_SCHEME = re.compile(r"(?i)^(https?|mailto):")


def u(url) -> str:
    """A URL in an href or src, as html/template filters and normalizes one: an unsafe scheme is #ZgotmplZ."""
    s = str(url or "")
    if ":" in s.split("/", 1)[0] and not _SAFE_SCHEME.match(s):
        return "#ZgotmplZ"
    return e(quote(s, safe="!#$&'()*+,-./:;=?@[]_~%"))


def unix(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(math.floor(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def f0(ts: float) -> str:
    return "%.0f" % ts


def short(seconds: float) -> str:
    """A row's age: now, 10m, 2h, 7d (app.js short agrees)."""
    s = int(seconds)
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def ago(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def due_text(due: datetime, now: datetime) -> str:
    """overdue 2h, due in 40m, due 17:30 (today), due tmrw, due Thu, due 4 Nov (app.js dueText agrees)."""
    d = (due - now).total_seconds()
    if d < 0:
        return "overdue " + short(-d)
    if d < 3600:
        return f"due in {int(d // 60)}m"
    days = (due.date() - now.date()).days
    if days == 0:
        return "due " + due.strftime("%H:%M")
    if days == 1:
        return "due tmrw"
    if days < 7:
        return "due " + due.strftime("%a")
    return f"due {due.day} {due.strftime('%b')}"


def at(t: datetime, now: datetime) -> str:
    """When a default applies: 09:00 today, tmrw 09:00, or Thu 09:00."""
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if t - now < timedelta(hours=48) and t.day == (now + timedelta(days=1)).day:
        return "tmrw " + t.strftime("%H:%M")
    return t.strftime("%a %H:%M")


CHECKS = {"success": "green", "failure": "failing", "pending": "running"}
BADGES = {"blocked", "fail", "done", "ready"}


class Page:
    """Renders with one clock, as the old templates did."""

    def __init__(self, now: float):
        self.now = now

    def short(self, ts):
        return short(self.now - ts)

    def ago(self, ts):
        return ago(self.now - ts)

    def due(self, ts):
        return due_text(unix(ts), unix(self.now))

    def at(self, ts):
        return at(unix(ts), unix(self.now))

    def auto_note(self, items) -> str:
        first = 0.0
        for it in items:
            if it.auto_at > 0 and (first == 0 or it.auto_at < first):
                first = it.auto_at
        return "athena takes the rec at " + self.at(first) + " unless you object" if first else ""

    # Pages

    def head(self, title, scheme, token, boot, q=None, here="") -> str:
        return ("<!doctype html>\n"
                f'<html lang="en"{f" data-scheme={chr(34)}{e(scheme)}{chr(34)}" if scheme else ""}>\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="color-scheme" content="light dark">\n'
                f'<meta name="athena-token" content="{e(token)}">\n<meta name="athena-boot" content="{e(boot)}">\n<title>{e(title)}</title>\n'
                '<link rel="icon" href="/static/mark.svg" type="image/svg+xml">\n'
                '<link rel="preload" href="/static/InstrumentSans.woff2" as="font" type="font/woff2" crossorigin>\n'
                '<link rel="preload" href="/static/InstrumentSerif-Regular.woff2" as="font" type="font/woff2" crossorigin>\n'
                f'<link rel="stylesheet" href="/static/style.css?v={e(boot)}">\n</head>\n<body>\n{SPRITE}\n<header>\n'
                f'<a class="lockup" href="/" aria-label="athena">{LOCKUP}</a>\n{nav(here)}<span class="health-slot"></span>\n'
                '<p class="status" role="status">reconnecting…</p>\n'
                '<form class="search" action="/items" role="search"><input type="search" name="q" placeholder="Search items" '
                f'aria-label="Search items" autocomplete="off"{f" value={chr(34)}{e(q)}{chr(34)}" if q else ""}></form>\n</header>')

    @staticmethod
    def foot(boot) -> str:
        return ('<div class="tip" aria-hidden="true" hidden></div>\n'
                '<div class="toast" role="status" aria-live="polite" hidden><span class="toast-text"></span>'
                '<button type="button" class="undo">Undo</button></div>\n'
                f'<script src="/static/app.js?v={e(boot)}"></script>\n</body>\n</html>')

    def page(self, view, scheme, token, boot) -> str:
        return f"{self.head(view.title, scheme, token, boot)}\n<main>{self.main(view)}</main>\n{self.foot(boot)}"

    def item_page(self, fv, scheme, token, boot) -> str:
        return (f"{self.head(fv.ask.name + ' · athena', scheme, token, boot)}\n"
                f'<main class="full" data-item="{e(fv.ask.id)}">{self.item_main(fv)}</main>\n{self.foot(boot)}')

    def search_page(self, q, hits, scheme, token, boot) -> str:
        if q:
            h2 = f'Items matching “{e(q)}” <span class="count">{len(hits)}</span>'
        else:
            h2 = "Search items"
        if hits:
            body = "<ul>" + "".join(
                f'<li class="item has-view"><div class="body"><p class="line"><span class="name"><a href="/item/{e(h.id)}">{e(h.name)}</a>'
                f'</span> <span class="call">{h.call}</span></p><p class="meta"><span class="label">{e(h.kind)}</span><span>{e(h.state)}'
                f'</span></p></div></li>' for h in hits) + "</ul>"
        elif q:
            body = '<p class="empty">No item matches.</p>'
        else:
            body = '<p class="empty">Names, titles, your words and how each resolved, open or done.</p>'
        return (f"{self.head('Search · athena', scheme, token, boot, q)}\n"
                f'<main class="full"><p class="back"><a href="/">← all items</a></p>\n<section class="group"><h2>{h2}</h2>\n{body}\n'
                f"</section></main>\n{self.foot(boot)}")

    # The main page's <main>

    def main(self, v) -> str:
        out = [f'<span hidden data-title="{e(v.title)}"></span>\n', self.health(v.health), '\n<div class="col yours">\n', self.needs(v), "\n"]
        if v.ready:
            out.append(self.group("ready", "Ready for you", v.ready, ""))
        out.append('\n</div>\n<div class="col others">\n')
        if v.awaiting:
            out.append(self.awaiting(v))
        out.append("\n" + self.group("progress", "In flight", v.in_progress, "Nothing in flight.") + "\n")
        if v.done:
            out.append('<details class="group done" data-fold="done" aria-labelledby="h-done"><summary><h2 id="h-done">Done today '
                       f'<span class="count">{len(v.done)}</span></h2></summary>\n<ul>{"".join(self.item(it) for it in v.done)}</ul></details>')
        out.append("\n")
        if v.sessions:
            out.append(self.group("sessions", "Rex sessions", v.sessions, ""))
        out.append("\n")
        if v.snoozed:
            out.append(f'<p class="snoozed">{v.snoozed} snoozed: each comes back when its time is up, or sooner if something happens to it.</p>')
        out.append("\n</div>\n")
        if v.notices:
            out.append('<aside class="notices">' + "".join(f"<p>{e(n)}</p>" for n in v.notices) + "</aside>")
        out.append("\n")
        return "".join(out)

    @staticmethod
    def health(h) -> str:
        return f'<p class="health {e(h.state)}" title="{e(h.title)}" role="status"><span class="dot" aria-hidden="true"></span>{e(h.text)}</p>'

    def needs(self, v) -> str:
        out = [f'<section class="group needs" aria-labelledby="h-needs">\n<h2 id="h-needs">Needs you <span class="count">{len(v.needs_you)}</span></h2>\n']
        if v.needs_you:
            out.append("<ul>" + "".join(self.item(it) for it in v.needs_you[:NEEDS_SHOWN]) + "</ul>\n")
            rest = v.needs_you[NEEDS_SHOWN:]
            if rest:
                note = self.auto_note(rest)
                out.append(f'<details class="more" data-fold="needs"><summary>{len(rest)} more{" · " + e(note) if note else ""}</summary>'
                           f'<ul>{"".join(self.item(it) for it in rest)}</ul></details>')
            out.append("\n")
        else:
            out.append('<p class="empty">Nothing waits on you.</p>\n')
        out.append("</section>")
        return "".join(out)

    def group(self, key, name, items, empty) -> str:
        body = f'<ul>{"".join(self.item(it) for it in items)}</ul>' if items else f'<p class="empty">{e(empty)}</p>'
        return (f'<section class="group {e(key)}" aria-labelledby="h-{e(key)}">\n<h2 id="h-{e(key)}">{e(name)} '
                f'<span class="count">{len(items)}</span></h2>\n{body}\n</section>')

    def item(self, it) -> str:
        cls = "item" + (" act" if it.badge else "") + (" has-view" if it.href else "")
        icon_cls = "icon" + (" badge" if it.icon in BADGES else "")
        name = f'<a href="{u(it.href)}">{e(it.lead())}</a>' if it.href else e(it.lead())
        second = it.second()
        line = f'<p class="line"><span class="name">{name}</span>' + (f' <span class="call">{second}</span>' if second else "")
        if it.due:
            line += f'<time class="when due" data-due="{f0(it.due)}" datetime="{iso(it.due)}">{e(self.due(it.due))}</time>'
        elif it.when:
            line += f'<time class="when" data-ts="{f0(it.when)}" data-short datetime="{iso(it.when)}">{e(self.short(it.when))}</time>'
        line += "</p>"
        swap = "swap"
        if it.actions is not None and it.actions.on_row():
            swap += " has-acts" + (" keep" if it.actions.for_pr else "")
        elif it.session:
            swap += " has-acts"
        meta = f'<p class="meta"><span class="label">{e(it.label)}</span>'
        if it.auto_at:
            meta += f'<span class="auto">athena takes the rec at {e(self.at(it.auto_at))}</span>'
        if it.sent:
            meta += f'<span class="sent">{e(it.sent)}</span>'
        if it.ticket_url:
            meta += f'<a class="ticket" href="{u(it.ticket_url)}">{e(it.ticket)}</a>'
        elif it.ticket:
            meta += f'<span class="ticket">{e(it.ticket)}</span>'
        if it.repo:
            meta += f"<span>{e(it.repo)}</span>"
        if it.agent:
            meta += f'<span class="agent">{e(it.agent)}</span>'
        if it.pr is not None:
            p = it.pr
            meta += f'<span class="pr"><a href="{u(p.url)}">{e(p.text)}</a>'
            if p.checks:
                meta += f' <span class="checks {e(p.checks)}">checks {e(CHECKS.get(p.checks, ""))}</span>'
            if p.review:
                meta += f" <span>{e(p.review)}</span>"
            if p.stale_since:
                meta += (f' <span class="stale">as of <time data-ts="{f0(p.stale_since)}" datetime="{iso(p.stale_since)}">'
                         f"{e(self.ago(p.stale_since))}</time></span>")
            meta += "</span>"
        meta += "</p>"
        acts = self.actions(it.actions)
        if it.session:
            acts = (f'<div class="acts" data-session="{e(it.session)}"><button type="button" class="pill" data-show-session '
                    f'aria-label="Show it in Rex"><svg class="pi" aria-hidden="true"><use href="#i-external-link"/></svg>Show</button></div>')
        detail = f'<p class="detail" title="{e(it.detail)}">{e(it.detail)}</p>' if it.detail else ""
        return (f'<li id="{e(it.id)}" class="{cls}" data-search="{e(it.search())}">\n'
                f'<svg class="{icon_cls}" viewBox="-12 -12 24 24" aria-hidden="true"><use href="#i-{e(it.icon)}"/></svg>\n'
                f'<div class="body">\n{line}\n<div class="{swap}">{meta}{acts}</div>{detail}\n</div>\n</li>')

    def awaiting(self, v) -> str:
        out = [f'<section class="awaiting" aria-labelledby="h-awaiting">\n<h2 id="h-awaiting">Awaiting review <span class="count">'
               f'{v.awaiting_count}</span></h2>\n<ul class="aprs">']
        for p in v.awaiting:
            cls = f' class="stack{" deep" if len(p.behind) > 1 else ""}"' if p.behind else ""
            out.append(f"<li{cls}>{self.apr(p)}")
            if p.behind:
                out.append(f'<ul class="behind" aria-label="Stacked on {e(p.num)}">' +
                           "".join(f"<li>{self.apr(b)}</li>" for b in p.behind) + "</ul>")
            out.append("</li>")
        out.append("</ul>\n</section>")
        return "".join(out)

    @staticmethod
    def apr(p) -> str:
        label = f"{p.title}, {p.num}, {p.label}"
        if p.waits_on:
            label += f", waits on {p.waits_on}"
        if p.behind:
            label += f", {len(p.behind)} stacked on it"
        if p.rechecking:
            label += ", re-checking"
        meta = f'<span class="apr-num">{e(p.num)}</span>'
        if p.posted:
            meta += '<span class="apr-posted">posted in #open-prs</span>'
        if p.waits_on:
            meta += f'<span class="apr-waits">waits on {e(p.waits_on)}</span>'
        if p.behind:
            meta += f'<span class="apr-more">+{len(p.behind)}</span>'
        acts = ""
        a = p.actions
        if a is not None:
            acts = f'<div class="acts" data-item="{e(a.item)}">'
            if a.recheck:
                acts += ('<button type="button" class="pill recheck" data-option="recheck" aria-label="Re-check and fix">'
                         '<svg class="pi" aria-hidden="true"><use href="#i-rotate-ccw"/></svg></button>')
            if a.dismiss:
                acts += ('<button type="button" class="pill dismiss" data-option="dismiss" data-hides aria-label="Dismiss">'
                         '<svg class="pi" aria-hidden="true"><use href="#i-x"/></svg></button>')
            acts += "</div>"
        return (f'<div class="apr{" rechecking" if p.rechecking else ""}" id="{e(p.id)}" data-search="{e(p.search())}">\n'
                f'<a class="apr-link" href="{u(p.url)}" title="{e(p.title)}&#10;{e(p.label)}" aria-label="{e(label)}">{e(p.short)}</a>\n'
                f'<div class="swap has-acts"><span class="apr-meta" aria-hidden="true">{meta}</span>{acts}</div>\n</div>')

    @staticmethod
    def pi(name) -> str:
        return f'<svg class="pi" aria-hidden="true"><use href="#i-{e(name)}"/></svg>'

    def actions(self, a) -> str:
        """A row's pills: they take the place of its label line on hover and focus."""
        if a is None or not a.on_row():
            return ""
        options = " options" if a.row_options() or a.for_pr or a.open else ""
        out = [f'<div class="acts{options}" data-item="{e(a.item)}"{" data-hides" if a.hides else ""}>']
        for o in a.row_options():
            out.append(f'<button type="button" class="pill" data-option="{e(o.id)}">{self.pi(o.icon) if o.icon else ""}{e(o.label)}</button>')
        if a.take_rec is not None:
            out.append(f'<button type="button" class="pill rec" data-option="{e(a.take_rec.id)}" data-tip="Recommended: {e(a.take_rec.label)}">'
                       f'{self.pi("check")}Take rec</button>')
        if a.undo:
            out.append(f'<button type="button" class="pill" data-option="undo-default" data-tip="Undo {e(a.undo)}: athena takes it back">'
                       f'{self.pi("rotate-ccw")}Undo</button>')
        if a.done:
            out.append(f'<button type="button" class="pill" data-do="done">{self.pi("check")}Done</button>')
        if a.snooze:
            out.append(f'<button type="button" class="pill" data-do="snooze" data-tip="Until tomorrow 09:00">{self.pi("clock")}Snooze</button>')
        if a.post_open_prs:
            out.append(f'<button type="button" class="pill" data-option="post-open-prs" aria-label="Post to #open-prs">{self.pi("send")}'
                       "Post to #open-prs</button>")
        if a.request_review:
            out.append(f'<button type="button" class="pill" data-option="request-review" aria-label="Request review">{self.pi("eye")}'
                       "Request review</button>")
        if a.recheck:
            out.append(f'<button type="button" class="pill" data-option="recheck" aria-label="Re-check and fix">{self.pi("rotate-ccw")}'
                       "Re-check</button>")
        if a.open_pr:
            out.append(f'<a class="pill" href="{u(a.open_pr)}" aria-label="Open PR {e(a.num)} on GitHub">{self.pi("git-pull-request")}Open PR</a>')
        if a.go:
            out.append(f'<button type="button" class="pill" data-option="go" data-head="{e(a.head)}" aria-label="Merge: tell athena go">'
                       f'{self.pi("git-merge")}Merge</button>')
        if a.dismiss:
            out.append(f'<button type="button" class="pill dismiss" data-option="dismiss" aria-label="Dismiss">{self.pi("x")}</button>')
        if not a.open:
            if a.discuss:
                out.append(f'<button type="button" class="pill" data-discuss aria-label="Discuss in Athena">{self.pi("message-circle")}'
                           '<span class="lbl">Discuss in Athena</span></button>')
            if a.reply:
                out.append(f'<button type="button" class="pill" data-reply aria-label="Reply…">{self.pi("send")}<span class="lbl">Reply…</span></button>')
        out.append("</div>")
        return "".join(out)

    def bar(self, a) -> str:
        """The full view's answers: every option with its label, the rec marked, and Don't ask me this again."""
        if a is None:
            return ""
        out = [f'<div class="acts bar" data-item="{e(a.item)}"{" data-hides" if a.hides else ""}>']
        for o in a.options:
            rec = a.take_rec is not None and o.id == a.take_rec.id
            out.append(f'<button type="button" class="pill{" rec" if rec else ""}" data-option="{e(o.id)}">{self.pi(o.icon) if o.icon else ""}'
                       f'{e(o.label)}{" <span class=" + chr(34) + "tag" + chr(34) + ">rec</span>" if rec else ""}</button>')
        if a.undo:
            out.append(f'<button type="button" class="pill" data-option="undo-default">{self.pi("rotate-ccw")}Undo {e(a.undo)}</button>')
        if a.dismiss:
            out.append(f'<button type="button" class="pill" data-option="dismiss">{self.pi("x")}Dismiss</button>')
        if a.discuss:
            out.append(f'<button type="button" class="pill" data-discuss>{self.pi("message-circle")}Discuss in Athena</button>')
        if a.reply:
            out.append(f'<button type="button" class="pill" data-reply>{self.pi("send")}Reply…</button>')
        if a.rule:
            out.append("<label class=\"rule\"><input type=\"checkbox\" name=\"rule\"> Don't ask me this again</label>")
        out.append("</div>")
        return "".join(out)

    def item_main(self, fv) -> str:
        a = fv.ask
        facts = (f'<p class="facts"><span>{e(fv.kind)}</span><span class="state">{e(fv.state)}</span><span>filed '
                 f'<time data-ts="{f0(a.created)}" datetime="{iso(a.created)}">{e(self.ago(a.created))}</time></span>')
        if a.due:
            facts += f'<span class="due"><time data-due="{f0(a.due)}" datetime="{iso(a.due)}">{e(self.due(a.due))}</time></span>'
        if a.stakes:
            facts += f'<span class="stakes">{", ".join(e(s) for s in a.stakes)}: always your call</span>'
        facts += "</p>"
        out = [f'<span hidden data-title="{e(a.name)} · athena"></span>\n<p class="back"><a href="/">← all items</a></p>\n<article class="view">\n'
               f"<h1>{e(a.name)}</h1>\n{facts}\n"]
        if a.words:
            out.append(f'<section><h2>You asked</h2><blockquote class="words">{e(a.words)}</blockquote></section>')
        out.append(f'\n<section><h2>Where it stands</h2><p class="call">{fv.call}</p>{f"<p>{e(a.why)}</p>" if a.why else ""}</section>\n')
        if fv.actions is not None:
            if a.kind == "decision":
                out.append("<section class=\"brief\"><h2>What's needed from you</h2>\n")
                if fv.body:
                    out.append(f'<div class="md">{fv.body}</div>')
                out.append("\n")
                if fv.auto_at:
                    out.append(f'<p class="auto">athena takes the rec at {e(self.at(fv.auto_at))} unless you object.</p>')
                out.append(f"\n{self.bar(fv.actions)}</section>\n")
            else:
                if fv.body:
                    out.append(f'<section><h2>Report</h2><div class="md">{fv.body}</div></section>')
                out.append("\n")
                if fv.actions.undo:
                    out.append(f'<section class="brief">{self.bar(fv.actions)}</section>')
        elif fv.body:
            out.append(f'<section><h2>{"The brief" if a.kind == "decision" else "Report"}</h2><div class="md">{fv.body}</div></section>')
        out.append("\n")
        if fv.previews:
            out.append("<section><h2>Previews</h2>" + "".join(
                f'<figure class="preview"><iframe src="{u(p.url)}" sandbox="allow-scripts" loading="lazy" title="{e(p.name)}"></iframe>'
                f'<figcaption><a class="pill" href="{u(p.url)}" target="_blank" rel="noopener noreferrer" data-tip="Opens in a tab of its own, '
                f'sandboxed: the preview can run its script but reach nothing of the dash">{self.pi("external-link")}{e(p.name)}</a></figcaption></figure>'
                for p in fv.previews) + "</section>")
        out.append("\n")
        if fv.workers or fv.pr or a.links:
            li = []
            for w in fv.workers:
                t = f'<li><span class="name">{e(w.title)}</span> <span class="label">{e(w.label)}</span>'
                if w.ticket_url:
                    t += f' <a href="{u(w.ticket_url)}">{e(w.ticket)}</a>'
                elif w.ticket:
                    t += f" {e(w.ticket)}"
                if w.pr is not None:
                    t += f' <a href="{u(w.pr.url)}">{e(w.pr.text)}</a>'
                li.append(t + "</li>")
            if fv.pr is not None:
                li.append(f'<li><a href="{u(fv.pr.url)}">{e(fv.pr.text)}</a></li>')
            for link in a.links:
                li.append(f'<li><a href="{u(link.url)}">{e(link.title)}</a></li>')
            out.append('<section><h2>Workers and links</h2><ul class="links">' + "".join(li) + "\n</ul></section>")
        out.append("\n")
        tl = "".join(f'<li><time data-ts="{f0(t.ts)}" datetime="{iso(t.ts)}">{e(self.ago(t.ts))}</time> {e(t.text)}</li>' for t in fv.timeline)
        out.append(f'<section><h2>Timeline</h2><ol class="timeline">{tl or "<li>Nothing yet.</li>"}</ol></section>\n')
        acts = ""
        if fv.done:
            acts += f'<button type="button" class="pill" data-do="done">{self.pi("check")}Done</button>'
        if fv.snooze:
            acts += ('<button type="button" class="pill" data-do="snooze" data-for="7200">Snooze 2 h</button>'
                     '<button type="button" class="pill" data-do="snooze" data-for="tomorrow">Until tomorrow</button>'
                     '<button type="button" class="pill" data-do="snooze" data-for="monday">Until Monday</button>')
        if fv.wake:
            acts += '<button type="button" class="pill" data-do="wake">Bring it back now</button>'
        if fv.reopen:
            acts += f'<button type="button" class="pill" data-do="reopen">{self.pi("rotate-ccw")}Reopen</button>'
        out.append(f'<div class="item-acts" data-item="{e(a.id)}">{acts}</div>\n</article>')
        return "".join(out)

    @staticmethod
    def glyphs(scheme) -> str:
        rows = "".join(
            f'<li class="item{" note" if note else ""}">\n<svg class="icon{" badge" if icon in BADGES else ""}" viewBox="-12 -12 24 24" '
            f'aria-hidden="true"><use href="#i-{icon}"/></svg>\n<div class="body"><p class="{"note-text" if note else "line"}"><span class="ticket">'
            f'{icon}</span> <span class="title">{e(text)}</span></p></div>\n</li>' for icon, text, note in GLYPHS)
        return ("<!doctype html>\n"
                f'<html lang="en"{f" data-scheme={chr(34)}{e(scheme)}{chr(34)}" if scheme else ""}>\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="color-scheme" content="light dark">\n'
                '<title>athena glyphs</title>\n<link rel="stylesheet" href="/static/style.css">\n</head>\n<body>\n'
                f'{SPRITE}\n<main class="sheet"><section class="group">\n<h2>Row glyphs <span class="count">{len(GLYPHS)}</span></h2>\n'
                f"<ul>{rows}</ul>\n</section></main>\n</body>\n</html>")


GLYPHS = [("blocked", "blocked, a worker waits on you", False), ("fail", "CI failed", False),
          ("ask", "athena asks: merge now or after the release?", True), ("note", "a note from athena", True),
          ("done", "done, or your review requested", False), ("ready", "ready to merge", False), ("review", "review comments", False),
          ("ok", "checks green", False), ("working", "working", False), ("pr", "PR open", False), ("idle", "idle", False),
          ("exited", "exited", False), ("merged", "merged", False), ("retired", "retired", False)]

SPRITE = (STATIC.parent / "sprite.svg").read_text().strip()


def nav(here="") -> str:
    """The links to the dash's other pages, beside the lockup: Flame, while its capability is on."""
    from athena.kernel import caps
    if not caps.get("flame"):
        return ""
    current = ' aria-current="page"' if here == "flame" else ""
    return f'<nav class="pages" aria-label="Pages"><a href="/flame"{current}>Flame</a></nav>\n'
