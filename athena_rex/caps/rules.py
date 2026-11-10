"""The standing orders a machine can check, in a pre-tool hook. It reads a command line as a shell would: each command of it, after `&&`, on a
later line, in a subshell or under `bash -c`, and quoted words are not commands. Deny rules in a profile only match how a line starts.
It guards against mistakes, and it is no sandbox: what a script does inside isn't on the line. Kills are the signal sandbox's
(kernel/sandbox.py); launchd, power and builds are the human's own settings, as in the old athena.
"""
import os
import re
import shlex

from athena_rex.kernel import caps

SUMMARY = "the standing orders a machine can check, held in a hook"
SEPARATORS = {";", "&", "&&", "|", "||", "(", ")", "\n", ";;", "|&"}
LEADS = {"sudo", "env", "nohup", "time", "exec", "command", "then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "nice", "caffeinate"}
SHELLS = {"sh", "bash", "zsh", "dash"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _words(line) -> list:
    """Each command of the line, as a list of words. A line a shell couldn't read either, as with a heredoc, is taken word by word per line."""
    lexer = shlex.shlex(line.replace("`", " ; "), posix=True, punctuation_chars="();&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = []
        for row in line.splitlines():
            tokens += row.split() + ["\n"]
    commands, current = [], []
    for token in tokens:
        if token in SEPARATORS or set(token) <= set("();&|\n"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    return commands + ([current] if current else [])


def _strip_leads(words) -> list:
    at = 0
    while at < len(words) and (words[at] in LEADS or _ASSIGN.match(words[at])):
        at += 1
    return words[at:]


def _git(args) -> str:
    at = 0
    while at < len(args) and args[at].startswith("-"):
        at += 2 if args[at] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    if at >= len(args) or args[at] != "push":
        return ""
    options = [word for word in args[at + 1:] if word.startswith("-") and word != "--"]
    positional = [word for word in args[at + 1:] if not word.startswith("-")]
    if "--force" in options or any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", word) for word in options) or any(word.startswith("+") for word in positional):
        return "no force-push. After a rebase the chief of staff asked for, `git push --force-with-lease`"
    if any(word.rsplit(":", 1)[-1].replace("refs/heads/", "") in ("main", "master") for word in positional[1:]):
        return "never push the default branch. Push your own and open a PR"
    return ""


def _one(words) -> str:
    words = _strip_leads(words)
    if not words:
        return ""
    program, args = os.path.basename(words[0]), words[1:]
    if program in SHELLS and "-c" in args[:-1]:
        return refused(args[args.index("-c") + 1])
    if program == "eval":
        return refused(" ".join(args))
    if program == "git":
        return _git(args)
    if program == "gh" and args[:2] == ["pr", "merge"] and "--admin" in args:
        return "never --admin: it goes round the reviews"
    if program == "rex" and "server" in args and "stop" in args and "-S" not in args:
        return "that stops every session on the server, the human's included"
    return ""


def refused(command) -> str:
    """Why this command may not run, or an empty string."""
    text = str(command or "")
    for words in _words(text):
        why = _one(words)
        if why:
            return why
    return ""


def answer(event) -> dict:
    """The hook's reply to a PreToolUse event, or nothing."""
    if event.get("tool_name") != "Bash":
        return {}
    why = refused((event.get("tool_input") or {}).get("command"))
    if not why:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": f"athena rule: {why}"}}


def commands():
    return {}
