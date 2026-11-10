"""Talk to a worker's terminal, refusing when it cannot take input or someone is typing there."""
from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, ledger
from athena_rex.kernel.backend import BackendError

SUMMARY = "type a message or slash command into a worker, safely"


def record_of(name) -> dict:
    worker = ledger.get(name)
    if not worker or worker.get("state") != "live":
        raise UserError(f"no live worker named {name}")
    if not ledger.managed(worker):
        raise UserError(f"{name} is not on this backend; it belongs to the old athena")
    return {**worker, "name": name}


def _configure(p):
    p.add_argument("name", help="a worker, or --all: every live worker")
    p.add_argument("text", nargs="?")
    p.add_argument("--all", action="store_true", help="the name is then the text: a new rule, to everyone at once")
    p.add_argument("--no-submit", action="store_true", help="type it without pressing Enter")
    p.add_argument("--wait-box", type=float, default=0.0, help="seconds to wait for the input box to empty")


def broadcast(backend, text) -> dict:
    """One outcome per worker. Each is typed with the same care as a send, so someone's typing or a blocked worker refuses for itself."""
    out = {}
    for worker in ledger.live():
        if not ledger.managed(worker):
            out[worker["name"]] = "on the old athena: not reached"
            continue
        try:
            agents.send(backend, {**worker}, text)
            out[worker["name"]] = "sent"
        except agents.Refused as exc:
            out[worker["name"]] = f"refused: {exc}"
        except BackendError as exc:
            out[worker["name"]] = f"may not have arrived: {exc}"
    return out


def _run(args):
    if args.all:
        outcomes = broadcast(caps.CTX.backend, args.text or args.name)
        for name, outcome in sorted(outcomes.items()):
            print(f"{name:<16} {outcome}")
        return 0 if all(o == "sent" for o in outcomes.values()) else 3
    if args.text is None:
        raise UserError("athena-rex send <worker> <text>, or send --all <text>")
    try:
        agents.send(caps.CTX.backend, record_of(args.name), args.text, submit=not args.no_submit,
                    wait_box=args.wait_box)
    except agents.Refused as exc:
        print(f"refused: {exc}")
        return 3
    print("sent")
    return 0


def _keys_cfg(p):
    p.add_argument("name")
    p.add_argument("keys", nargs="+", help="key names: enter, esc, down, ctrl+u ...")


def _keys(args):
    caps.CTX.backend.press(agents.place_of(record_of(args.name)), *args.keys)
    return 0


def _screen_cfg(p):
    p.add_argument("name")


def _screen(args):
    print(caps.CTX.backend.screen(agents.place_of(record_of(args.name))), end="")
    return 0


def _nudge_cfg(p):
    p.add_argument("name")
    p.add_argument("text", nargs="+")


def _nudge(args):
    """The old athena's `nudge`: its words joined, refused while the worker's goal waits, and a bare slash command gets one more Enter,
    as the first only picks the command menu's suggestion."""
    import re
    import time
    record = record_of(args.name)
    if record.get("goal_pending"):
        raise UserError(f"{args.name} has not received its goal yet and may be at a startup prompt; the human answers that in its "
                        f"Rex session, then: athena-rex resume {args.name}")
    text = " ".join(args.text)
    try:
        agents.send(caps.CTX.backend, record, text)
    except agents.Refused as exc:
        print(f"refused: {exc}")
        return 3
    if re.fullmatch(r"/\S+", text):
        time.sleep(0.5)
        caps.CTX.backend.press(agents.place_of(record), "enter")
    print("sent")
    return 0


def commands():
    return {"send": caps.Command(SUMMARY, _configure, _run),
            "nudge": caps.Command("type a prompt or slash command into a worker (SendMessage for messages)", _nudge_cfg, _nudge),
            "keys": caps.Command("press keys in a worker's terminal", _keys_cfg, _keys),
            "screen": caps.Command("print what a worker's terminal shows now", _screen_cfg, _screen)}
