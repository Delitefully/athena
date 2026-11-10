#!/bin/bash
# Builds the menu bar app with swiftc: menubar/build/AthenaRexMenu.app, or <out>/AthenaRexMenu.app when given a folder.
# Nothing here is installed or started: `athena-rex services install` makes it a login item, which runs `athena-rex menubar run`.
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
if [ -n "${SWIFTC:-}" ]; then swiftc=("$SWIFTC"); else swiftc=(/usr/bin/xcrun swiftc); fi
if ! "${swiftc[@]}" --version >/dev/null 2>&1; then  # before anything is made or removed
	echo "build.sh: swiftc not found: install Xcode or its command line tools (xcode-select --install)" >&2
	exit 3
fi
out=${1:-"$here/build"}
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
app="$out/AthenaRexMenu.app"
scratch=$(mktemp -d "$out/.build.XXXXXX")  # beside the app, so the swap is a rename on one volume
trap 'rm -rf "$scratch"' EXIT

# Everything is made in scratch, and the app in place is replaced only once the new one is whole: a failed build leaves
# the last good app running and installable.
new="$scratch/AthenaRexMenu.app"
mkdir -p "$new/Contents/MacOS" "$new/Contents/Resources"
"${swiftc[@]}" -O -swift-version 5 -module-cache-path "$scratch/cache" -framework AppKit \
	-o "$new/Contents/MacOS/AthenaRexMenu" "$here"/Sources/*.swift
cp "$here"/glyphs/knot-*.svg "$new/Contents/Resources/"
cat >"$new/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key><string>AthenaRexMenu</string>
	<key>CFBundleIdentifier</key><string>local.athena-rex.menubar</string>
	<key>CFBundleName</key><string>athena-rex</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1</string>
	<key>LSMinimumSystemVersion</key><string>13.0</string>
	<key>LSUIElement</key><true/>
</dict>
</plist>
EOF
rm -rf "$scratch/old"
[ -e "$app" ] && mv "$app" "$scratch/old"
mv "$new" "$app"
echo "built $app"
