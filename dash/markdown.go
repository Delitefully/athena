package main

import (
	"bytes"
	"html/template"
	"net/url"
	"strings"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/extension"
	"github.com/yuin/goldmark/parser"
	"github.com/yuin/goldmark/text"
	"github.com/yuin/goldmark/util"
)

// md renders an ask's body, server-side, the one place the page shows Markdown: paragraphs, lists, GFM tables, bare
// links, bold and code. goldmark's default renderer omits raw HTML (never html.WithUnsafe here) and drops dangerous
// URLs; safeLinks then keeps only http, https, mailto and relative links, and turns images into their alt text.
var md = goldmark.New(
	goldmark.WithExtensions(extension.NewTable(extension.WithTableCellAlignMethod(extension.TableCellAlignAttribute)),
		extension.Linkify, extension.Strikethrough), // align="…", since the page's CSP refuses a style attribute
	goldmark.WithParserOptions(parser.WithASTTransformers(util.Prioritized(safeLinks{}, 0))),
)

// renderMarkdown is an ask's body as HTML. A body goldmark cannot render shows as escaped text.
func renderMarkdown(s string) template.HTML {
	var buf bytes.Buffer
	if err := md.Convert([]byte(s), &buf); err != nil {
		return template.HTML("<p>" + template.HTMLEscapeString(s) + "</p>")
	}
	return template.HTML(buf.String())
}

// allowedURL: an http, https or mailto URL, or a relative one. Any other scheme, in any case, is refused, and so is
// what does not parse as a URL (a control character, say).
func allowedURL(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return false
	}
	switch strings.ToLower(u.Scheme) {
	case "", "http", "https", "mailto":
		return true
	}
	return false
}

// safeLinks unwraps every link whose URL allowedURL refuses, keeping its text, and every image, keeping its alt text:
// the page shows no image from a body (the CSP would block most, and a data: one is not worth the review).
type safeLinks struct{}

func (safeLinks) Transform(doc *ast.Document, reader text.Reader, _ parser.Context) {
	src := reader.Source()
	var drop []ast.Node
	ast.Walk(doc, func(n ast.Node, entering bool) (ast.WalkStatus, error) {
		if !entering {
			return ast.WalkContinue, nil
		}
		switch v := n.(type) {
		case *ast.Link:
			if !allowedURL(string(v.Destination)) {
				drop = append(drop, n)
			}
		case *ast.AutoLink:
			u := string(v.URL(src))
			if v.AutoLinkType == ast.AutoLinkEmail && !strings.HasPrefix(strings.ToLower(u), "mailto:") {
				u = "mailto:" + u
			}
			if !allowedURL(u) {
				drop = append(drop, n)
			}
		case *ast.Image:
			drop = append(drop, n)
		}
		return ast.WalkContinue, nil
	})
	for _, n := range drop {
		parent := n.Parent()
		if parent == nil {
			continue
		}
		if al, ok := n.(*ast.AutoLink); ok {
			parent.ReplaceChild(parent, n, ast.NewString(al.Label(src)))
			continue
		}
		for c := n.FirstChild(); c != nil; {
			next := c.NextSibling()
			parent.InsertBefore(parent, n, c)
			c = next
		}
		parent.RemoveChild(parent, n)
	}
}
