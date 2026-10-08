#!/bin/sh
# Integration lab: an isolated herdr server, a sandbox repo with a bare origin,
# and real Claude workers (Haiku). Never touches the user's live herdr session.
#
#   tests/lab/lab.sh up        start the lab server, sandbox repo and a fake HQ pane
#   tests/lab/lab.sh env       print the env exports for running athena against the lab
#   tests/lab/lab.sh trust     trust the sandbox repo once in Claude (answers the dialog)
#   tests/lab/lab.sh down      stop the lab server and delete everything it made
set -eu
here=$(cd "$(dirname "$0")/../.." && pwd)
LAB=${ATHENA_LAB:-/private/tmp/claude-501/cl}
SANDBOX="$HOME/Developer/.athena-lab-sandbox"
WTROOT="$HOME/Developer/.athena-lab-wt"
CLAUDE_DIR=$(dirname "$(command -v claude)")

h() {
	env -i HOME="$HOME" USER="$USER" TERM=xterm-256color \
		PATH="$CLAUDE_DIR:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
		XDG_CONFIG_HOME="$LAB/cfg" XDG_STATE_HOME="$LAB/xstate" herdr --session athena-lab "$@"
}

write_wrapper() {
	cat >"$LAB/h" <<EOF
#!/bin/sh
exec env -i HOME="$HOME" USER="$USER" TERM=xterm-256color PATH="$CLAUDE_DIR:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" XDG_CONFIG_HOME="$LAB/cfg" XDG_STATE_HOME="$LAB/xstate" herdr --session athena-lab "\$@"
EOF
	chmod +x "$LAB/h"
}

case "${1:-}" in
up)
	mkdir -p "$LAB/cfg/herdr" "$LAB/xstate" "$LAB/state"
	printf '[server]\nheadless_cols = 380\nheadless_rows = 50\n' >"$LAB/cfg/herdr/config.toml"
	write_wrapper
	(cd "$LAB" && nohup "$LAB/h" server >"$LAB/server.log" 2>&1 &)
	sleep 3
	"$LAB/h" status server | grep -q "$LAB" || { echo "lab server socket not under $LAB" >&2; exit 1; }
	if [ ! -d "$SANDBOX" ]; then
		git init -q --bare -b main "$LAB/origin.git"
		git init -q -b main "$SANDBOX"
		git -C "$SANDBOX" config user.email lab@example.com
		git -C "$SANDBOX" config user.name lab
		printf '# sandbox\n' >"$SANDBOX/README.md"
		git -C "$SANDBOX" add README.md
		git -C "$SANDBOX" commit -q -m init
		git -C "$SANDBOX" remote add origin "$LAB/origin.git"
		git -C "$SANDBOX" push -q -u origin main
		git -C "$SANDBOX" remote set-head origin main
	fi
	hq=$("$LAB/h" workspace create --cwd "$LAB" --label hq | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')
	echo "$hq" >"$LAB/hq_pane"
	echo "lab up: hq pane $hq"
	;;
env)
	cat <<EOF
export ATHENA_HERDR="$LAB/h" ATHENA_STATE_DIR="$LAB/state" ATHENA_WORKTREE_ROOT="$WTROOT" \\
  ATHENA_HQ_PANE="$(cat "$LAB/hq_pane")" ATHENA_PLUGIN_DIR="$here" ATHENA_MAX_WORKERS=4
EOF
	;;
trust)
	ws=$("$LAB/h" workspace create --cwd "$SANDBOX" --label trust | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')
	"$LAB/h" agent start trust --kind claude --pane "$ws" --timeout 60000 -- --model haiku >/dev/null 2>&1 || true
	sleep 2
	"$LAB/h" pane send-keys "$ws" down >/dev/null; sleep 0.5; "$LAB/h" pane send-keys "$ws" enter >/dev/null
	sleep 8
	"$LAB/h" agent prompt trust "/exit" >/dev/null 2>&1 || true
	sleep 2
	"$LAB/h" pane read "$ws" | grep -v '^\s*$' | tail -5
	;;
down)
	[ -x "$LAB/h" ] && "$LAB/h" server stop >/dev/null 2>&1 || true
	sleep 1
	if [ -d "$SANDBOX" ]; then git -C "$SANDBOX" worktree prune 2>/dev/null || true; fi
	rm -rf "$SANDBOX" "$WTROOT" "$LAB"
	echo "lab down"
	;;
*)
	sed -n 2,10p "$0"
	exit 2
	;;
esac
