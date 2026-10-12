package main

import (
	_ "embed"
	"strings"
)

// icons.txt names the icons of static/icons.svg that an ask's option may show; an unknown name shows no icon.
//
//go:embed icons.txt
var iconList string

var icons = func() map[string]bool {
	out := map[string]bool{}
	for _, line := range strings.Split(iconList, "\n") {
		if line = strings.TrimSpace(line); line != "" && !strings.HasPrefix(line, "#") {
			out[line] = true
		}
	}
	return out
}()

func knownIcon(name string) bool { return icons[name] }
