"""Checks on a worker's brief and goal before spawn: wording the goal evaluator could never call met.

The evaluator reads the goal and the brief literally. Most of its "not met" loops came from a few kinds of wording,
each of which a worker cannot get past on its own:

- outcome: GOAL or ACCEPTANCE asks for what only others do after the worker's DONE (ship, merge, approval, undraft,
  deploy). The worker reports a draft PR; HQ reviews, undrafts and gets it merged.
- human: an ACCEPTANCE item needs the human (a sign-in, a click, an answer). It goes under HUMAN-CHECKS, which HQ
  batches for the human once the worker is done, so the worker is never blocked on it.
- vague: an ACCEPTANCE item too short and loose to show met ("Docs updated.").
- remote / unrunnable: VERIFY waits on CI or a PR's checks, or is a placeholder or a manual step, rather than
  commands that run and pass in the worktree.
- delivery: the goal needs a SendMessage to succeed. The ATHENA-REPORT line is the report; a dead socket once turned
  this clause into 784 "not met" verdicts.
- missing: a required field is absent or empty.

CONTEXT, FORBIDDEN, NOTES and HUMAN-CHECKS are never linted: "never merge" belongs there.
Each problem names its rule (`kind`) and, but for `missing`, the brief's line. Heuristics, so
`athena spawn --skip-brief-check` overrides them when one is wrong for a brief, and the ledger records that it did.
"""
import re

FIELDS = ("GOAL", "SCOPE", "CONTEXT", "ACCEPTANCE", "VERIFY", "HUMAN-CHECKS", "TIMEBOX", "FORBIDDEN", "NOTES",
          "PREFERENCES")
REQUIRED = ("GOAL", "SCOPE", "ACCEPTANCE", "VERIFY", "FORBIDDEN")

# `FIELD:`, `**FIELD:**`, or `FIELD (a note):` at the start of a line.
_FIELD = re.compile(r"^\s*(?:\*\*)?(" + "|".join(re.escape(f) for f in FIELDS) +
                    r")(?: \([^)\n]*\))?:(?:\*\*)?[ \t]*(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$")

# What only others do after the worker's DONE, as the state a goal asks for: "Ship X", "is merged", "ready for review
# and merge", "a non-draft PR". Not a feature that mentions merging ("merge rules", "a mod that ships in the plugin")
# nor someone else's ("after #12 merged").
OUTCOME = (
    re.compile(r"^\W*ship\b|\b(?:is|are|be|been|gets?) shipped\b", re.I),
    re.compile(r"\b(?:the|its|this|each|every|both|a) PRs? (?:is|are|be|gets?|has been|have been) (?:merged|approved)\b|"
               r"\bPR #\d+ (?:is|gets?|has been) (?:merged|approved)\b|\bmerge[- ]ready\b|"
               r"\bready (?:to|for)[\w' ]{0,25}merge\b", re.I),
    re.compile(r"\bland(?:s|ed)? (?:on|in) (?:main|master|the default branch)\b", re.I),
    re.compile(r"\b(?:deploy(?:ed|s)?|released?) (?:to|on|in) (?:prod|production|staging|users)\b", re.I),
    re.compile(r"\bapprov(?:al|ed) by\b", re.I),
    re.compile(r"\b(?:a|the|both)(?: PRs?)?(?: is| are)? non-draft\b|\bnot (?:as )?a draft\b|"
               r"\b(?:is|are|be|marked?|opened?|open (?:it|the PR))\s+ready for[\w' ]{0,12} review\b", re.I),
)
OUTCOME_FIX = ("say what the worker produces and shows: commits, a draft PR, VERIFY passing. Merging, approvals, "
               "undrafting and deploys happen after HQ's review; the evaluator holds the worker to them and it loops")

HUMAN = (  # the human as the one who does a step of the acceptance, not a feature that talks to the human
    re.compile(r"\bthe human (?:signs?|logs?|approves?|confirms?|clicks?|verif(?:y|ies)|reconnects?|connects?|uploads?)\b",
               re.I),
    re.compile(r"\b(?:ask|have|wait for|waits for) (?:athena|hq|the human)\b|\bNEED:", re.I),
)
HUMAN_FIX = ("move it to HUMAN-CHECKS: HQ batches those for the human once the worker is done, so the worker never "
             "waits on them")

VAGUE = re.compile(r"\b(?:updated|works?|working|done|clean|good|better|improved|fixed|handled|correct(?:ly)?|"
                   r"appropriate(?:ly)?|polished|nice|properly|as needed|where possible|etc)\b", re.I)
ANCHOR = re.compile(r"`|/|\d|\.[A-Za-z]{1,5}\b|\btests?\b|--|\"")
VAGUE_FIX = "say what must be true and how it shows: which file says what, which command, which output"

# Waiting on GitHub's checks, not running commands: "CI green", "the CI results", `gh pr checks`. A command that runs
# "as CI does" is fine.
REMOTE = re.compile(r"\bgh pr checks\b|\bgh run (?:view|watch)\b|\bCI (?:is |are )?(?:green|passes|passing|results?)\b|"
                    r"\bthe CI results?\b|\bCI [\w ]{0,20}step result\b|\bchecks? (?:are |is )?green\b", re.I)
REMOTE_FIX = ("VERIFY is commands the worker runs and passes in its worktree; CI and PR checks are part of DONE and "
              "HQ checks them")
# A placeholder for the whole VERIFY (the template's own), or a step no command can do. `<n>` or `<your worktree>`
# inside a command is the worker's to fill in.
UNRUNNABLE = re.compile(r"^\s*<[^<>\n]{1,80}>\s*$|<the exact command|\bTBD\b|\bTODO\b|\bmanual(?:ly)?\b|\bby hand\b|"
                        r"\bin (?:the|a|your) browser\b", re.I)
UNRUNNABLE_FIX = ("name the exact commands, which must already pass on main apart from what this work adds; leave "
                  "out checks that fail there")

DELIVERY = re.compile(r"\bSendMessage\b|\breport(?:ed|ing)? to (?:the session named )?(?:athena|hq)\b", re.I)
DELIVERY_FIX = ("drop it: the ATHENA-REPORT line is the report, and a SendMessage that cannot be delivered would keep "
                "the goal unmet")


def _uncommented(text) -> str:
    """The text without HTML comments, each comment's newlines kept so line numbers stay the file's."""
    return _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text or "")


def _line_of(lines, problem):
    """The 1-based line of the file where a problem's words are, or None."""
    words = problem.get("match") or ""
    head = " ".join(problem.get("text", "").split())[:40]
    for needle in (head, words):
        if not needle:
            continue
        for i, line in enumerate(lines, 1):
            if needle in " ".join(line.split()):
                return i
    field = problem.get("field")
    for i, line in enumerate(lines, 1):
        found = _FIELD.match(line)
        if found and found.group(1) == field:
            return i
    return None


def parse(text) -> dict:
    """The brief's fields, each with its text (stripped), in the order they appear. HTML comments are dropped."""
    fields, current = {}, None
    for line in _uncommented(text).splitlines():
        found = _FIELD.match(line)
        if found:
            current = found.group(1)
            fields[current] = [found.group(2)]
        elif current:
            fields[current].append(line)
    return {k: "\n".join(v).strip() for k, v in fields.items()}


def items(text) -> list:
    """A field's list items, a wrapped item joined into one line; a field with no list is one item."""
    out = []
    for line in (text or "").splitlines():
        found = _BULLET.match(line)
        if found:
            out.append(found.group(1).strip())
        elif line.strip() and out:
            out[-1] += " " + line.strip()
        elif line.strip():
            out.append(line.strip())
    return [i for i in out if i]


def _problem(field, kind, text, fix, match=None):
    """`match`: the words that tripped the check, so the message can quote them in place."""
    out = {"field": field, "kind": kind, "text": text, "fix": fix}
    if match:
        out["match"] = match
    return out


def _first(patterns, text):
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None


# Someone else's, or a feature's condition, not the goal's state: "after #12 merged", "PRs that are merged", "when
# the PR is approved".
_SOMEONE_ELSES = re.compile(r"(?:#\d+|\balready|\bwas|\bwere|\bbeen|\bjust)\s*$|"
                            r"\b(?:when|once|after|until|unless|if|before|whether|that|which|whose)\b[^.;:]*$", re.I)


def _outcome(text):
    """The first words in `text` that ask for an outcome after DONE, or None."""
    for pattern in OUTCOME:
        for m in pattern.finditer(text):
            if not _SOMEONE_ELSES.search(text[:m.start()]):
                return m.group(0)
    return None


def _outcomes(field, texts):
    return [_problem(field, "outcome", t, OUTCOME_FIX, word) for t in texts if (word := _outcome(t))]


def _vague(item) -> bool:
    if item.endswith(":") or ANCHOR.search(item):
        return False
    return len(item.split()) <= 6 and bool(VAGUE.search(item))


def check(text) -> list:
    """The problems in a brief, each {"field", "kind", "text", "fix"}; [] when it passes."""
    fields = parse(text)
    found = [_problem(f, "missing", "", f"add {f}: the goal holds the worker to it")
             for f in REQUIRED if not fields.get(f)]
    found += _outcomes("GOAL", [fields["GOAL"]] if fields.get("GOAL") else [])
    acceptance = items(fields.get("ACCEPTANCE"))
    found += _outcomes("ACCEPTANCE", acceptance)
    found += [_problem("ACCEPTANCE", "human", i, HUMAN_FIX, w) for i in acceptance if (w := _first(HUMAN, i))]
    found += [_problem("ACCEPTANCE", "vague", i, VAGUE_FIX) for i in acceptance if _vague(i)]
    verify = fields.get("VERIFY") or ""
    if verify and (w := _first([REMOTE], verify)):
        found.append(_problem("VERIFY", "remote", verify, REMOTE_FIX, w))
    if verify and (w := _first([UNRUNNABLE], verify)):
        found.append(_problem("VERIFY", "unrunnable", verify, UNRUNNABLE_FIX, w))
    lines = _uncommented(text).splitlines()
    for problem in found:
        line = _line_of(lines, problem) if problem["kind"] != "missing" else None
        if line:
            problem["line"] = line
    return found


def check_goal(condition) -> list:
    """The problems in the /goal condition HQ wrote (before athena's own suffix)."""
    text = " ".join((condition or "").split())
    found = _outcomes("goal", [text] if text else [])
    if (w := _first([DELIVERY], text)):
        found.append(_problem("goal", "delivery", text, DELIVERY_FIX, w))
    return found


def describe(problem, width=100) -> str:
    """One line: the field, the kind, the text quoted around the words that tripped it, and the fix."""
    text = " ".join(problem["text"].split())
    start = max(text.find(problem.get("match") or "\0"), 0)
    begin = max(0, min(start - width // 3, len(text) - width))
    quoted = text[begin:begin + width]
    quoted = ("…" if begin else "") + quoted + ("…" if begin + width < len(text) else "")
    where = f"line {problem['line']}: " if problem.get("line") else ""
    return (f"{where}{problem['field']} ({problem['kind']})" + (f' "{quoted}"' if text else "") +
            f": {problem['fix']}")
