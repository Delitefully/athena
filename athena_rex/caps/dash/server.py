"""The HTTP side: loopback only, a Host check against DNS rebinding, and a per-start token on anything that writes."""
import html
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from athena_rex.cli import UserError
from athena_rex.caps.dash import markdown, view
from athena_rex.kernel import caps, store
from athena_rex.kernel.backend import BackendError

START = secrets.token_hex(8)
TOKEN = secrets.token_hex(16)
WATCHED = ("needs-you.json", "ledger.jsonl", "watch.json", "health.json", "history.jsonl", "actions.jsonl")
POLL = 1.0
TITLES = (("needs", "Needs you"), ("ready", "Ready for you"), ("awaiting", "Awaiting review"), ("flight", "In flight"),
          ("sessions", "Rex sessions"), ("done", "Done today"))
CSS = """
:root{--ground:#f4efe6;--ink:#1f2340;--muted:#6d6f85;--line:#d9d2c4;--mark:#a8322a;--wash:#ebe4d6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--ground:#14162b;--ink:#ece6da;--muted:#9a9cb3;--line:#2c2f4d;--mark:#e0705f;--wash:#1e2140}}
:root[data-theme=dark]{--ground:#14162b;--ink:#ece6da;--muted:#9a9cb3;--line:#2c2f4d;--mark:#e0705f;--wash:#1e2140}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font:14px/1.45 -apple-system,"Instrument Sans",system-ui,sans-serif;font-variant-numeric:tabular-nums}
header{display:flex;gap:16px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line)}header h1{font:400 20px Georgia,"Instrument Serif",serif;margin:0;color:var(--mark)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}.green{background:#3d9a5f}.red{background:var(--mark)}.grey{background:var(--muted)}
#told{margin:0;padding:8px 20px;background:var(--mark);color:var(--ground)}input[type=search]{margin-left:auto;background:transparent;color:inherit;border:1px solid var(--line);border-radius:6px;padding:5px 9px;min-width:0;width:220px}
main{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:20px;max-width:1500px}@media (max-width:820px){main{grid-template-columns:1fr;padding:16px}}
h2{font:400 19px Georgia,"Instrument Serif",serif;margin:0 0 8px}h2 small{color:var(--muted);font-size:14px;margin-left:6px}section{margin-bottom:26px}
.row{display:grid;grid-template-columns:1fr auto;gap:0 12px;padding:7px 8px;border-radius:6px;cursor:pointer}.row:hover,.row:focus-within{background:var(--wash)}
.line{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.line b{font-weight:600}.line a{color:inherit;text-decoration:none}.age{color:var(--muted)}
.sub{color:var(--muted);grid-column:1/-1;font-size:12.5px;min-height:18px}.decision b{color:var(--mark)}.blocked b:before{content:"! ";color:var(--mark)}
button{font:inherit;font-size:12px;background:transparent;color:inherit;border:1px solid var(--line);border-radius:11px;padding:1px 9px;cursor:pointer}button:hover{background:var(--line)}
.acts{display:none;gap:6px}.row:hover .acts,.row:focus-within .acts{display:flex}.row:hover .lab,.row:focus-within .lab{display:none}@media (hover:none){.acts{display:flex;margin-top:4px}}
details summary{cursor:pointer;color:var(--muted);padding:5px 8px}.note{color:var(--muted);font-size:12.5px}
article{max-width:780px;padding:20px}article h1{font:400 26px Georgia,"Instrument Serif",serif;margin:0 0 4px}article pre{background:var(--wash);padding:10px;overflow:auto;border-radius:6px}article table{border-collapse:collapse}article td,article th{border:1px solid var(--line);padding:4px 8px}
blockquote{font:italic 16px Georgia,serif;margin:12px 0;padding-left:12px;border-left:2px solid var(--mark)}.opts{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.opts button{font-size:14px;padding:5px 14px;border-radius:15px}.rec{border-color:var(--mark)}
"""
JS = """
const token=document.querySelector('meta[name=token]').content;
function tell(t){const n=document.getElementById('told');if(n){n.textContent=t;n.hidden=!t;}}
function post(path,body){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Athena-Token':token},body:JSON.stringify(body)}).then(async r=>{if(r.ok){tell('');refresh();}else tell('Not saved: '+((await r.text())||r.status));return r;}).catch(()=>tell('Not saved: the dashboard did not answer.'));}
async function refresh(){const r=await fetch(location.pathname+'?fragment=1');if(!r.ok)return;const t=await r.text();const m=document.querySelector('[data-main]');const open=[...document.querySelectorAll('details[open]')].map(d=>d.id);m.innerHTML=t;open.forEach(i=>{const d=document.getElementById(i);if(d)d.open=true});filter();}
function answered(r){if(!r||!r.ok)return;r.clone().json().then(j=>{if(!j.seq)return;const n=document.getElementById('told');n.innerHTML='';const s=document.createElement('span');s.textContent='Sent to athena. ';const u=document.createElement('button');u.textContent='Undo';u.onclick=()=>post('/api/action',{undo:true,seq:j.seq});n.append(s,u);n.hidden=false;setTimeout(()=>{if(n.contains(u))tell('');},5000);}).catch(()=>{});}
function rule(){const c=document.getElementById('rule');return !!(c&&c.checked);}
function answer(body){if(rule())body.rule=true;return post('/api/action',body).then(answered);}
document.addEventListener('click',e=>{const b=e.target.closest('[data-act]');if(b){e.stopPropagation();const d=b.dataset;if(d.act==='option')answer({item:d.item,option:d.value});else if(d.act==='go')answer({item:d.item,option:'go',head:d.value});else if(d.act==='discuss')post('/api/action',{item:d.item,discuss:true}).then(answered);else if(d.act==='reply'){const t=prompt('Reply');if(t)answer({item:d.item,text:t});}else if(d.act==='done')post('/api/item',{item:d.item,do:'done'});else if(d.act==='snooze')post('/api/item',{item:d.item,do:'snooze',until:'1d'});else if(d.act==='reopen')post('/api/item',{item:d.item,do:'reopen'});else if(d.act==='show')post('/api/session',{session:d.value});return;}const r=e.target.closest('.row[data-href]');if(r&&!e.target.closest('a'))location.href=r.dataset.href;});
function filter(){const s=document.getElementById('q');if(!s)return;const w=s.value.toLowerCase().split(/\\s+/).filter(Boolean);document.querySelectorAll('.row').forEach(r=>{const t=r.textContent.toLowerCase();r.style.display=w.every(x=>t.includes(x))?'':'none';if(w.length&&r.style.display==='')(r.closest('details')||{}).open=true;});}
document.addEventListener('input',e=>{if(e.target.id==='q')filter();});
const es=new EventSource('/events');let start=null;es.onmessage=e=>{const d=JSON.parse(e.data);if(start&&d.start!==start)location.reload();start=d.start;if(d.kind==='change')refresh();const n=document.getElementById('live');if(n)n.textContent='';};es.onerror=()=>{const n=document.getElementById('live');if(n)n.textContent='reconnecting…';};
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _button(row, act, value, text) -> str:
    key = row.key or row.id
    if act == "link":
        return f'<a class="pill" href="{esc(value)}" target="_blank" rel="noopener noreferrer">{esc(text)}</a>'
    return f'<button data-act="{esc(act)}" data-item="{esc(key)}" data-value="{esc(value)}">{esc(text)}</button>'


def _row_html(row: view.Row, now) -> str:
    target = f' data-href="{esc(row.link)}"' if row.link and row.link.startswith("/") else ""
    name = f'<a href="{esc(row.link)}">{esc(row.name)}</a>' if row.link else esc(row.name)
    buttons = "".join(_button(row, a, v, t) for a, v, t in row.actions)
    acts = f'<span class="acts">{buttons}</span>' if buttons else ""
    label = f"{row.label} · {row.sent}" if row.sent else row.label
    return (f'<div class="row {esc(row.mark)}" id="{esc(row.id)}"{target}><div class="line"><b>{name}</b> {esc(row.text)}</div>'
            f'<div class="age">{view.age(row.ts, now)}</div><div class="sub"><span class="lab">{esc(label)}</span>{acts}</div></div>')


def main_html(data=None) -> str:
    data = data or view.fold()
    now = data["now"]
    columns = {"left": [], "right": []}
    for key, title in TITLES:
        rows = data["sections"][key]
        if not rows and key in ("awaiting", "done", "sessions", "ready"):
            continue
        shown = rows[:view.SHOW_MAX] if key != "done" else []
        body = "".join(_row_html(r, now) for r in shown)
        rest = rows[len(shown):]
        if rest:
            label = "Show" if key == "done" else f"{len(rest)} more"
            body += (f'<details id="fold-{key}"><summary>{label}</summary>'
                     + "".join(_row_html(r, now) for r in rest) + "</details>")
        if not rows:
            body = f'<p class="note">{"Nothing waits on you." if key == "needs" else "Nothing in flight."}</p>'
        side = "left" if key in ("needs", "ready") else "right"
        columns[side].append(f'<section id="{key}"><h2>{esc(title)}<small>{len(rows)}</small></h2>{body}</section>')
    foot = ""
    if data["snoozed"]:
        foot += f'<p class="note">{data["snoozed"]} snoozed: each comes back when its time is up, or sooner if something happens to it.</p>'
    for note in data["notes"]:
        foot += f'<p class="note">{esc(note)}</p>'
    return f'<div>{"".join(columns["left"])}</div><div>{"".join(columns["right"])}{foot}</div>'


def _shell(title, body, health) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="token" content="{TOKEN}"><title>{esc(title)}</title><style>{CSS}</style></head><body>'
            f'<header><h1><a href="/" style="color:inherit;text-decoration:none">athena</a></h1>'
            f'<span title="{esc(health["text"])}"><span class="dot {esc(health["color"])}"></span>{esc(health["text"])}</span>'
            f'<span id="live" class="note"></span><input id="q" type="search" placeholder="Search items" aria-label="Search items"></header>'
            f'<p id="told" role="alert" hidden></p>'
            f'{body}<script>{JS}</script></body></html>')


def page_html() -> str:
    data = view.fold()
    needs = len(data["sections"]["needs"])
    return _shell(f"({needs}) athena" if needs else "athena", f'<main data-main>{main_html(data)}</main>', data["health"])


def _find_item(item_id):
    items = caps.get("items")
    for item in items.load()["items"]:
        if item.get("id") == item_id:
            return item
    for item in store.read_lines(store.path("items-archive.jsonl")):
        if item.get("id") == item_id:
            return item
    return None


def item_html(item) -> str:
    parts = [f"<h1>{esc(item.get('name'))}</h1><p class='note'>{esc(item.get('kind'))} · {esc(item.get('next'))}"
             f"{' · resolved' if item.get('resolved') else ''}</p>"]
    if item.get("words"):
        parts.append(f"<h3>You asked</h3><blockquote>{esc(item['words'])}</blockquote>")
    parts.append(f"<h3>Where it stands</h3><p>{esc(item.get('text'))}</p><p class='note'>{esc(item.get('why'))}</p>")
    if item.get("body"):
        parts.append(f"<h3>{'Report' if item.get('kind') == 'result' else 'Brief'}</h3>{markdown.render(item['body'])}")
    for name in item.get("previews") or []:
        url = f"/preview/{esc(item['id'])}/{esc(name)}"
        parts.append(f'<figure class="preview"><iframe src="{url}" sandbox="allow-scripts" loading="lazy" title="{esc(name)}" '
                     f'style="width:100%;height:420px;border:1px solid var(--line)"></iframe><figcaption><a href="{url}" target="_blank" '
                     f'rel="noopener noreferrer">{esc(name.rstrip("/"))}</a></figcaption></figure>')
    acts = view.fold()["actionable"].get(item["id"]) if not item.get("resolved") else None
    if acts is not None:
        iid = esc(item["id"])
        buttons = ""
        if acts.get("undo"):
            buttons += f'<button data-act="option" data-item="{iid}" data-value="undo-default">Undo {esc(acts["undo"])}</button>'
        else:
            for option in item.get("options") or []:
                rec = " rec" if option.get("id") == item.get("rec") else ""
                buttons += f'<button class="{rec}" data-act="option" data-item="{iid}" data-value="{esc(option.get("id"))}">{esc(option.get("label"))}</button>'
        if acts.get("discuss"):
            buttons += f'<button data-act="discuss" data-item="{iid}">Discuss in Athena</button>'
        if acts.get("reply"):
            buttons += f'<button data-act="reply" data-item="{iid}">Reply…</button>'
        if acts.get("done"):
            buttons += f'<button data-act="done" data-item="{iid}">Done</button>'
        if acts.get("snooze"):
            buttons += f'<button data-act="snooze" data-item="{iid}">Snooze</button>'
        if acts.get("rule"):
            buttons += '<label class="note"><input type="checkbox" id="rule"> Don\'t ask me this again</label>'
        parts.append(f'<div class="opts">{buttons}</div>')
    elif not item.get("resolved"):
        parts.append('<p class="note">Answered: athena has it.</p>')
    else:
        parts.append(f'<p>{esc(item.get("resolved_note"))}</p><div class="opts"><button data-act="reopen" data-item="{esc(item["id"])}">Reopen</button></div>')
    log = "".join(f"<li>{time.strftime('%b %d %H:%M', time.localtime(e.get('ts') or 0))} {esc(e.get('text'))}</li>"
                  for e in item.get("log") or [])
    if log:
        parts.append(f"<h3>Timeline</h3><ul>{log}</ul>")
    return "".join(parts)


def search_html(query) -> str:
    items = caps.get("items")
    words = query.lower().split()
    found = []
    for item in items.load()["items"] + list(store.read_lines(store.path("items-archive.jsonl"))):
        text = " ".join(str(item.get(k) or "") for k in ("name", "text", "why", "pr", "id")).lower()
        if words and all(w in text for w in words):
            found.append(item)
    rows = "".join(f'<p><a href="/item/{esc(i.get("id"))}">{esc(i.get("name"))}</a> {esc(i.get("text"))}</p>' for i in found[:200])
    return f"<article data-main><h1>Search: {esc(query)}</h1>{rows or '<p class=note>nothing found</p>'}</article>"


def signature():
    out = []
    root = store.state_dir()
    for name in WATCHED:
        try:
            st = os.stat(root / name)
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, 0, 0))
    try:
        st = os.stat(root / "workers")
        out.append(("workers", st.st_mtime_ns, 0))
    except OSError:
        pass
    return tuple(out)


class Handler(BaseHTTPRequestHandler):
    server_version = "athena-rex"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, kind="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "bad host")
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        fragment = "fragment" in query
        path = parsed.path
        try:
            if path == "/healthz":
                return self._send(200, json.dumps({"pid": os.getpid(), "state_dir": str(store.state_dir()), "start": START}),
                                  "application/json")
            if path == "/":
                return self._send(200, main_html() if fragment else page_html())
            if path == "/events":
                return self._events()
            if path.startswith("/item/"):
                item = _find_item(path[len("/item/"):])
                if not item:
                    return self._send(404, "no such item")
                body = item_html(item)
                health = view.fold()["health"]
                return self._send(200, body if fragment else _shell(item.get("name") or "item", f"<article data-main>{body}</article>", health))
            if path.startswith("/preview/"):
                return self._preview(path[len("/preview/"):])
            if path == "/items":
                health = view.fold()["health"]
                return self._send(200, _shell("search", search_html((query.get("q") or [""])[0]), health))
        except Exception as exc:
            return self._send(500, f"<pre>{esc(type(exc).__name__)}: {esc(exc)}</pre>")
        return self._send(404, "not found")

    def _preview(self, rest):
        """A file of an item's preview, read-only and sandboxed: no allow-same-origin, so its script reaches nothing of the dash."""
        parts = rest.split("/")
        if len(parts) < 2 or any(not p or p.startswith(".") for p in parts[:-1]) or not store.valid_name(parts[0]):
            return self._send(404, "not found")
        if not parts[-1]:
            parts[-1] = "index.html"
        root = store.path("previews").resolve()
        target = root.joinpath(*parts)
        try:
            if root not in target.resolve().parents or target.is_symlink() or not target.is_file():
                return self._send(404, "not found")
            data = target.read_bytes()
        except OSError:
            return self._send(404, "not found")
        kinds = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
                 ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json"}
        self.send_response(200)
        self.send_header("Content-Type", kinds.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "sandbox allow-scripts")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last = signature()
        beat = 0
        try:
            self.wfile.write(f'data: {json.dumps({"kind": "hello", "start": START})}\n\n'.encode())
            self.wfile.flush()
            while True:
                time.sleep(POLL)
                now = signature()
                beat += 1
                if now != last or beat >= 25:
                    kind = "change" if now != last else "beat"
                    self.wfile.write(f'data: {json.dumps({"kind": kind, "start": START})}\n\n'.encode())
                    self.wfile.flush()
                    last, beat = now, 0
        except OSError:
            return

    def do_POST(self):
        port = self.server.server_address[1]
        if not self._host_ok() or self.headers.get("Origin") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._send(403, "bad origin")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or \
                not secrets.compare_digest(self.headers.get("X-Athena-Token", ""), TOKEN):
            return self._send(403, "bad token")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            return self._send(400, "bad json")
        items = caps.get("items")
        if not isinstance(body, dict) or not items:
            return self._send(400, "bad request")
        try:
            if self.path == "/api/action":
                return self._action(items, body)
            if self.path == "/api/item":
                return self._item(items, body)
            if self.path == "/api/session":
                return self._show_session(body)
        except UserError as exc:
            return self._send(409, str(exc))
        except OSError as exc:
            return self._send(503, f"it could not be written: {exc}")
        return self._send(404, "not found")

    def _action(self, items, body):
        """As the old dash: one answer per POST, and only one the page offers for that row (`actionable`); a go names the head the page
        showed and is taken only while the PR is still at it; "Don't ask me this again" goes only with an answer to a decision. Undo
        takes back an answer within its settle time. Nothing here writes to GitHub: each answer is queued for athena."""
        if body.get("undo"):
            if not isinstance(body.get("seq"), int) or body["seq"] <= 0 or set(body) - {"undo", "seq"}:
                return self._send(400, "undo names a seq only")
            if not items.undo(int(body["seq"])):
                return self._send(409, "too late to undo: athena already has it")
            return self._send(200, "{}", "application/json")
        item_id = str(body.get("item") or "")
        text = body.get("text")
        if text is not None:
            text = str(text).strip()
            if not text or len(text) > 2000:
                return self._send(400, "a reply is 1 to 2000 characters")
        given = [k for k in ("option", "text", "discuss") if body.get(k) not in (None, False, "")]
        if len(given) != 1:
            return self._send(400, "one answer per POST")
        option = str(body["option"]) if "option" in given else None
        head = str(body.get("head") or "")
        if head and option != "go":
            return self._send(400, "a head goes with a go only")
        acts = view.fold()["actionable"].get(item_id)
        if acts is None or not acts.allows(option, text, bool(body.get("discuss"))):
            return self._send(409, "the page does not offer that answer")
        if body.get("rule") and (not acts.get("rule") or body.get("discuss")):
            return self._send(409, "don't ask again goes with an answer to a decision")
        if option == "go" and (not acts.get("head") or head != acts["head"]):
            return self._send(409, "the PR moved on: look again")
        fields = {"discuss": True} if body.get("discuss") else {"text": text} if text is not None else {"option": option}
        if option == "go":
            fields["head"] = head
        if body.get("rule"):
            fields["rule"] = True
        record = items.answer(item_id, hides=bool(acts.get("hides")), **fields)
        return self._send(200, json.dumps({"seq": record["seq"]}), "application/json")

    def _show_session(self, body):
        """Rex's client action, which its Remote Control setting may refuse. Only a session on the server's own list."""
        sessions = caps.get("sessions")
        wanted = str(body.get("session") or "")
        if not sessions or not any(s["id"] == wanted and s["kind"] != "other" for s in sessions.snapshot().get("sessions") or []):
            return self._send(409, "that session isn't one of athena's")
        try:
            caps.CTX.backend.focus(wanted)
        except BackendError as exc:
            return self._send(409, f"Rex would not show it: {exc.message or exc.code}. Rex's Remote Control setting may be off.")
        return self._send(200, "{}", "application/json")

    def _item(self, items, body):
        item_id, what = str(body.get("item") or ""), body.get("do")
        if what == "done":
            items.dash_done(item_id)
        elif what == "snooze":
            items.snooze(item_id, str(body.get("until") or "1d"))
        elif what == "reopen":
            with store.locked("ask.lock"):
                data = items.load()
                item = items.find(data, item_id)
                item.pop("resolved", None)
                item.pop("resolved_note", None)
                items.save(data)
        else:
            return self._send(400, "unknown")
        return self._send(200, "{}", "application/json")


def _defaults_loop():
    items = caps.get("items")
    while items:
        time.sleep(60)
        try:
            items.apply_defaults()
        except Exception:
            pass


def _sessions_loop():
    """The list on the page stays current with no watch running, and no request ever waits on Rex."""
    sessions = caps.get("sessions")
    while sessions:
        try:
            sessions.refresh(caps.CTX)
        except Exception:
            pass
        time.sleep(sessions.REFRESH_EVERY)


def serve(port: int) -> int:
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"port {port} is in use: {exc}; set ATHENA_DASH_PORT")
        return 1
    httpd.daemon_threads = True
    threading.Thread(target=_defaults_loop, daemon=True).start()
    threading.Thread(target=_sessions_loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
