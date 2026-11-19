"""athena in Rex's command palette: the actions, the line that loads them, and what they ask athena.

`palette.lua`, beside this file, defines the actions in Rex's own Lua. Each asks athena what to offer, puts the question in the
palette (`rex.ui.pick`, `rex.ui.input`), and hands the answer back to athena:

    athena_answer   Answer a Decision...   the dashboard's own answer, through the dashboard's own writer
    athena_message  Message a Worker...    `athena send`
    athena_open     Open...                a worker's window, its pull request, an item's page on the dashboard
    athena_retire   Retire a Worker...     `athena retire`, once Retire is chosen in a second question

The rules live here, not in the Lua: what is offered, what an answer may be, and every write. The Lua finds `bin/athena` from
its own path, which `install` writes through `current` (as `store.ROOT` is, unresolved), so it always runs the current release.
"""
import json
import os
import re
import webbrowser
from pathlib import Path

from athena.cli import UserError
from athena.kernel import caps, lab, store, world

SUMMARY = "athena's actions in Rex's command palette: install them, and what they ask"
LUA = Path(os.path.abspath(__file__)).with_name("palette.lua")
MARK = "-- athena's palette actions (athena palette install)"
ACTIONS = (("athena_answer", "Answer a Decision…"), ("athena_message", "Message a Worker…"), ("athena_open", "Open…"),
           ("athena_retire", "Retire a Worker…"))
PICK_MAX = 2000  # what rex.ui.pick takes
LINE_MAX = 160  # a subtitle, cut to one line of the palette
PR_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,9}$")


# Rex's config file and the one line in it that loads the actions

def config_path(env=None) -> Path:
    """Where Rex reads its config, as Rex documents it. REX_CONFIG counts in the Rex server's environment, not this one's."""
    env = os.environ if env is None else env
    if env.get("REX_CONFIG"):
        return Path(env["REX_CONFIG"]).expanduser()
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(base, "rex", "init.lua")


def _lua_string(text: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise UserError(f"{text!r} has a control character, so no Lua line can name it")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(lua=LUA, state_dir=None) -> str:
    """Loads the actions for this state dir, whatever the Rex server's environment holds, and only warns in Rex's log when the
    file is gone, so the rest of the config still loads."""
    state = _lua_string(str(state_dir or store.state_dir()))
    return (f'do local ok, err = pcall(function() dofile({_lua_string(str(lua))}){{ state_dir = {state} }} end) '
            f'if not ok then rex.log("warn", "athena palette: " .. tostring(err)) end end {MARK}')


def _marked(text: str) -> list:
    return [n for n, l in enumerate(text.splitlines()) if l.rstrip().endswith(MARK)]


def installed(target) -> str:
    """`installed`, `another` (a marked line naming another file), `absent` (no line) or `no file`."""
    try:
        text = Path(target).read_text()
    except FileNotFoundError:
        return "no file"
    lines = text.splitlines()
    marked = _marked(text)
    if not marked:
        return "absent"
    return "installed" if [lines[n] for n in marked] == [line()] else "another"


def install(target, explicit: bool) -> str:
    """Puts the line in Rex's config once: `installed`, `updated` (a marked line replaced) or `already`. Rex's own config is the
    real world's, so only a state dir that opens the door writes it; `--config` names another file, such as a scratch one."""
    target = Path(target)
    if not explicit:
        world.require("writing Rex's config")
    if lab.active() and lab.off_limits(target):
        raise UserError(f"refusing {target}: ATHENA_WORKER={os.environ['ATHENA_WORKER']} writes only a scratch Rex config")
    was = installed(target)
    if was == "installed":
        return "already"
    text = "" if was == "no file" else target.read_text()
    lines, marked = text.splitlines(), _marked(text)
    if marked:
        lines[marked[0]] = line()
        lines = [l for n, l in enumerate(lines) if n not in marked[1:]]
    else:
        lines.append(line())
    store.write_text(target, "\n".join(lines) + "\n")
    return "updated" if marked else "installed"


# What the actions offer

def _one_line(text) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= LINE_MAX else words[:LINE_MAX - 1] + "…"


def _item(id_, title, subtitle="", icon="", detail="") -> dict:
    out = {"id": id_, "title": title}
    for key, value in (("subtitle", _one_line(subtitle)), ("icon", icon), ("detail", detail)):
        if value:
            out[key] = value
    return out


def _dash():
    """The dashboard's server, holding a view of the state as it is now and running nothing: no page, no defaults applied."""
    if caps.get("dash") is None or caps.get("items") is None:
        raise UserError("the dashboard is off in config (rex.caps), and answers go through it")
    from athena.caps.dash import server, view
    dash = server.Server(store.state_dir(), 0, os.environ.get("ATHENA_LINEAR_WORKSPACE", ""), athena=True)
    dash.view = view.build(dash.load(), dash.now(), dash.linear_workspace)
    return dash


def _decisions(dash) -> list:
    """(item, the row's actions) for each decision waiting for the human that the page lets them answer, in the page's order.
    The rest of what the page answers is left to it: workers' rows, PR buttons, and permission prompts, which press keys."""
    raw = {i.get("id"): i for i in caps.get("items").load()["items"]}
    out = []
    for row in dash.view.needs_you:
        item, acts = raw.get(row.item), row.actions
        if not row.id.startswith("ask-") or acts is None or not item or item.get("kind") != "decision" or item.get("permission"):
            continue
        if acts.options or acts.reply:
            out.append((item, acts))
    return out


def decisions() -> dict:
    """The decisions to pick from, and for each the answers it takes: its options, the recommended one first and starred, and
    whether a typed reply is offered."""
    items, answers = [], {}
    for item, acts in _decisions(_dash())[:PICK_MAX]:
        name = item.get("name") or "Untitled item"
        items.append(_item(item["id"], name, item.get("text"), "tag", item["id"]))
        rec = acts.take_rec.id if acts.take_rec else ""
        options = sorted(acts.options, key=lambda o: o.id != rec)
        answers[item["id"]] = {"title": f"Answer: {name}", "reply": bool(acts.reply),
                               "options": [_item(o.id, o.label or o.id, icon="star" if o.id == rec else "",
                                                 detail="recommended" if o.id == rec else "") for o in options]}
    return {"items": items, "answers": answers}


def answer(item_id: str, option=None, text=None) -> dict:
    """Queues the answer exactly as the dashboard does: its own checks against what the page offers, its own line in
    actions.jsonl under ask.lock. HQ reads it with `athena ask queue`, and the watch rings `dash-queue` for it."""
    dash = _dash()
    found = {item["id"]: item for item, _ in _decisions(dash)}
    if item_id not in found:
        raise UserError(f"{item_id} is not a decision waiting for your answer: answered already, snoozed, resolved or not one")
    from athena.caps.dash import server
    request = {"item": item_id, "option": option} if text is None else {"item": item_id, "text": text}
    try:
        seq = dash.take_action(request)
    except server.HTTPError as exc:
        raise UserError(f"{item_id}: {exc.msg}")
    name = found[item_id].get("name") or item_id
    if text is None:
        label = next((o.get("label") for o in found[item_id].get("options") or [] if o.get("id") == option), option)
        said = f"Answered {name}: {label}"
    else:
        said = f"Replied to {name}: {_one_line(text)}"
    return {"said": said + ". athena takes it up in a few seconds.", "seq": seq}


def _live() -> list:
    status = caps.get("status")
    return status.collect() if status else []


def _pr_of(row) -> str:
    for key in ("report", "last_report"):
        url = (row.get(key) or {}).get("pr") if isinstance(row.get(key), dict) else None
        if isinstance(url, str) and PR_URL.match(url):
            return url
    return ""


def workers() -> dict:
    return {"items": [_item(r["name"], r["name"], r.get("title") or r.get("summary"), "terminal", r.get("state") or "")
                      for r in _live()][:PICK_MAX]}


def targets() -> dict:
    """What Open offers: each live worker's window and its pull request, then every open item's page."""
    out = []
    for r in _live():
        out.append(_item(f"worker:{r['name']}", r["name"], r.get("title") or r.get("summary"), "terminal", r.get("state") or ""))
        url = _pr_of(r)
        if url:
            repo, number = url[len("https://github.com/"):].split("/pull/")
            out.append(_item(f"pr:{url}", f"{repo.split('/')[1]}#{number}", f"{r['name']}'s pull request", "branch"))
    items = caps.get("items")
    for item in items.open_items() if items else []:
        out.append(_item(f"item:{item['id']}", item.get("name") or "Untitled item", item.get("text"), "document", item["id"]))
    return {"items": out[:PICK_MAX]}


def open_target(target: str) -> dict:
    """Only what `targets` offers now. A worker's window is shown by the Lua, in the app that asked: athena says which. A page
    opens in the browser, which is the real world's."""
    if target not in {t["id"] for t in targets()["items"]}:
        raise UserError(f"{target} is not something athena offers to open now")
    kind, _, rest = target.partition(":")
    if kind == "worker":
        row = next(r for r in _live() if r["name"] == rest)
        if not row.get("space") or not row.get("window"):
            raise UserError(f"{rest} has no Rex window on record")
        return {"said": f"Showing {rest}.", "focus": {"session_id": row["space"], "window_id": row["window"]}}
    world.require("opening a browser")
    if kind == "pr":
        url = rest
    else:
        dash = caps.get("dash")
        dash.on()
        url = f"{dash.url()}item/{rest}"
    webbrowser.open(url)
    return {"said": f"Opened {url} in your browser."}


# The command

def _configure(p):
    sub = p.add_subparsers(dest="action")
    sub.add_parser("show", help="the actions, and whether Rex's config loads them (the default)")
    put = sub.add_parser("install", help="add the line that loads them to Rex's config, once")
    put.add_argument("--config", help="this file instead of Rex's own config (REX_CONFIG, else ~/.config/rex/init.lua)")
    sub.add_parser("decisions", help="JSON: the decisions waiting for you, and the answers each takes")
    said = sub.add_parser("answer", help="JSON: answer a decision, as the dashboard does")
    said.add_argument("item")
    one = said.add_mutually_exclusive_group(required=True)
    one.add_argument("--option")
    one.add_argument("--text", help="a typed reply")
    sub.add_parser("workers", help="JSON: the live workers")
    sub.add_parser("targets", help="JSON: what Open offers")
    show = sub.add_parser("open", help="JSON: open one of them")
    show.add_argument("target")


def _show():
    target = config_path()
    state = installed(target)
    words = {"installed": "loads them", "another": "loads them from another copy of athena or for another state dir: athena "
             "palette install points it here",
             "absent": "does not load them", "no file": "does not exist yet"}
    print(f"actions   {LUA}")
    for name, title in ACTIONS:
        print(f"          {name:<15} athena: {title}")
    print(f"config    {target}: {words[state]}")
    if state != "installed":
        print("install   athena palette install, then rex config reload")
    return 0


def _run(args):
    action = args.action or "show"
    if action == "show":
        return _show()
    if action == "install":
        target = Path(args.config).expanduser() if args.config else config_path()
        done = install(target, explicit=bool(args.config))
        print({"installed": f"added the line to {target}", "updated": f"pointed the line in {target} here",
               "already": f"{target} already loads them"}[done])
        if done != "already":
            print("Rex reads it on its next load: rex config reload")
        return 0
    out = {"decisions": decisions, "workers": workers, "targets": targets}.get(action)
    if out:
        result = out()
    elif action == "answer":
        result = answer(args.item, option=args.option, text=args.text)
    else:
        result = open_target(args.target)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def commands():
    return {"palette": caps.Command(SUMMARY, _configure, _run)}
