#!/bin/sh
# Stops each dashboard whose state dir is inside the folder given: dash.on() starts one in a session of its own, so a test run or a
# rehearsal cut short left it serving. Found only by its pid file there, <state>/run/dash.json; only a pid over 1,
# one pid at a time and never a group; and only a process that is a dash.
[ -d "${1:-}" ] || exit 0
ps=${ATHENA_STOP_DASHES_PS:-/bin/ps} # the test sandbox can't run the real one, a setuid program
find "$1" -type f -path '*/run/dash.json' 2>/dev/null | while IFS= read -r file; do
	pid=$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$file" | head -n 1)
	case "$pid" in '' | *[!0-9]*) continue ;; esac
	[ "$pid" -gt 1 ] || continue
	command=$("$ps" -ww -o command= -p "$pid" 2>/dev/null) || continue
	case "$command" in
	*athena*' dash run') kill -TERM "$pid" 2>/dev/null ;;
	esac
done
exit 0
