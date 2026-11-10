#!/bin/bash
# The installer end to end in an isolated HOME, with launchd a stub, and a check that the real ~/.claude, ~/.local/bin and launchd did
# not change. Prints the whole transcript. scripts/install-transcript.sh
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
real=$(eval echo "~$(id -un)")
root=/private/tmp/claude-$(id -u)/ari$$
mkdir -p "$root/home" "$root/stubs" "$root/agents"
trap 'rm -rf "$root"' EXIT
touch "$root/home/.athena-scratch" "$root/stubs/.athena-stubs"
printf '#!/bin/sh\necho "launchctl $*" >> "%s/launchctl.log"\n' "$root" >"$root/stubs/launchctl"
chmod +x "$root/stubs/launchctl"

fingerprint() {
	{
		stat -f '%m %z %N' "$real/.claude/settings.json" "$real/.claude/settings.local.json" 2>/dev/null
		shasum -a 256 "$real/.claude/settings.json" 2>/dev/null
		ls -lT "$real/.local/bin" 2>/dev/null
		ls -lT "$real/Library/LaunchAgents" 2>/dev/null
		ls -d "$real/.local/share/athena-rex" "$real/Library/Application Support/athena" 2>/dev/null
	} | shasum -a 256
}
before=$(fingerprint)

iso() { env -u ATHENA_STATE_DIR -u ATHENA_CONFIG HOME="$root/home" ATHENA_WORKER=rex-transcript ATHENA_WORLD_STUBS="$root/stubs" ATHENA_LAUNCH_AGENTS="$root/agents" \
	ATHENA_DASH_PORT=2891 PATH="$root/home/.local/bin:$PATH" "$@"; }
step() { printf '\n$ %s\n' "$*"; iso "$@"; code=$?; echo "[exit $code]"; [ "$code" = "${want:-0}" ] || failed=1; }
failed=0

echo "isolated HOME: $root/home"
step "$here/bin/athena-rex" install --dry-run
step "$here/bin/athena-rex" install
echo; echo "\$ ls ~/.local/bin ~/.local/share/athena-rex ~/.local/share/athena-rex/releases"
ls "$root/home/.local/bin" "$root/home/.local/share/athena-rex" "$root/home/.local/share/athena-rex/releases"
step athena-rex --help
step athena-rex install --check
step "$here/bin/athena-rex" install
step "$here/bin/athena-rex" install --services
echo; echo "\$ launchctl calls recorded by the stub"; cat "$root/launchctl.log" | sed "s|$root|<root>|g"
step athena-rex profiles show worker
step athena-rex install --uninstall
echo; echo "\$ what is left under the isolated HOME"
(cd "$root/home" && find . -maxdepth 4 -not -path './Library/Application Support/athena/*' | sort)
echo "launchd plists left: $(ls "$root/agents" | wc -l | tr -d ' ')"

after=$(fingerprint)
echo
echo "real ~/.claude settings, ~/.local/bin and ~/Library/LaunchAgents (launchd itself is never asked):"
echo "  before $before"
echo "  after  $after"
if [ "$before" = "$after" ]; then echo "  unchanged"; else echo "  CHANGED"; failed=1; fi
test ! -e "$real/.local/bin/athena-rex" && echo "  no athena-rex in the real ~/.local/bin"
test ! -e "$real/.local/share/athena-rex" && echo "  no ~/.local/share/athena-rex in the real home"
[ "$failed" = 0 ] && echo "transcript: pass" || { echo "transcript: FAILED"; exit 1; }
