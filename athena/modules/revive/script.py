"""A launch script, <state>/run/<name>.sh as kernel/agents.py's `launcher` writes it, made to resume a conversation.

Its exec line gets `--resume <id>` in front of Claude's own flags, and any resume it already had is dropped. Every other word, and
every other line (the pid it records, the folder it starts in), stays as it was, byte for byte: these are its exact flags.

A Pi worker's launch already names its conversation (`--session-id`, chosen at spawn), and starting it again continues that
conversation, so its script is run as it is: Pi has no `--resume <id>`.
"""
import shlex

PID_LINE = "echo $$ > "
EXEC = "exec "
FLAGS_START = ("--name", "--resume")  # Claude's own flags start with one of these, after any sandbox-exec, env and the claude words


class NotLaunch(ValueError):
    """The text isn't athena's launch of this Claude, so nothing is resumed from it."""


def _without_resume(flags) -> list:
    out, words = [], iter(flags)
    for word in words:
        if word == "--resume":
            next(words, None)
        elif not word.startswith("--resume="):
            out.append(word)
    return out


def resumed(text: str, name: str, session: str) -> str:
    """The script that starts `name`'s Claude on `session`, or NotLaunch with why not."""
    lines = text.splitlines(keepends=True)
    if not any(line.startswith(PID_LINE) for line in lines):
        raise NotLaunch("it records no pid")
    at = [i for i, line in enumerate(lines) if line.startswith(EXEC)]
    if len(at) != 1:
        raise NotLaunch(f"it has {len(at)} exec lines, not one")
    try:
        words = shlex.split(lines[at[0]][len(EXEC):])
    except ValueError as exc:
        raise NotLaunch(f"its exec line doesn't parse: {exc}")
    start = next((i for i, word in enumerate(words) if word in FLAGS_START or word.startswith("--resume=")), None)
    if start is None:
        raise NotLaunch("its exec line has no Claude flags")
    head, flags = words[:start], _without_resume(words[start:])
    if not any(word == "--name" and following == name for word, following in zip(flags, flags[1:])):
        raise NotLaunch(f"it doesn't launch --name {name}")
    if "--session-id" in flags:  # Pi: the same launcher brings back the same conversation
        if not any(word == "--session-id" and following == session for word, following in zip(flags, flags[1:])):
            raise NotLaunch(f"its Pi launch is for another conversation than {session}")
        return text
    lines[at[0]] = EXEC + " ".join(shlex.quote(word) for word in [*head, "--resume", session, *flags]) + "\n"
    return "".join(lines)
