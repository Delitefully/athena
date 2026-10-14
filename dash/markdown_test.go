package main

import (
	"regexp"
	"strings"
	"testing"
)

// Nothing in a body can put markup, a script or a link to anything but http, https, mailto or a relative URL on the page.
func TestABodyCannotInjectMarkupOrScript(t *testing.T) {
	vectors := []string{
		"<script>alert(1)</script>",
		"<img src=x onerror=alert(1)>",
		`<a href="javascript:alert(1)">x</a>`,
		"<iframe src=//evil.test></iframe>",
		"[x](javascript:alert(1))",
		"[x](JaVaScRiPt:alert(1))",
		"[x](  javascript:alert(1))",
		"[x](&#106;avascript:alert(1))",
		"[x](java&#x09;script:alert(1))",
		"[x](data:text/html;base64,PHNjcmlwdD4=)",
		"[x](vbscript:msgbox(1))",
		"[x](file:///etc/passwd)",
		"[x][r]\n\n[r]: javascript:alert(1)",
		"<javascript:alert(1)>",
		"<JaVaScRiPt:alert(1)>",
		"![a](javascript:alert(1))",
		"![a](data:image/svg+xml;base64,PHN2Zz4=)",
		"![a](https://evil.test/pixel.png)",
		`[x](https://ok.test "t\"><script>alert(1)</script>")`,
		"| a | b |\n|---|---|\n| <script>x</script> | [y](javascript:alert(1)) |",
		"`<script>`",
		"```html\n<script>alert(1)</script>\n```",
	}
	// Text may say anything, escaped: what must never appear is a tag, an event handler or a URL in an attribute.
	bad := regexp.MustCompile(`(?i)<script|<img|<iframe|\son\w+=|(href|src)="\s*(javascript|vbscript|data|file|ftp)|src=|<a href="[^"]*evil`)
	for _, v := range vectors {
		out := string(renderMarkdown(v))
		if m := bad.FindString(out); m != "" {
			t.Errorf("%q rendered %q (found %q)", v, out, m)
		}
	}
}

// The scheme allowlist is checked on top of goldmark's own dangerous-URL check, so it also refuses what goldmark lets by.
func TestOnlyHTTPMailtoAndRelativeLinks(t *testing.T) {
	for raw, want := range map[string]bool{
		"https://github.com/acme/web/pull/7": true,
		"http://127.0.0.1:2843/":             true,
		"HTTPS://Example.test/x":             true,
		"mailto:dev@acme.test":               true,
		"/preview/a1/index.html":             true,
		"notes.html#top":                     true,
		"JaVaScRiPt:alert(1)":                false,
		"javascript:alert(1)":                false,
		"ftp://files.acme.test/x":            false,
		"chrome://settings":                  false,
		"tel:+15555550100":                   false,
		"\x01javascript:alert(1)":            false,
		"data:text/plain,hi":                 false,
	} {
		if got := allowedURL(raw); got != want {
			t.Errorf("allowedURL(%q) = %v, want %v", raw, got, want)
		}
	}
	// goldmark itself lets ftp: by; the allowlist keeps the text and drops the link.
	out := string(renderMarkdown("get [the file](ftp://files.acme.test/x) and [this](JaVaScRiPt:alert(1)) <ftp://files.acme.test/y>"))
	if strings.Contains(out, "<a") || !strings.Contains(out, "the file") || !strings.Contains(out, "this") || !strings.Contains(out, "ftp://files.acme.test/y") {
		t.Fatalf("got %q", out)
	}
}

func TestABodyRendersParagraphsListsTablesAndLinks(t *testing.T) {
	// A column's alignment is an attribute: the page's CSP refuses inline styles.
	out := string(renderMarkdown("**Rec: B.** Fewer hosts.\n\nSecond paragraph with `code`.\n\n" +
		"- one\n- two\n\n1. first\n2. second\n\n" +
		"| Option | Cost |\n|---|--:|\n| A | 2 d |\n| B | 1 d |\n\n" +
		"See [the PR](https://github.com/acme/web/pull/7), https://acme.test/x and <mailto:dev@acme.test>."))
	for _, want := range []string{
		"<p><strong>Rec: B.</strong> Fewer hosts.</p>",
		"<p>Second paragraph with <code>code</code>.</p>",
		"<ul>\n<li>one</li>\n<li>two</li>\n</ul>",
		"<ol>\n<li>first</li>",
		"<table>", "<th>Option</th>", `<td align="right">2 d</td>`, "<td>B</td>",
		`<a href="https://github.com/acme/web/pull/7">the PR</a>`,
		`<a href="https://acme.test/x">https://acme.test/x</a>`,
		`<a href="mailto:dev@acme.test">mailto:dev@acme.test</a>`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
}
