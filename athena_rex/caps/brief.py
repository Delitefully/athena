"""Checks on a brief and goal before spawn: wording a goal evaluator could never call met. Heuristics, so --skip-brief-check overrides."""
import os
import re

from athena_rex.kernel import caps

SUMMARY = "check a brief and goal for wording that can never be met"
FIELDS = ("GOAL", "SCOPE", "CONTEXT", "ACCEPTANCE", "VERIFY", "HUMAN-CHECKS", "TIMEBOX", "FORBIDDEN", "NOTES", "PREFERENCES")
REQUIRED = ("GOAL", "SCOPE", "ACCEPTANCE", "VERIFY", "FORBIDDEN")
_FIELD = re.compile(r"^\s*(?:\*\*)?(" + "|".join(map(re.escape, FIELDS)) + r")(?: \([^)\n]*\))?:(?:\*\*)?[ \t]*(.*)$")
_OUTCOME = re.compile(r"^\W*ship\b|\b(?:is|are|be|been|gets?) shipped\b|\b(?:the|its|this|each|every|a) PRs? "
                      r"(?:is|are|be|gets?|has been|have been) (?:merged|approved)\b|\bmerge[- ]ready\b|"
                      r"\bready (?:to|for)[\w' ]{0,25}merge\b|\bnon-draft PR\b|\bundraft", re.I)
_HUMAN = re.compile(r"\b(?:the human|you) (?:signs? in|clicks?|approves?|answers?)\b|\bask athena\b|\bsign in\b", re.I)
_REMOTE = re.compile(r"\bgh pr checks\b|\bwait(?:s|ing)? (?:for|on) CI\b|\bCI (?:is )?(?:green|passes|passing)\b", re.I)
_PLACEHOLDER = re.compile(r"^\s*(?:tbd|todo|n/?a|none|manual(?:ly)?\b.*|<[^>]*>)\s*$", re.I)
_DELIVERY = re.compile(r"\bSendMessage\b.{0,60}\b(?:succeed|received|delivered|reaches?|must)\b|\bmust\b.{0,40}\bSendMessage\b", re.I)


def parse(text: str) -> dict:
    """{field: [(line number, text)]}"""
    out, current = {}, None
    for number, line in enumerate(re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines(), 1):
        found = _FIELD.match(line)
        if found:
            current = found.group(1)
            out.setdefault(current, [])
            if found.group(2).strip():
                out[current].append((number, found.group(2).strip()))
        elif current and line.strip():
            out[current].append((number, line.strip()))
    return out


def check(text: str) -> list:
    fields, found = parse(text), []
    for name in REQUIRED:
        if not fields.get(name):
            found.append(f"{name}: missing or empty")
    for name in ("GOAL", "ACCEPTANCE"):
        for number, line in fields.get(name, []):
            if _OUTCOME.search(line):
                found.append(f"line {number} {name}: asks for what only others do after DONE: {line[:80]}")
    for number, line in fields.get("ACCEPTANCE", []):
        body = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line)
        if _HUMAN.search(body):
            found.append(f"line {number} ACCEPTANCE: only the human can do this; move it to HUMAN-CHECKS: {body[:80]}")
        elif len(body.split()) < 3:
            found.append(f"line {number} ACCEPTANCE: too loose to show met: {body[:80]}")
    for number, line in fields.get("VERIFY", []):
        if _REMOTE.search(line):
            found.append(f"line {number} VERIFY: waits on CI, not a command that passes in the worktree: {line[:80]}")
        elif _PLACEHOLDER.match(line):
            found.append(f"line {number} VERIFY: a placeholder or manual step: {line[:80]}")
    return found


def check_goal(goal: str) -> list:
    found = []
    if _OUTCOME.search(goal or ""):
        found.append("goal: asks for what only others do after DONE")
    if _DELIVERY.search(goal or ""):
        found.append("goal: needs a SendMessage to succeed; the ATHENA-REPORT line is the report")
    return found


def timebox_seconds(text):
    """The seconds a brief's TIMEBOX line gives, or None. Turns aren't counted here; a time is what a watch can hold it to."""
    found = re.search(r"^\W*TIMEBOX\W+(\d+(?:\.\d+)?)\s*(h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?)\b", text or "", re.M | re.I)
    if not found:
        return None
    return int(float(found.group(1)) * (3600 if found.group(2).lower().startswith("h") else 60))


def problems(path, goal="") -> list:
    with open(os.path.expanduser(str(path))) as f:
        return check(f.read()) + check_goal(goal)


def _configure(p):
    p.add_argument("action", choices=["check"])
    p.add_argument("file")
    p.add_argument("--goal", default="")


def _run(args):
    found = problems(args.file, args.goal)
    for line in found:
        print(line)
    if not found:
        print("brief ok")
    return 1 if found else 0


def commands():
    return {"brief": caps.Command(SUMMARY, _configure, _run)}
