#!/bin/bash
# The only way to run the tests: inside an OS sandbox that holds for every child process, Python or not. tests/harness.py
# refuses to run outside it. It denies writes under the real HOME, reads of live state and of Claude's own files, the
# card's volume, the real tools, the network bar loopback, every Unix socket but its own folder's, and signals to anything
# not started in here.
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
home=$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | /usr/bin/awk '{print $2}')
base=/private/tmp/claude-$(/usr/bin/id -u)
mkdir -p "$base"
run=$(mktemp -d "${TMPDIR:-$base}/ar-test.XXXXXX") || exit 1
run=$(cd "$run" && pwd -P)
probe_write="$base/.ar-sandbox-probe.$$"
sockets=$(mktemp -d "$base/s.XXXXXX") || exit 1
probe_exec="$base/.ar-sandbox-exec.$$"
printf '#!/bin/sh\ntouch "$1"\n' >"$probe_exec"
chmod 700 "$probe_exec"
sleep 3600 &
outsider=$!
cleanup() {
	kill "$outsider" 2>/dev/null
	sh "$here/scripts/stop-dashes.sh" "$run" # a dash starts detached, so a run cut short left it serving; only this run's own
	rm -rf "$run" "$sockets" "$probe_write" "$probe_exec"
}
trap cleanup EXIT INT TERM

# Seatbelt matches the program's resolved path, so a rule on a symlink like Homebrew's gh matches nothing.
tools="(subpath \"/opt/homebrew/Cellar/gh\") (subpath \"/usr/local/Cellar/gh\")"
for tool in /usr/bin/security /usr/bin/osascript /usr/bin/pmset /usr/sbin/ioreg /usr/bin/caffeinate /bin/launchctl /usr/bin/open \
	/opt/homebrew/bin/gh /usr/local/bin/gh; do
	tools="$tools (literal \"$tool\")"
	if [ -e "$tool" ]; then
		tools="$tools (literal \"$(/usr/bin/readlink -f "$tool")\")"
	fi
done
live="(subpath \"$home/.local/state/athena\") (subpath \"$home/Library/Application Support/athena\")"
profile="(version 1)
(allow default)
(deny file-write* (subpath \"$home\") (subpath \"/Volumes\") (literal \"$probe_write\"))
(allow file-write* (subpath \"$here\") (subpath \"$run\"))
(deny file-read-data (subpath \"/Volumes\") (subpath \"$home/.claude\") (literal \"$home/.claude.json\") $live)
(deny process-exec $tools (literal \"$probe_exec\") (subpath \"/Applications/Rex Beta.app\") (subpath \"$home/.local/share/claude\") (literal \"$home/.local/bin/claude\"))
(deny signal)
(allow signal (target same-sandbox))
(deny network-outbound)
(allow network-outbound (remote ip \"localhost:*\"))
(allow network-outbound (subpath \"$sockets\"))
(deny network-outbound (remote ip \"localhost:2843\"))"

printf '%s\n' "$profile" >"$run/profile.sb"
# A private modules folder's own tests run too (tests/test_private_modules.py); the folder is only read.
private_modules=${ATHENA_REX_MODULES_DIR:-$home/.config/athena-rex/modules}
[ -d "$private_modules" ] || private_modules=
env -u ATHENA_STATE_DIR -u ATHENA_WORKER -u ATHENA_BRIEF -u ATHENA_HQ TMPDIR="$run" ATHENA_TEST_MODULES_DIR="$private_modules" \
	ATHENA_SANDBOX_PROBE_WRITE="$probe_write" ATHENA_SANDBOX_PROBE_EXEC="$probe_exec" ATHENA_SANDBOX_OUTSIDER="$outsider" \
	ATHENA_SANDBOX_PROFILE="$run/profile.sb" ATHENA_SANDBOX_SOCKETS="$sockets" \
	/usr/bin/sandbox-exec -f "$run/profile.sb" /usr/bin/python3 -B -W ignore -m unittest discover -s "$here/tests" "$@"
