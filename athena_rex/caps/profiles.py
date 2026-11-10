"""What each role may do without asking: a permission profile per role, generated and diffed by `install`, and put in each session's
`--settings`. Standing grants are rules here, not memories.
"""
import difflib
import json

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, store

SUMMARY = "what each role may do without asking"
READS = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git branch:*)", "Bash(git fetch:*)",
         "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh pr list:*)", "Bash(gh run view:*)"]
ROLES = {
    "worker": {"allow": READS + ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)", "Bash(git rebase:*)", "Bash(gh pr create:*)",
                                 "Bash(athena-rex heavy:*)", "Bash(athena-rex whoami:*)", "Bash(aws sso login:*)", "Bash(docker run --rm:*)"],
               "deny": []},
    "hq": {"allow": READS + ["Bash(athena-rex:*)", "Bash(gh pr merge --match-head-commit:*)"],
           "deny": []},  # no deny lists: the human's own settings and auto mode decide, as in the old athena
}


def _file(role):
    return store.path("profiles", f"{role}.json")


def built(role) -> dict:
    """What ships, with the human's grants from config. A grant adds to allow; nothing here can take a deny away."""
    if role not in ROLES:
        raise UserError(f"no role {role}: {', '.join(ROLES)}")
    grants = (config.rex("grants") or {}).get(role) or []
    extra = list(config.get("hq_allow") or []) if role == "hq" else []
    allow = list(dict.fromkeys(ROLES[role]["allow"] + list(grants) + extra))
    return {"allow": allow, "deny": list(ROLES[role]["deny"])}


def permissions(role) -> dict:
    """As generated, else as built. A deny written by an earlier build is dropped: there are none now."""
    written = store.read_dict(_file(role))
    fresh = built(role)
    if not written:
        return fresh
    return {"allow": list(dict.fromkeys((written.get("allow") or []) + fresh["allow"])), "deny": fresh["deny"]}


def _text(profile) -> list:
    return json.dumps(profile, indent=1, sort_keys=True).splitlines()


def diff(role) -> list:
    before = store.read_dict(_file(role))
    if not before:
        return []
    return [line for line in difflib.unified_diff(_text(before), _text(built(role)), f"{role} now", f"{role} after", lineterm="")]


def write_all() -> dict:
    """{role: what changed}. A first write has no lines to show, and says so with None."""
    out = {}
    for role in ROLES:
        first = not _file(role).exists()
        out[role] = None if first else diff(role)
        store.write_text(_file(role), json.dumps(built(role), indent=1, sort_keys=True) + "\n")
    return out


def grant(role, rule):
    data = store.read_dict(config.config_file())
    rex = data.setdefault("rex", {})
    rules = rex.setdefault("grants", {}).setdefault(role, [])
    if rule not in rules:
        rules.append(rule)
    store.write_json(config.config_file(), data, indent=2)
    config.reset()
    store.write_text(_file(role), json.dumps(built(role), indent=1, sort_keys=True) + "\n")


def _configure(p):
    p.add_argument("action", nargs="?", choices=["show", "diff", "grant"], default="show")
    p.add_argument("role", nargs="?", choices=list(ROLES))
    p.add_argument("rule", nargs="?", help="grant: a Claude permission rule, such as 'Bash(aws sso login:*)'")


def _run(args):
    if args.action == "grant":
        if not (args.role and args.rule):
            raise UserError("athena-rex profiles grant <role> '<rule>'")
        grant(args.role, args.rule)
        print(f"{args.role} may now: {args.rule}. A session started from now on has it.")
        return 0
    for role in ([args.role] if args.role else ROLES):
        if args.action == "diff":
            print("\n".join(diff(role)) or f"{role}: no change")
        else:
            print(json.dumps({role: permissions(role)}, indent=1))
    return 0


def commands():
    return {"profiles": caps.Command(SUMMARY, _configure, _run)}
