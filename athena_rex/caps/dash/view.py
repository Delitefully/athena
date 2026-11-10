"""Fold the state files into the page's sections. Pure over what it reads, and a bad file only empties its own source."""
import time
from typing import NamedTuple, Optional

from athena_rex.kernel import caps, config, ledger, store

SHOW_MAX = 7
PR_STALE = 600
DAY = 86400
STUCK_AFTER = 600
WORKER_LABEL = {"blocked": "blocked", "idle-unreported": "stopped without a report", "exited": "its Claude is gone",
                "done": "done", "limit": "parked at a usage limit", "working": "working", "idle": "idle", "starting": "starting", "unknown": "no status yet"}


class Row(NamedTuple):
    id: str
    name: str
    text: str
    label: str
    ts: float
    link: Optional[str] = None
    mark: str = ""
    actions: tuple = ()
    due: Optional[float] = None
    key: str = ""  # what its answers name: an item's id, `w-<worker>` or `pr:<owner/name#n>`
    sent: str = ""  # an answer waits for athena: "merge go sent", or "athena hasn't picked this up yet"


class Actions(dict):
    """What a row or an item's full view lets the human answer, as the old dash's Actions: options, take_rec, discuss, reply, rule,
    recheck, request_review, go (with head), post_open_prs, undo (the label of a default athena applied), done, snooze, hides."""

    def allows(self, option=None, text=None, discuss=False) -> bool:
        if discuss:
            return bool(self.get("discuss"))
        if text:
            return bool(self.get("reply"))
        special = {"recheck": "recheck", "request-review": "request_review", "go": "go", "post-open-prs": "post_open_prs"}
        if option in special:
            return bool(self.get(special[option]))
        if option == "undo-default":
            return bool(self.get("undo"))
        return option in (self.get("options") or ())


STUCK_WHY = "athena hasn't picked this up yet"
SENT_TEXT = {"recheck": "re-check sent", "request-review": "review request sent", "go": "merge go sent", "post-open-prs": "posting…"}


def _age(ts, now) -> str:
    seconds = max(0, now - float(ts or now))
    for span, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= span:
            return f"{int(seconds // span)}{unit}"
    return "now"


age = _age


def _title_without_name(item) -> str:
    text, name = str(item.get("text") or ""), str(item.get("name") or "")
    if name and text.lower().startswith(name.lower()):
        return text[len(name):].lstrip(" :·-—") or text
    return text


def _item_row(item, now, acts, label=None, sent="") -> Row:
    buttons = []
    if acts.get("take_rec"):
        rec = next((o for o in item.get("options") or [] if o.get("id") == item.get("rec")), {"label": item.get("rec")})
        buttons.append(("option", item["rec"], "Take rec"))
    if acts.get("undo"):
        buttons.append(("option", "undo-default", f"Undo {acts['undo']}"))
    if acts.get("done"):
        buttons.append(("done", "", "Done"))
    if acts.get("snooze"):
        buttons.append(("snooze", "", "Snooze"))
    if item.get("permission"):
        buttons = [("option", "approve", "Approve"), ("option", "deny", "Deny")] + buttons
    if label is None:
        label = " · ".join(str(x) for x in (_kind_label(item), item.get("pr")) if x)
        if item.get("auto") and item.get("rec"):
            label += " · athena takes the rec on its own"
    return Row(item["id"], str(item.get("name") or "Untitled item"), _title_without_name(item), label,
               float(item.get("updated") or item.get("created") or now), link=f"/item/{item['id']}",
               mark="decision" if item.get("kind") == "decision" else "", actions=tuple(buttons), key=item["id"], sent=sent)


def _kind_label(item) -> str:
    kind, nxt = item.get("kind"), item.get("next")
    if kind == "decision" and nxt == "you":
        return "your call"
    if kind in ("result", "incident"):
        return kind
    if kind == "promise":
        return "athena promised"
    return "your move" if nxt == "you" else f"{nxt}'s move"


def pr_actions(key, move, url, head, open_prs) -> Actions:
    """A Needs you PR row's buttons, by why it needs the human, as the old dash's prActions: Discuss on each; Request review (and Post to
    #open-prs once that lane is set up) with no reviewer requested; Re-check when checks fail; Merge, a go at this head, where athena
    merges and the page can name the head. Open PR is a plain link."""
    acts = Actions(item=f"pr:{key}", discuss=True, for_pr=True)
    kind = move.get("kind")
    if kind == "unrequested":
        acts.update(request_review=True, post_open_prs=open_prs)
    elif kind == "failing":
        acts["recheck"] = True
    elif kind in ("approved", "mergeable"):
        acts["open_pr"] = url
        if not move.get("human_merge") and head:
            acts.update(go=True, head=head)
    elif kind == "changes":
        acts["open_pr"] = url
    else:
        acts.update(for_pr=False, reply=True)
    return acts


def _pr_buttons(acts) -> tuple:
    out = []
    if acts.get("post_open_prs"):
        out.append(("option", "post-open-prs", "Post to #open-prs"))
    if acts.get("request_review"):
        out.append(("option", "request-review", "Request review"))
    if acts.get("recheck"):
        out.append(("option", "recheck", "Re-check"))
    if acts.get("open_pr"):
        out.append(("link", acts["open_pr"], "Open PR"))
    if acts.get("go"):
        out.append(("go", acts["head"], "Merge"))
    if acts.get("discuss"):
        out.append(("discuss", "", "Discuss"))
    if acts.get("reply"):
        out.append(("reply", "", "Reply…"))
    return tuple(out)


def state_label(state, report) -> str:
    """The old dash's stateLabel."""
    if state == "done":
        return "done with concerns, athena reviewing" if (report or {}).get("status") == "DONE_WITH_CONCERNS" else "done, athena reviewing"
    if state in ("", "unknown", "starting"):
        return "starting"
    return {"idle-unreported": "stopped without a report", "exited": "its Claude exited",
            "limit": "parked at a usage limit"}.get(state, state)


def _worker_row(r, acts=None, sent="", label=None, detail=None) -> Row:
    report = r.get("report") or {}
    session = r.get("session") or {}
    label = label or state_label(r["state"], report)
    if session.get("id"):
        label += f' · session “{session.get("label") or r["name"]}”' + (" gone" if session.get("seen") is False else "")
    if detail is None:
        detail = r.get("summary") or (report.get("concerns") or [""])[0] or r.get("branch") or ""
    acts = acts if acts is not None else Actions(item=f"w-{r['name']}", discuss=True, reply=True)
    buttons = _pr_buttons(acts)
    return Row(f"w-{r['name']}", r.get("title") or r["name"], " ".join(str(detail).split())[:200], label, float(r.get("hook_ts") or 0),
               link=(r.get("pr") or {}).get("url"), mark="blocked" if r["state"] == "blocked" or r.get("pending") else "",
               actions=buttons, key=acts["item"], sent=sent)


SESSION_KIND = {"hq": "the chief of staff", "orphan": "left behind", "other": "not athena's"}


def _session_rows(now, notes, by_name) -> list:
    sessions = caps.get("sessions")
    seen = sessions.snapshot(now) if sessions else {}
    if sessions and seen.get("down"):
        notes.append(f"Rex didn't answer ({seen['down']}), so no sessions are listed")
    elif sessions and not seen:
        notes.append("Rex sessions: no recent look yet")
    out = []
    for s in sorted(seen.get("sessions") or [], key=lambda s: ("hq", "worker", "orphan", "other").index(s["kind"])):
        state = (by_name.get(s.get("owner")) or {}).get("state")
        text = f"{s['owner']} · {WORKER_LABEL.get(state, state)}" if s["kind"] == "worker" else SESSION_KIND[s["kind"]] + (f" ({s['owner']})" if s["kind"] == "orphan" else "")
        out.append(Row(f"s-{s['id'].rpartition(':')[2]}", str(s.get("label")), text, s["kind"], float(seen["ts"]), mark="decision" if s["kind"] == "orphan" else "",
                       actions=(("show", s["id"], "Show"),) if s["kind"] != "other" else ()))
    return out


def _pr_key(url):
    import re
    found = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", str(url or ""))
    return f"{found.group(1)}#{found.group(2)}".lower() if found else None


def _waiting(actions, only=None) -> dict:
    """When each item's oldest still queued answer was given."""
    out = {}
    for a in actions:
        if a.get("status") != "queued" or only and a.get("option") not in only:
            continue
        out[a.get("item")] = min(out.get(a.get("item"), float(a["ts"])), float(a["ts"]))
    return out


def fold(now=None) -> dict:
    now = time.time() if now is None else now
    sections = {"needs": [], "ready": [], "awaiting": [], "flight": [], "sessions": [], "done": []}
    actionable = {}
    notes = []
    status = caps.get("status")
    watched = store.read_dict(store.path("watch.json"))
    pr_of = {k: (v or {}).get("pr") for k, v in (watched.get("workers") or {}).items() if isinstance(v, dict)}
    fetched = [float(p["fetched"]) for p in pr_of.values() if p and p.get("fetched")]
    if fetched and now - min(fetched) > PR_STALE:
        notes.append(f"GitHub data is {int((now - min(fetched)) // 60)} minutes old")
    rows = []
    if status:
        try:
            rows = status.collect()
        except Exception as exc:
            notes.append(f"workers: {exc}")
    items_cap = caps.get("items")
    loaded = items_cap.load() if items_cap else {"items": []}
    items = loaded["items"]
    if loaded.get("unreadable"):
        notes.append("items unreadable: needs-you.json does not parse, so this is its last generation")
    actions = list(items_cap._fold(items_cap._action_lines()).values()) if items_cap else []
    queued = _waiting(actions)
    sent = {}
    for a in actions:
        if a.get("status") == "queued" and now - float(a["ts"]) < STUCK_AFTER and SENT_TEXT.get(a.get("option")):
            sent[a.get("item")] = SENT_TEXT[a["option"]]
    auto = {a.get("item"): a for a in actions if a.get("auto") and a.get("status") == "queued"}
    open_prs = bool(config.get("open_prs_channel")) and store.path("open-prs-posted.json").exists()

    def place_pr(row_for, key, move, url, head, live_worker=None):
        """A PR's row where whose move it is says: Needs you with its buttons, Awaiting review with Re-check, else In flight."""
        if move.get("kind") == "awaiting":
            acts = Actions(item=f"pr:{key}", recheck=True)
            rechecking = key and f"pr:{key}" in sent and sent[f"pr:{key}"] == SENT_TEXT["recheck"]
            if rechecking:
                acts["recheck"] = False
            actionable[acts["item"]] = acts
            sections["awaiting"].append(row_for(acts, "re-checking…" if rechecking else ""))
        elif move.get("needs") and key:
            acts = pr_actions(key, move, url, head, open_prs)
            mark = sent.get(acts["item"], "")
            if mark:
                for flag in ("request_review", "recheck", "go", "post_open_prs"):
                    acts[flag] = False
            actionable[acts["item"]] = acts
            sections["needs"].append(row_for(acts, mark))
        else:
            acts = Actions(item=f"w-{live_worker}", discuss=True, reply=True) if live_worker else Actions(item=f"pr:{key}")
            actionable[acts["item"]] = acts
            sections["flight"].append(row_for(acts, ""))

    owned = set()
    for r in rows:
        r["pr"] = pr_of.get(r["name"])
        pr = r["pr"] or {}
        move = pr.get("move") or {}
        key = _pr_key(pr.get("url"))
        if key:
            owned.add(key)
        acts = Actions(item=f"w-{r['name']}", discuss=True, reply=True)
        report = r.get("report") or {}
        if r.get("pending"):
            actionable[acts["item"]] = acts
            sections["needs"].append(_worker_row(r, acts, label="waiting at a startup prompt",
                                                 detail="Answer it in its Rex session; athena then sends the goal."))
        elif r["state"] == "blocked":
            label = {"BLOCKED": "blocked", "NEEDS_CONTEXT": "needs context"}.get(str(report.get("status")), "permission prompt")
            detail = (report.get("concerns") or [""])[0] if label != "permission prompt" else r.get("summary") or ""
            actionable[acts["item"]] = acts
            sections["needs"].append(_worker_row(r, acts, label=label, detail=detail))
        elif pr.get("state") == "MERGED":
            sections["done"].append(_worker_row(r, Actions(item=f"w-{r['name']}"), label="merged, not retired yet"))
        elif pr.get("state") == "OPEN" and not pr.get("draft") and move and move.get("kind") != "working" and \
                (r["state"] != "working" or move.get("kind") in ("approved", "queued", "changes")):
            place_pr(lambda acts, mark, r=r, move=move: _worker_row(r, acts, mark, label=move.get("label")), key, move, pr.get("url"),
                     pr.get("head"), None if move.get("needs") else r["name"])
        else:
            actionable[acts["item"]] = acts
            sections["flight"].append(_worker_row(r, acts))
    follow = caps.get("follow")
    followed = follow.known() if follow else {}
    for key, p in sorted((followed.get("prs") or {}).items()):
        if key.lower() in owned or not isinstance(p, dict) or p.get("state") != "OPEN" or not p.get("move"):
            continue

        def followed_row(acts, mark, p=p, key=key):
            who = f"{p['owner']}'s PR" if p.get("owner") else "stacked PR"
            return Row(f"p-{key}", str(p.get("title") or key), f"{key} · {who}", p["move"]["label"],
                       float(p.get("fetched") or followed.get("ts") or now), link=p.get("url"), actions=_pr_buttons(acts),
                       key=acts["item"], sent=mark)
        place_pr(followed_row, key.lower(), p["move"], p.get("url"), p.get("head"))
    for asking in followed.get("review_requests") or []:
        sections["needs"].append(Row(f"rr-{asking.get('repo')}#{asking.get('number')}", str(asking.get("title") or ""),
                                     f"{asking.get('repo')}#{asking.get('number')} by {asking.get('author')}", "your review requested",
                                     float(followed.get("ts") or now), link=asking.get("url")))
    snoozed = 0
    for item in items:
        if item.get("resolved"):
            if now - float(item["resolved"]) < DAY:
                sections["done"].append(Row(item["id"], str(item.get("name")), str(item.get("resolved_note") or ""),
                                            "resolved", float(item["resolved"]), link=f"/item/{item['id']}", key=item["id"]))
            continue
        if items_cap and items_cap.snoozed(item, now):
            snoozed += 1
            continue
        acts = Actions(item=item["id"], options=[o.get("id") for o in item.get("options") or []])
        labels = {o.get("id"): o.get("label") for o in item.get("options") or []}
        if item["id"] in auto or item.get("applied"):
            label = labels.get(auto[item["id"]].get("option")) if item["id"] in auto else None
            verb = "athena is taking the rec on its own: "
            if item.get("applied"):
                label, verb = item["applied"].get("label"), "athena took the rec on its own: "
            acts.update(done=True, undo=label or "default", options=[])
            actionable[item["id"]] = acts
            sections["ready"].append(_item_row(item, now, acts, verb + str(label)))
            continue
        mark = ""
        if item["id"] in queued:
            if now - queued[item["id"]] < STUCK_AFTER:
                continue
            mark = STUCK_WHY
        if item.get("kind") == "decision":
            acts.update(discuss=not item.get("permission"), reply=not item.get("permission"), hides=True, rule=not item.get("permission"), snooze=True,
                        take_rec=bool(item.get("rec")))
        elif item.get("kind") == "result":
            acts.update(done=True, snooze=True)
        else:
            acts.update(done=True)
        actionable[item["id"]] = acts
        row = _item_row(item, now, acts, sent=mark)
        if item.get("kind") == "result" and item.get("next") == "you":
            sections["ready"].append(row)
        elif item.get("next") == "you":
            sections["needs"].append(row)
        else:
            sections["flight"].append(row)
    for line in store.read_lines(store.path("history.jsonl")):
        ts = float(line.get("ts") or 0)
        if now - ts < DAY and (line.get("pr") or line.get("linear")):
            sections["done"].append(Row(f"h-{line.get('name')}", str(line.get("title") or line.get("name")), "",
                                        "retired", ts, link=(line.get("pr") or {}).get("url")))
    for key in sections:
        sections[key].sort(key=lambda r: r.ts, reverse=True)
    sections["sessions"] = _session_rows(now, notes, {r["name"]: r for r in rows})
    limits = caps.get("limits")
    away = limits.unavailable() if limits else []
    if away:
        notes.append(f"Claude's account changed. Unavailable until `athena-rex limits accept`: {', '.join(away)}")
    sections["needs"].sort(key=lambda r: 0 if r.mark == "blocked" else 1)
    sections["done"] = sections["done"][:12]
    health = store.read_json(store.path("health.json"), None)
    return {"sections": sections, "health": _health(health, now), "notes": notes, "snoozed": snoozed, "now": now,
            "watch_age": now - float(watched.get("ts") or now), "live": len(rows), "known": len(ledger.workers()),
            "actionable": actionable}


def _health(health, now) -> dict:
    """Never green without a fresh check."""
    if not health:
        return {"color": "grey", "text": "doctor hasn't reported"}
    age_s = now - float(health.get("ts") or 0)
    if age_s < -60:
        return {"color": "grey", "text": "the clock moved back since doctor's last check"}
    if age_s > 180:
        return {"color": "grey", "text": f"doctor not running · last check {time.strftime('%H:%M', time.localtime(health.get('ts')))}"}
    if health.get("ok"):
        return {"color": "green", "text": "all ok"}
    problems = [p.get("check") for p in health.get("problems") or []]
    return {"color": "red", "text": ", ".join(str(p) for p in problems[:4]) or "not ok"}
